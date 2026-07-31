"""Private read-only dashboard: session auth, status, upload list, CSV preview.

Mounted by `main.create_app` under `/dashboard`. JSON APIs live under
`/dashboard/api`; the compiled React application and its SPA fallback are served
from `/dashboard` so the browser, the read APIs, and the ingest endpoints all
share one origin and no CORS configuration is required.

Everything here reads. Nothing in this module writes to the database, the
uploads root, or any stored blob.

See prd/phase-4-frontend-dashboard.md.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import math
import re
import sqlite3
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("piserver.dashboard")


def _server():
    """The `main` module, imported on use rather than at module load.

    `main` imports this module at its top level and builds its app instance at
    the bottom, so a module-level `import main` here would be a cycle whose
    outcome depends on which module a caller imports first.
    """
    import main

    return main


SESSION_COOKIE_NAME = "dashboard_session"
SESSION_COOKIE_PATH = "/dashboard"
# 12 hours, as an exact contract: a session whose payload does not span exactly
# this many seconds is rejected rather than honoured for its stated lifetime.
SESSION_LIFETIME_SECONDS = 43_200
MIN_SESSION_SECRET_BYTES = 32

LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 900  # 15 minutes

# One missed heartbeat at the recommended five-minute cadence still reads as
# online; two consecutive misses do not.
ONLINE_WINDOW_SECONDS = 600

DEFAULT_UPLOAD_PAGE_SIZE = 50
MAX_UPLOAD_PAGE_SIZE = 100

MAX_PREVIEW_RECORDS = 100
MAX_PREVIEW_FIELD_CHARACTERS = 262_144
MAX_PREVIEW_CONTENT_BYTES = 1_048_576
# Headroom over the field cap so an oversized field is rejected by our own
# check with a 422 rather than by the parser's global limit.
CSV_PARSER_FIELD_LIMIT = MAX_PREVIEW_FIELD_CHARACTERS * 2

# Decimal integers with no sign, no padding, and no surrounding whitespace.
CANONICAL_POSITIVE_INTEGER = re.compile(r"\A[1-9][0-9]*\Z")

UPLOAD_QUERY_PARAMETERS = frozenset({"limit", "before_id"})

# `style-src` allows inline styles because the Radix dialog sets positioning and
# scroll-lock styles on elements at runtime (§4). `script-src` stays strict:
# no 'unsafe-inline', no 'unsafe-eval', no external origins.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
    )
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

NO_STORE = "no-store"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


# --- Configuration validation -------------------------------------------------


def validate_dashboard_settings(settings) -> None:
    """Refuse to start with dashboard configuration that is missing or unsafe.

    Raised failures are logged as well: a refused start shows up in a platform
    healthcheck only as "never became healthy", and the traceback is easy to
    miss in a deploy log.
    """
    problems = []

    if not settings.dashboard_password:
        problems.append("DASHBOARD_PASSWORD environment variable is required")
    if not settings.dashboard_session_secret:
        problems.append("DASHBOARD_SESSION_SECRET environment variable is required")
    elif len(settings.dashboard_session_secret.encode("utf-8")) < MIN_SESSION_SECRET_BYTES:
        problems.append(
            "DASHBOARD_SESSION_SECRET must contain at least "
            f"{MIN_SESSION_SECRET_BYTES} UTF-8 bytes"
        )
    if (
        settings.dashboard_password
        and settings.api_key
        and settings.dashboard_password == settings.api_key
    ):
        problems.append(
            "DASHBOARD_PASSWORD must not equal API_KEY: the ingest credential "
            "must never be able to sign in to the dashboard"
        )

    if problems:
        message = "; ".join(problems)
        logger.critical("REFUSING TO START: %s", message)
        raise RuntimeError(message)


# --- Sessions -----------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, payload: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()


def issue_session(secret: str, issued_at: int) -> tuple[str, int]:
    """Return a signed `payload.signature` token and its expiry epoch second."""
    expires_at = issued_at + SESSION_LIFETIME_SECONDS
    body = json.dumps(
        {"exp": expires_at, "iat": issued_at}, separators=(",", ":"), sort_keys=True
    )
    payload = _b64url_encode(body.encode("utf-8"))
    return f"{payload}.{_b64url_encode(_signature(secret, payload))}", expires_at


def verify_session(secret: str, token: Optional[str], now: int) -> Optional[int]:
    """Return the session's expiry epoch second, or None if it is not usable.

    Rejects a tampered signature, a malformed payload, a lifetime that is not
    exactly 12 hours, and an expiry that has been reached.
    """
    if not token or not secret:
        return None

    payload, separator, provided = token.partition(".")
    if not separator or not payload or not provided:
        return None

    try:
        provided_signature = _b64url_decode(provided)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(provided_signature, _signature(secret, payload)):
        return None

    try:
        claims = json.loads(_b64url_decode(payload).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None

    issued_at, expires_at = claims.get("iat"), claims.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return None
    if isinstance(issued_at, bool) or isinstance(expires_at, bool):
        return None
    if expires_at - issued_at != SESSION_LIFETIME_SECONDS:
        return None
    if now >= expires_at:
        return None
    return expires_at


def _cookie_header(value: str, max_age: int, secure: bool) -> str:
    attributes = [
        f"{SESSION_COOKIE_NAME}={value}",
        f"Max-Age={max_age}",
        f"Path={SESSION_COOKIE_PATH}",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def session_cookie(token: str, secure: bool) -> str:
    return _cookie_header(token, SESSION_LIFETIME_SECONDS, secure)


def clearing_cookie(secure: bool) -> str:
    return _cookie_header("", 0, secure)


# --- Login throttling ---------------------------------------------------------


class LoginRateLimiter:
    """Per-source failed-login counter over a rolling window.

    In-memory is sufficient for the one-replica MVP: a process restart clears
    it, which is documented rather than worked around. Expired entries are
    pruned so source addresses are not retained beyond the window.
    """

    def __init__(
        self,
        limit: int = LOGIN_FAILURE_LIMIT,
        window_seconds: int = LOGIN_FAILURE_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for source in list(self._failures):
            failures = self._failures[source]
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                del self._failures[source]

    def retry_after(self, source: str, now: float) -> Optional[int]:
        """Whole seconds until this source may try again, or None if it may now."""
        self._prune(now)
        failures = self._failures.get(source)
        if failures is None or len(failures) < self._limit:
            return None
        return max(1, math.ceil(failures[0] + self._window - now))

    def record_failure(self, source: str, now: float) -> None:
        self._prune(now)
        self._failures.setdefault(source, deque()).append(now)

    def clear(self, source: str) -> None:
        self._failures.pop(source, None)


def client_address(request: Request) -> str:
    """The source address to throttle, as the rightmost trusted forwarded hop.

    Railway appends the real client address to `X-Forwarded-For`; anything to
    the left of it may have been supplied by the client, so only the rightmost
    value is read. An absent or unparseable header falls back to the direct
    connection address, which is what local development uses.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        candidate = forwarded.rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            logger.warning("Ignoring an unparseable rightmost X-Forwarded-For value")

    client = request.client
    if client is None or not client.host:
        return "unknown"
    try:
        return str(ipaddress.ip_address(client.host))
    except ValueError:
        return client.host


# --- Request helpers ----------------------------------------------------------


def get_settings(request: Request):
    return request.app.state.settings


def unauthenticated(settings) -> HTTPException:
    """A 401 that also clears whatever cookie the browser presented."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"set-cookie": clearing_cookie(settings.secure_cookies)},
    )


def require_session(request: Request, settings=Depends(get_settings)) -> int:
    expires_at = verify_session(
        settings.dashboard_session_secret,
        request.cookies.get(SESSION_COOKIE_NAME),
        int(time.time()),
    )
    if expires_at is None:
        raise unauthenticated(settings)
    return expires_at


def read_connection(database_path: Path) -> sqlite3.Connection:
    """A row-mapped connection that SQLite itself refuses to let us write."""
    connection = _server().connect(database_path)
    connection.execute("PRAGMA query_only=1")
    connection.row_factory = sqlite3.Row
    return connection


def parse_rfc3339_utc(value: str) -> datetime:
    """Parse a stored RFC 3339 UTC timestamp, rejecting anything else."""
    text = value.strip()
    if not _server().RFC3339_UTC_PATTERN.match(text):
        raise ValueError("not an RFC 3339 UTC timestamp")
    body = text[:-1] if text[-1] in "Zz" else text[:-6]
    return datetime.fromisoformat(body[:10] + "T" + body[11:]).replace(tzinfo=timezone.utc)


# --- Response models ----------------------------------------------------------


class SessionRequest(BaseModel):
    password: str


class SessionResponse(BaseModel):
    authenticated: bool
    expires_at: str


class StatusResponse(BaseModel):
    status: str
    device_id: Optional[str]
    last_seen_at: Optional[str]
    online_window_seconds: int
    server_time: str


class UploadItem(BaseModel):
    id: int
    device_id: str
    card_uuid: str
    filename: str
    size: int
    received_at: str


class UploadListResponse(BaseModel):
    items: list[UploadItem]
    next_before_id: Optional[int]


class PreviewResponse(BaseModel):
    upload_id: int
    filename: str
    card_uuid: str
    records: list[list[str]]
    truncated: bool


# --- CSV preview --------------------------------------------------------------


class PreviewRejected(Exception):
    """The stored file cannot be previewed; `reason` is safe to show a user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def preview_records(text: str) -> tuple[list[list[str]], bool]:
    """Parse at most the first 100 records within the aggregate content cap.

    A record is either returned whole or omitted whole: a record that would push
    the accumulated cell content past the cap is dropped and marks the preview
    truncated, so no response ever contains a partial CSV record.
    """
    if csv.field_size_limit() < CSV_PARSER_FIELD_LIMIT:
        csv.field_size_limit(CSV_PARSER_FIELD_LIMIT)

    records: list[list[str]] = []
    total_bytes = 0
    truncated = False

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        for record in reader:
            for cell in record:
                if len(cell) > MAX_PREVIEW_FIELD_CHARACTERS:
                    raise PreviewRejected(
                        "A CSV field exceeds the "
                        f"{MAX_PREVIEW_FIELD_CHARACTERS}-character preview limit."
                    )

            if len(records) >= MAX_PREVIEW_RECORDS:
                truncated = True
                break

            record_bytes = sum(len(cell.encode("utf-8")) for cell in record)
            if total_bytes + record_bytes > MAX_PREVIEW_CONTENT_BYTES:
                truncated = True
                break

            total_bytes += record_bytes
            records.append(record)
    except csv.Error as exc:
        raise PreviewRejected("The stored file is not valid CSV.") from exc

    return records, truncated


def load_preview_text(settings, stored_path: str) -> str:
    """Read a stored blob as strict UTF-8 after confirming it is in the root.

    The path comes from a row selected by integer upload ID, but it is still
    re-checked against the configured uploads root: a row that points anywhere
    else is a conflict, not a file to open.
    """
    uploads_root = settings.uploads_root
    try:
        resolved_root = uploads_root.resolve()
        resolved = Path(stored_path).resolve()
    except OSError as exc:
        logger.exception("Could not resolve a stored upload path")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the stored file",
        ) from exc

    if not resolved.is_relative_to(resolved_root):
        logger.error("Refusing to read an upload row pointing outside the uploads root")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored file for this upload is unavailable",
        )

    if not resolved.is_file():
        logger.warning("Upload row has no readable stored file")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored file for this upload is unavailable",
        )

    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        # The stored path is deliberately absent from the response body.
        logger.exception("Could not read a stored upload blob")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the stored file",
        ) from exc

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreviewRejected("The stored file is not valid UTF-8 text.") from exc


# --- Static assets ------------------------------------------------------------


def _asset_response(path: Path, immutable: bool) -> FileResponse:
    return FileResponse(
        path,
        headers={"Cache-Control": IMMUTABLE_CACHE if immutable else NO_STORE},
    )


def _spa_shell(settings) -> Response:
    """The login/dashboard shell, or a clear 503 when no build is present."""
    index = settings.dashboard_dist_root / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The dashboard frontend has not been built. Run the Vite build, "
                "or use the Vite development server for local development."
            ),
        )
    return _asset_response(index, immutable=False)


def register_dashboard(app: FastAPI) -> None:
    """Attach the dashboard's security headers, read APIs, and static shell."""
    app.state.login_rate_limiter = LoginRateLimiter()

    @app.middleware("http")
    async def dashboard_security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/dashboard" or path.startswith("/dashboard/"):
            for header, value in SECURITY_HEADERS.items():
                response.headers[header] = value
            # Hashed assets set their own long-lived value first; everything
            # else, including every API response and the SPA shell, is no-store.
            response.headers.setdefault("Cache-Control", NO_STORE)
        return response

    # --- Session ---

    @app.post("/dashboard/api/session", status_code=status.HTTP_204_NO_CONTENT)
    def create_session(
        payload: SessionRequest, request: Request, settings=Depends(get_settings)
    ) -> Response:
        limiter: LoginRateLimiter = request.app.state.login_rate_limiter
        source = client_address(request)
        now = time.time()

        retry_after = limiter.retry_after(source, now)
        if retry_after is not None:
            logger.warning("Throttled a dashboard login attempt")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts",
                headers={"Retry-After": str(retry_after)},
            )

        # Fixed-length digests keep the comparison constant-time for non-ASCII
        # input too, where the raw UTF-8 encodings could differ in length.
        submitted = hashlib.sha256(payload.password.encode("utf-8")).digest()
        configured = hashlib.sha256(settings.dashboard_password.encode("utf-8")).digest()
        if not hmac.compare_digest(submitted, configured):
            limiter.record_failure(source, now)
            logger.warning("Rejected a dashboard login with an incorrect password")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

        limiter.clear(source)
        token, _ = issue_session(settings.dashboard_session_secret, int(now))
        logger.info("Issued a dashboard session")
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"set-cookie": session_cookie(token, settings.secure_cookies)},
        )

    @app.get("/dashboard/api/session", response_model=SessionResponse)
    def read_session(expires_at: int = Depends(require_session)) -> SessionResponse:
        return SessionResponse(
            authenticated=True,
            expires_at=_server().rfc3339_utc(datetime.fromtimestamp(expires_at, timezone.utc)),
        )

    @app.delete("/dashboard/api/session", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(settings=Depends(get_settings)) -> Response:
        """Idempotent: logging out twice, or without a session, is harmless."""
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"set-cookie": clearing_cookie(settings.secure_cookies)},
        )

    # --- Pi status ---

    @app.get(
        "/dashboard/api/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_session)],
    )
    def read_status(settings=Depends(get_settings)) -> StatusResponse:
        try:
            with closing(read_connection(settings.database_path)) as connection:
                row = connection.execute(
                    "SELECT device_id, received_at FROM pings ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            # An unreadable database is unknown, not offline: reporting offline
            # would blame the Pi for a server fault.
            logger.exception("Could not read the pings table for dashboard status")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read the connection status",
            ) from exc

        now = datetime.now(timezone.utc)
        server_time = _server().rfc3339_utc(now)

        if row is None:
            return StatusResponse(
                status="never_seen",
                device_id=None,
                last_seen_at=None,
                online_window_seconds=ONLINE_WINDOW_SECONDS,
                server_time=server_time,
            )

        try:
            last_seen = parse_rfc3339_utc(row["received_at"])
        except (ValueError, TypeError) as exc:
            logger.error(
                "Newest ping row has a received_at that is not an RFC 3339 UTC "
                "timestamp; reporting status as unavailable"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read the connection status",
            ) from exc

        age_seconds = (now - last_seen).total_seconds()
        if age_seconds < 0:
            logger.warning(
                "Newest ping is %.0f seconds in the future; treating the device as "
                "online and returning the stored timestamp unchanged",
                -age_seconds,
            )
        online = age_seconds <= ONLINE_WINDOW_SECONDS

        return StatusResponse(
            status="online" if online else "offline",
            device_id=row["device_id"],
            last_seen_at=_server().rfc3339_utc(last_seen),
            online_window_seconds=ONLINE_WINDOW_SECONDS,
            server_time=server_time,
        )

    # --- Uploads ---

    @app.get(
        "/dashboard/api/uploads",
        response_model=UploadListResponse,
        dependencies=[Depends(require_session)],
    )
    def list_uploads(request: Request, settings=Depends(get_settings)) -> UploadListResponse:
        limit, before_id = _upload_query(request)

        # One extra row answers "is there another page?" without a second query.
        sql = (
            "SELECT id, device_id, card_uuid, filename, size, received_at FROM uploads "
        )
        parameters: list[Any] = []
        if before_id is not None:
            sql += "WHERE id < ? "
            parameters.append(before_id)
        sql += "ORDER BY id DESC LIMIT ?"
        parameters.append(limit + 1)

        try:
            with closing(read_connection(settings.database_path)) as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            logger.exception("Could not read the uploads table for the dashboard")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read stored uploads",
            ) from exc

        has_more = len(rows) > limit
        page = rows[:limit]

        items = []
        for row in page:
            try:
                received_at = _server().rfc3339_utc(parse_rfc3339_utc(row["received_at"]))
            except (ValueError, TypeError) as exc:
                logger.error(
                    "Upload row %s has a received_at that is not an RFC 3339 UTC "
                    "timestamp",
                    row["id"],
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not read stored uploads",
                ) from exc
            items.append(
                UploadItem(
                    id=int(row["id"]),
                    device_id=row["device_id"],
                    card_uuid=row["card_uuid"],
                    filename=row["filename"],
                    size=int(row["size"]),
                    received_at=received_at,
                )
            )

        return UploadListResponse(
            items=items,
            next_before_id=int(page[-1]["id"]) if has_more and page else None,
        )

    @app.get(
        "/dashboard/api/uploads/{upload_id}/preview",
        response_model=PreviewResponse,
        dependencies=[Depends(require_session)],
    )
    def preview_upload(upload_id: str, settings=Depends(get_settings)) -> PreviewResponse:
        if not CANONICAL_POSITIVE_INTEGER.match(upload_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="upload_id must be a positive integer",
            )
        identifier = int(upload_id)

        try:
            with closing(read_connection(settings.database_path)) as connection:
                row = connection.execute(
                    "SELECT id, card_uuid, filename, stored_path FROM uploads WHERE id = ?",
                    (identifier,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.exception("Could not read an upload row for preview")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read stored uploads",
            ) from exc

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found"
            )

        try:
            text = load_preview_text(settings, row["stored_path"])
            records, truncated = preview_records(text)
        except PreviewRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.reason
            ) from exc

        return PreviewResponse(
            upload_id=identifier,
            filename=row["filename"],
            card_uuid=row["card_uuid"],
            records=records,
            truncated=truncated,
        )

    # --- Compiled frontend ---

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_root(settings=Depends(get_settings)) -> Response:
        return _spa_shell(settings)

    @app.get("/dashboard/{asset_path:path}", include_in_schema=False)
    def dashboard_asset(asset_path: str, settings=Depends(get_settings)) -> Response:
        # An unmatched dashboard API route is a 404, never the SPA shell: a
        # fetch for a mistyped endpoint must not receive HTML with a 200.
        if asset_path == "api" or asset_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        dist_root = settings.dashboard_dist_root
        if asset_path:
            try:
                candidate = (dist_root / asset_path).resolve()
            except OSError:
                candidate = None
            if (
                candidate is not None
                and candidate.is_file()
                and candidate.is_relative_to(dist_root.resolve())
            ):
                # Vite writes content-hashed names under assets/, so those may
                # be cached indefinitely; anything else is revalidated.
                immutable = candidate.parent == (dist_root / "assets").resolve()
                return _asset_response(candidate, immutable=immutable)

        return _spa_shell(settings)


def _upload_query(request: Request) -> tuple[int, Optional[int]]:
    """Validate `limit` and `before_id`, rejecting anything else with a 422."""
    seen: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in UPLOAD_QUERY_PARAMETERS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown query parameter: {key}",
            )
        if key in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"query parameter must not be repeated: {key}",
            )
        seen[key] = value

    limit = DEFAULT_UPLOAD_PAGE_SIZE
    if "limit" in seen:
        if not CANONICAL_POSITIVE_INTEGER.match(seen["limit"]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"limit must be an integer from 1 to {MAX_UPLOAD_PAGE_SIZE}",
            )
        limit = int(seen["limit"])
        if limit > MAX_UPLOAD_PAGE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"limit must be an integer from 1 to {MAX_UPLOAD_PAGE_SIZE}",
            )

    before_id = None
    if "before_id" in seen:
        if not CANONICAL_POSITIVE_INTEGER.match(seen["before_id"]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="before_id must be a positive integer",
            )
        before_id = int(seen["before_id"])

    return limit, before_id
