"""Private dashboard: session auth, status, upload list, CSV preview, downloads.

Mounted by `main.create_app` under `/dashboard`. JSON APIs live under
`/dashboard/api`; the compiled React application and its SPA fallback are served
from `/dashboard` so the browser, the read APIs, and the ingest endpoints all
share one origin and no CORS configuration is required.

Everything here reads. Nothing in this module writes to the database, the
uploads root, or any stored blob — including the download routes, which return
stored bytes and leave the row and the file exactly as they were.

See prd/phase-4-frontend-dashboard.md and prd/phase-5-file-downloads.md.
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
import os
import re
import sqlite3
import time
import zipfile
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

# A filename substring long enough for any real name but short enough that the
# LIKE pattern stays cheap.
MAX_SEARCH_CHARACTERS = 255

# The grouped view is a browsing aid, not an export: past this many cards the
# response says so rather than growing without bound.
MAX_CARD_GROUPS = 200

# Sortable columns, mapped to the SQL that orders them. Nothing from the request
# is ever interpolated into the statement — only these fixed fragments are, and
# only after the requested key matched a member of this mapping. Text columns
# sort case-insensitively so `Logger` and `logger` sit together.
UPLOAD_SORT_COLUMNS = {
    "received_at": "received_at",
    "filename": "filename COLLATE NOCASE",
    "card_uuid": "card_uuid COLLATE NOCASE",
    "device_id": "device_id COLLATE NOCASE",
    "size": "size",
}
DEFAULT_UPLOAD_SORT = "received_at"
UPLOAD_SORT_ORDERS = {"asc": "ASC", "desc": "DESC"}
DEFAULT_UPLOAD_ORDER = "desc"

MAX_PREVIEW_RECORDS = 100
MAX_PREVIEW_FIELD_CHARACTERS = 262_144
MAX_PREVIEW_CONTENT_BYTES = 1_048_576
# Headroom over the field cap so an oversized field is rejected by our own
# check with a 422 rather than by the parser's global limit.
CSV_PARSER_FIELD_LIMIT = MAX_PREVIEW_FIELD_CHARACTERS * 2

# One archive request never covers more rows than one page of the list can show,
# so "select everything on this page" always fits. The same number is already
# MAX_UPLOAD_PAGE_SIZE and the grouped view's per-card ceiling.
MAX_ARCHIVE_FILES = MAX_UPLOAD_PAGE_SIZE
# Summed from the `size` column before any file is opened. Roughly twelve
# maximum-size uploads: generous for real logger CSVs, and short of the 2 GiB
# that 100 files at the ingest ceiling would be.
MAX_ARCHIVE_BYTES = 268_435_456  # 256 MiB
# Blobs are copied into the archive in chunks so peak memory is one chunk plus
# deflate state, and so a disconnected client is noticed promptly rather than
# after a whole file. Matches the ingest path's chunk size.
ARCHIVE_CHUNK_BYTES = 262_144
# zlib's default: CSV text deflates several-fold at a small fraction of the CPU
# level 9 costs for a marginal gain.
ARCHIVE_COMPRESS_LEVEL = 6
# The ZIP format cannot represent a timestamp before this.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Decimal integers with no sign, no padding, and no surrounding whitespace.
CANONICAL_POSITIVE_INTEGER = re.compile(r"\A[1-9][0-9]*\Z")
CANONICAL_NON_NEGATIVE_INTEGER = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")

UPLOAD_FILTER_PARAMETERS = frozenset({"q", "card_uuid", "device_id"})
UPLOAD_LIST_PARAMETERS = UPLOAD_FILTER_PARAMETERS | {"limit", "offset", "sort", "order"}
DOWNLOAD_PARAMETERS: frozenset[str] = frozenset()
ARCHIVE_PARAMETERS = frozenset({"ids"})

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
    # `total` counts every row matching the filters, not just this page, so the
    # client can render "1-50 of 812" and size its pager without a second call.
    total: int
    limit: int
    offset: int
    sort: str
    order: str


class CardSummary(BaseModel):
    """One card's contribution, as the grouped view renders it."""

    device_id: str
    card_uuid: str
    file_count: int
    total_bytes: int
    oldest_received_at: str
    newest_received_at: str


class UploadSummaryResponse(BaseModel):
    total_files: int
    total_bytes: int
    card_count: int
    device_count: int
    oldest_received_at: Optional[str]
    newest_received_at: Optional[str]
    cards: list[CardSummary]
    cards_truncated: bool
    # The full set of values, deliberately *not* narrowed by the active filters:
    # these populate the filter controls, and a control that only offered the
    # value already selected could never be changed.
    all_card_uuids: list[str]
    all_device_ids: list[str]


class PreviewResponse(BaseModel):
    upload_id: int
    filename: str
    card_uuid: str
    records: list[list[str]]
    truncated: bool


# --- Upload queries -----------------------------------------------------------


def _escape_like(value: str) -> str:
    r"""Neutralize the LIKE wildcards in user text, for `ESCAPE '\'`.

    Without this a search for `_` or `%` would match every filename rather than
    the literal character the user typed.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class UploadFilters:
    """The subset of rows a request is asking about.

    The same instance drives the list query, its total, and the summary, so a
    filtered table and the statistics above it can never describe different
    row sets.
    """

    search: Optional[str] = None
    card_uuid: Optional[str] = None
    device_id: Optional[str] = None

    @property
    def active(self) -> bool:
        return any((self.search, self.card_uuid, self.device_id))

    def where(self) -> tuple[str, list[Any]]:
        """A `WHERE ...` fragment (or empty) and its bound parameters."""
        clauses: list[str] = []
        parameters: list[Any] = []

        if self.device_id is not None:
            clauses.append("device_id = ?")
            parameters.append(self.device_id)
        if self.card_uuid is not None:
            clauses.append("card_uuid = ?")
            parameters.append(self.card_uuid)
        if self.search is not None:
            # SQLite's LIKE is already case-insensitive for ASCII, which is the
            # whole of the accepted filename alphabet.
            clauses.append("filename LIKE ? ESCAPE '\\'")
            parameters.append(f"%{_escape_like(self.search)}%")

        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), parameters


def _single_valued_query(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    """Every recognized query parameter, at most once each.

    An unknown or repeated parameter is a 422 rather than something silently
    ignored: a misspelled filter that quietly returned everything would read as
    a filter that found nothing to exclude.
    """
    seen: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed:
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
    return seen


def _upload_filters(seen: dict[str, str]) -> UploadFilters:
    """Read the filter parameters, rejecting values no stored row could hold."""
    search = None
    if "q" in seen:
        # Trimmed because a trailing space from a paste is never the intent, and
        # a whitespace-only search means "no search" rather than "match nothing".
        candidate = seen["q"].strip()
        if len(candidate) > MAX_SEARCH_CHARACTERS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"q must be at most {MAX_SEARCH_CHARACTERS} characters",
            )
        search = candidate or None

    card_uuid = None
    if "card_uuid" in seen and seen["card_uuid"]:
        card_uuid = seen["card_uuid"]
        if not _server().CARD_UUID_PATTERN.match(card_uuid):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="card_uuid is not a valid card identifier",
            )

    device_id = None
    if "device_id" in seen and seen["device_id"]:
        device_id = seen["device_id"]
        if not _server().DEVICE_ID_PATTERN.match(device_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="device_id is not a valid device identifier",
            )

    return UploadFilters(search=search, card_uuid=card_uuid, device_id=device_id)


def _upload_ordering(seen: dict[str, str]) -> tuple[str, str]:
    """The validated `(sort, order)` keys, defaulting to newest first."""
    sort = seen.get("sort", DEFAULT_UPLOAD_SORT)
    if sort not in UPLOAD_SORT_COLUMNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sort must be one of: {', '.join(sorted(UPLOAD_SORT_COLUMNS))}",
        )

    order = seen.get("order", DEFAULT_UPLOAD_ORDER)
    if order not in UPLOAD_SORT_ORDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="order must be one of: asc, desc",
        )
    return sort, order


def _order_by(sort: str, order: str) -> str:
    """`ORDER BY` for a validated sort key, always broken by a unique column.

    Sizes and timestamps tie freely. Without `id` behind them, two rows sharing
    a value could swap places between one page and the next, so a row would be
    shown twice while another was never shown at all.
    """
    direction = UPLOAD_SORT_ORDERS[order]
    return f" ORDER BY {UPLOAD_SORT_COLUMNS[sort]} {direction}, id {direction}"


def _upload_paging(seen: dict[str, str]) -> tuple[int, int]:
    """The validated `(limit, offset)` for a list request."""
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

    offset = 0
    if "offset" in seen:
        if not CANONICAL_NON_NEGATIVE_INTEGER.match(seen["offset"]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="offset must be a non-negative integer",
            )
        offset = int(seen["offset"])

    return limit, offset


def _rendered_timestamp(value: Any, context: str) -> str:
    """Re-render a stored timestamp, refusing to guess at one we cannot read."""
    try:
        return _server().rfc3339_utc(parse_rfc3339_utc(value))
    except (ValueError, TypeError) as exc:
        logger.error("%s is not an RFC 3339 UTC timestamp", context)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read stored uploads",
        ) from exc


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


def resolve_stored_path(settings, stored_path: str) -> Path:
    """Resolve a row's blob path after confirming it is inside the uploads root.

    The path comes from a row selected by integer upload ID, but it is still
    re-checked against the configured uploads root: a row that points anywhere
    else is a conflict, not a file to open. `resolve()` follows symlinks, so a
    link inside the root that points outside it resolves outside and is refused
    on exactly the same footing as a stored path that was never in the root.
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

    try:
        is_file = resolved.is_file()
    except OSError as exc:
        # A path that cannot be examined at all is a read failure, not a row
        # that points at nothing.
        logger.exception("Could not examine a stored upload blob")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the stored file",
        ) from exc

    if not is_file:
        logger.warning("Upload row has no readable stored file")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored file for this upload is unavailable",
        )

    return resolved


def load_preview_text(settings, stored_path: str) -> str:
    """Read a stored blob as strict UTF-8 after confirming it is in the root."""
    resolved = resolve_stored_path(settings, stored_path)

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


# --- Downloads ----------------------------------------------------------------


def stat_stored_file(resolved: Path) -> os.stat_result:
    """Stat a blob here rather than letting the response do it later.

    `FileResponse` stats the file again when it sends, and a failure there
    happens after the route has returned — an unhandled 500 instead of a 503.
    Reading the metadata up front turns that into an ordinary error response.
    """
    try:
        return resolved.stat()
    except OSError as exc:
        logger.exception("Could not stat a stored upload blob")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the stored file",
        ) from exc


def parse_archive_ids(raw: str) -> list[int]:
    """Read the comma-separated `ids` parameter, rejecting anything ambiguous.

    No padding, no blanks, no repeats: a request that asks for the same file
    twice has no unambiguous archive, and no control in the dashboard can
    produce one. Rejecting is consistent with how every other parameter here
    treats a value it cannot read exactly one way.
    """
    if raw == "":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be a comma-separated list of upload IDs",
        )

    identifiers: list[int] = []
    seen: set[int] = set()
    for element in raw.split(","):
        if not CANONICAL_POSITIVE_INTEGER.match(element):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="ids must be a comma-separated list of upload IDs",
            )
        identifier = int(element)
        if identifier in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="ids must not repeat an upload ID",
            )
        seen.add(identifier)
        identifiers.append(identifier)

    # Checked before any query runs, which is also what keeps the request URI
    # bounded well inside every server's request-line limit.
    if len(identifiers) > MAX_ARCHIVE_FILES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"an archive may contain at most {MAX_ARCHIVE_FILES} files",
        )

    return identifiers


def _archive_member_name(row) -> str:
    """`device_id/card_uuid/filename`, re-validated component by component.

    The three parts come from a database row rather than from the request, so
    they are checked against the same whitelists ingest applied before they
    become path segments inside the archive. A row that fails is corrupt in the
    same way a `stored_path` outside the uploads root is corrupt.
    """
    server = _server()
    device_id = row["device_id"]
    card_uuid = row["card_uuid"]
    filename = row["filename"]
    try:
        if not isinstance(device_id, str) or not server.DEVICE_ID_PATTERN.match(device_id):
            raise ValueError("device_id is not a valid identifier")
        server.validate_card_uuid(card_uuid)
        server.validate_filename(filename)
    except (ValueError, TypeError) as exc:
        logger.error("Refusing to archive an upload row with an unusable identity")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored file for this upload is unavailable",
        ) from exc

    return f"{device_id}/{card_uuid}/{filename}"


def _archive_timestamp(received_at: Any) -> tuple[int, int, int, int, int, int]:
    """A ZIP `date_time` from the row's `received_at`, not the file's mtime.

    Using the stored timestamp keeps an archive identical across a volume
    restore that rewrites mtimes, and avoids `zipfile`'s hard failure on any
    modification time before 1980.
    """
    try:
        moment = parse_rfc3339_utc(received_at)
    except (ValueError, TypeError):
        return ZIP_EPOCH
    stamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    return stamp if stamp >= ZIP_EPOCH else ZIP_EPOCH


@dataclass(frozen=True)
class ArchiveMember:
    """One validated file, ready to be copied into the archive."""

    path: Path
    name: str
    date_time: tuple[int, int, int, int, int, int]


def collect_archive_members(settings, identifiers: list[int]) -> list[ArchiveMember]:
    """Resolve every requested row, or fail before a single byte is sent.

    Once the response starts streaming there is no way to report a problem, so
    everything that can be checked is checked here: that every requested ID
    exists, that the selection fits under the byte cap, and that each blob is
    present inside the uploads root with a usable name.
    """
    placeholders = ",".join("?" for _ in identifiers)
    try:
        with closing(read_connection(settings.database_path)) as connection:
            rows = connection.execute(
                "SELECT id, device_id, card_uuid, filename, size, received_at, stored_path "
                f"FROM uploads WHERE id IN ({placeholders})",
                identifiers,
            ).fetchall()
    except sqlite3.Error as exc:
        logger.exception("Could not read upload rows for an archive")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read stored uploads",
        ) from exc

    # All or nothing: an archive quietly missing a file the user selected is
    # worse than an error, because nothing on the client can notice.
    if len(rows) != len(identifiers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")

    total_bytes = sum(row["size"] for row in rows)
    if total_bytes > MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "the selected files exceed the "
                f"{MAX_ARCHIVE_BYTES}-byte archive limit; select fewer files"
            ),
        )

    members = [
        ArchiveMember(
            path=resolve_stored_path(settings, row["stored_path"]),
            name=_archive_member_name(row),
            date_time=_archive_timestamp(row["received_at"]),
        )
        for row in rows
    ]
    # Sorted by name rather than by the order the IDs arrived: the archive is a
    # function of which files were chosen, not of the order they were ticked, so
    # the same selection always produces the same bytes.
    members.sort(key=lambda member: member.name)
    return members


class _ChunkSink:
    """A write-only sink with no `tell` or `seek`.

    `zipfile` detects that it cannot seek and emits data descriptors instead of
    rewriting local headers, which is what makes a streamed archive possible
    without buffering the whole thing first.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        if not self._chunks:
            return b""
        payload = b"".join(self._chunks)
        self._chunks.clear()
        return payload


def stream_archive(members: list[ArchiveMember]):
    """Yield the ZIP a chunk at a time.

    A read that fails here is past the point where an error could be reported,
    so it is logged and allowed to propagate: the central directory is never
    written, and every extractor reports the truncated result as corrupt. A
    finished archive silently missing a member would not be noticeable at all.
    """
    sink = _ChunkSink()
    with zipfile.ZipFile(
        sink,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=ARCHIVE_COMPRESS_LEVEL,
        allowZip64=True,
    ) as archive:
        for member in members:
            info = zipfile.ZipInfo(member.name, date_time=member.date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            try:
                with archive.open(info, "w") as entry, member.path.open("rb") as source:
                    while True:
                        chunk = source.read(ARCHIVE_CHUNK_BYTES)
                        if not chunk:
                            break
                        entry.write(chunk)
                        payload = sink.drain()
                        if payload:
                            yield payload
            except OSError:
                logger.exception("Could not read a stored blob while streaming an archive")
                raise
            payload = sink.drain()
            if payload:
                yield payload

    payload = sink.drain()
    if payload:
        yield payload


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
        seen = _single_valued_query(request, UPLOAD_LIST_PARAMETERS)
        filters = _upload_filters(seen)
        sort, order = _upload_ordering(seen)
        limit, offset = _upload_paging(seen)

        where, filter_parameters = filters.where()

        try:
            with closing(read_connection(settings.database_path)) as connection:
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM uploads{where}", filter_parameters
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT id, device_id, card_uuid, filename, size, received_at "
                    f"FROM uploads{where}{_order_by(sort, order)} LIMIT ? OFFSET ?",
                    [*filter_parameters, limit, offset],
                ).fetchall()
        except sqlite3.Error as exc:
            logger.exception("Could not read the uploads table for the dashboard")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read stored uploads",
            ) from exc

        items = [
            UploadItem(
                id=int(row["id"]),
                device_id=row["device_id"],
                card_uuid=row["card_uuid"],
                filename=row["filename"],
                size=int(row["size"]),
                received_at=_rendered_timestamp(
                    row["received_at"], f"Upload row {row['id']} received_at"
                ),
            )
            for row in rows
        ]

        return UploadListResponse(
            items=items, total=total, limit=limit, offset=offset, sort=sort, order=order
        )

    @app.get(
        "/dashboard/api/uploads/summary",
        response_model=UploadSummaryResponse,
        dependencies=[Depends(require_session)],
    )
    def summarize_uploads(
        request: Request, settings=Depends(get_settings)
    ) -> UploadSummaryResponse:
        """Totals and per-card rollups for the same rows the list would return.

        Registered ahead of `/uploads/{upload_id}/preview` only in reading order;
        the paths do not overlap, so no route shadows another.
        """
        filters = _upload_filters(
            _single_valued_query(request, UPLOAD_FILTER_PARAMETERS)
        )
        where, filter_parameters = filters.where()

        try:
            with closing(read_connection(settings.database_path)) as connection:
                totals = connection.execute(
                    "SELECT COUNT(*) AS total_files, "
                    "COALESCE(SUM(size), 0) AS total_bytes, "
                    # A card identity is a device and a card together: the same
                    # card UUID read on two devices is two rollups, so counting
                    # bare UUIDs here would disagree with the rows below.
                    "COUNT(DISTINCT device_id || '/' || card_uuid) AS card_count, "
                    "COUNT(DISTINCT device_id) AS device_count, "
                    "MIN(received_at) AS oldest, MAX(received_at) AS newest "
                    f"FROM uploads{where}",
                    filter_parameters,
                ).fetchone()

                # One past the cap distinguishes "exactly at the cap" from
                # "more than we will show".
                card_rows = connection.execute(
                    "SELECT device_id, card_uuid, COUNT(*) AS file_count, "
                    "COALESCE(SUM(size), 0) AS total_bytes, "
                    "MIN(received_at) AS oldest, MAX(received_at) AS newest "
                    f"FROM uploads{where} GROUP BY device_id, card_uuid "
                    "ORDER BY newest DESC, card_uuid COLLATE NOCASE ASC LIMIT ?",
                    [*filter_parameters, MAX_CARD_GROUPS + 1],
                ).fetchall()

                # Unfiltered on purpose: see `all_card_uuids` on the model.
                all_card_uuids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT card_uuid FROM uploads "
                        "ORDER BY card_uuid COLLATE NOCASE"
                    )
                ]
                all_device_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT device_id FROM uploads "
                        "ORDER BY device_id COLLATE NOCASE"
                    )
                ]
        except sqlite3.Error as exc:
            logger.exception("Could not summarize the uploads table for the dashboard")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read stored uploads",
            ) from exc

        cards_truncated = len(card_rows) > MAX_CARD_GROUPS
        cards = [
            CardSummary(
                device_id=row["device_id"],
                card_uuid=row["card_uuid"],
                file_count=int(row["file_count"]),
                total_bytes=int(row["total_bytes"]),
                oldest_received_at=_rendered_timestamp(
                    row["oldest"], f"Card {row['card_uuid']} oldest received_at"
                ),
                newest_received_at=_rendered_timestamp(
                    row["newest"], f"Card {row['card_uuid']} newest received_at"
                ),
            )
            for row in card_rows[:MAX_CARD_GROUPS]
        ]

        # MIN/MAX over the stored text is chronological because every row this
        # server writes is second-precision UTC with a `Z` suffix.
        total_files = int(totals["total_files"])
        return UploadSummaryResponse(
            total_files=total_files,
            total_bytes=int(totals["total_bytes"]),
            card_count=int(totals["card_count"]),
            device_count=int(totals["device_count"]),
            oldest_received_at=(
                _rendered_timestamp(totals["oldest"], "Oldest upload received_at")
                if total_files
                else None
            ),
            newest_received_at=(
                _rendered_timestamp(totals["newest"], "Newest upload received_at")
                if total_files
                else None
            ),
            cards=cards,
            cards_truncated=cards_truncated,
            all_card_uuids=all_card_uuids,
            all_device_ids=all_device_ids,
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

    # --- Downloads ---

    @app.get(
        "/dashboard/api/uploads/archive",
        dependencies=[Depends(require_session)],
    )
    def download_archive(request: Request, settings=Depends(get_settings)) -> StreamingResponse:
        seen = _single_valued_query(request, ARCHIVE_PARAMETERS)
        if "ids" not in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="ids is required",
            )

        members = collect_archive_members(settings, parse_archive_ids(seen["ids"]))
        name = datetime.now(timezone.utc).strftime("uploads-%Y%m%d-%H%M%SZ.zip")

        return StreamingResponse(
            stream_archive(members),
            media_type="application/zip",
            # The name is ASCII by construction, so the plain form is enough.
            # Browsers using `fetch` ignore this and name the file themselves;
            # it is here for `curl` and any other direct client.
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get(
        "/dashboard/api/uploads/{upload_id}/download",
        dependencies=[Depends(require_session)],
    )
    def download_upload(
        upload_id: str, request: Request, settings=Depends(get_settings)
    ) -> FileResponse:
        _single_valued_query(request, DOWNLOAD_PARAMETERS)
        if not CANONICAL_POSITIVE_INTEGER.match(upload_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="upload_id must be a positive integer",
            )
        identifier = int(upload_id)

        try:
            with closing(read_connection(settings.database_path)) as connection:
                row = connection.execute(
                    "SELECT id, filename, stored_path FROM uploads WHERE id = ?",
                    (identifier,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.exception("Could not read an upload row for download")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not read stored uploads",
            ) from exc

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found"
            )

        resolved = resolve_stored_path(settings, row["stored_path"])
        # The blob is returned exactly as stored. Unlike the preview, a file
        # that is not valid UTF-8 or not valid CSV still downloads: this is the
        # bytes that were received, not an interpretation of them.
        return FileResponse(
            resolved,
            media_type="text/csv",
            filename=row["filename"],
            stat_result=stat_stored_file(resolved),
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


