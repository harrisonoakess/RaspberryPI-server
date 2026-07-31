"""Pi ingest server: POST /ping, POST /upload, GET /health, and /dashboard.

Persists one row per accepted ping, and one blob plus one row per accepted
`(device_id, card_uuid, filename)` upload, into a Railway volume so both
survive process restarts, container restarts, and redeployments.

The `card_uuid` segment is what lets one Pi deliver files from several SD cards
over its lifetime: two cards can each carry a `logger-0001.csv`, and those are
two distinct files, not one file uploaded twice.

The private read-only dashboard and its JSON APIs live under `/dashboard`, in
`dashboard.py`, served from this same process and origin.

See prd/phase-1-connection.md, prd/phase-2-data-sync.md,
prd/phase-3-multi-card-ingestion.md, and prd/phase-4-frontend-dashboard.md.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import unicodedata
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from starlette.datastructures import UploadFile

import dashboard

logger = logging.getLogger("piserver")

# Hostname-style identifier: 1-63 chars, alphanumeric start and end, internal hyphens.
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# Filesystem UUID of the SD card a file came from, validated as an opaque safe
# token rather than a strict UUID shape: vfat/exfat cards expose short
# blkid-style ids (A1B2-C3D4) while ext2/ext3/ext4 expose full RFC 4122 UUIDs.
# `\A`/`\Z` rather than `^`/`$`, so a trailing newline cannot slip through into
# a path segment. Comparison is case-sensitive: the Pi sends the exact token
# exposed under /dev/disk/by-uuid.
CARD_UUID_PATTERN = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")
MAX_CARD_UUID_CHARACTERS = 64

# RFC 3339 timestamp restricted to UTC ("Z" or a zero offset).
RFC3339_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]00:00)$"
)

# Phase 2 upload limits. The Pi enforces the same ceiling before it queues a file.
DEFAULT_MAX_UPLOAD_BYTES = 20_971_520  # 20 MiB
MAX_FILENAME_CHARACTERS = 255
# ext4/vfat cap a name at 255 *bytes*; a longer name would fail at open() with
# ENAMETOOLONG, so reject it as a bad request instead of a server error.
MAX_FILENAME_BYTES = 255
UPLOAD_CHUNK_BYTES = 262_144
# Room for the multipart boundaries, headers, and the two text fields when
# rejecting an oversized request from its Content-Length alone.
MULTIPART_ENVELOPE_ALLOWANCE = 65_536

PINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""

# Kept separate from PINGS_SCHEMA because the one-time Phase 2 -> Phase 3 reset
# recreates this table alone and must leave the ping history untouched.
UPLOADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    card_uuid TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (device_id, card_uuid, filename)
);
"""

SCHEMA = PINGS_SCHEMA + UPLOADS_SCHEMA

# Phase 2 kept `UNIQUE (device_id, filename)` and no card column, so its rows
# and blobs cannot be read under the Phase 3 identity. Startup refuses to serve
# such a database unless the operator explicitly asks for the reset.
UPLOADS_SCHEMA_ABSENT = "absent"
UPLOADS_SCHEMA_LEGACY = "legacy"
UPLOADS_SCHEMA_PHASE3 = "phase3"


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the server."""

    api_key: str
    database_path: Path
    uploads_path: Optional[Path] = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    # One-time Phase 2 -> Phase 3 authorization. A no-op once the uploads table
    # already has the Phase 3 schema, so leaving it set cannot destroy new data.
    reset_uploads: bool = False
    # Phase 4 dashboard credentials. Neither is stripped or normalized: the
    # password is compared exactly, and trimming the secret would silently
    # reduce its entropy.
    dashboard_password: str = ""
    dashboard_session_secret: str = ""
    # `Secure` on the session cookie, set whenever the process is running on
    # Railway. Omitted only for plain-HTTP local development.
    secure_cookies: bool = False
    dashboard_dist_path: Optional[Path] = None

    @property
    def uploads_root(self) -> Path:
        """Directory holding upload blobs.

        Defaults to a sibling of the database so a single configured volume
        location covers both without a second environment variable.
        """
        if self.uploads_path is not None:
            return self.uploads_path
        return self.database_path.parent / "uploads"

    @property
    def dashboard_dist_root(self) -> Path:
        """Directory holding the compiled frontend the Docker build copies in."""
        if self.dashboard_dist_path is not None:
            return self.dashboard_dist_path
        return Path(__file__).resolve().parent / "static"

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "Settings":
        env = os.environ if env is None else env
        return Settings(
            api_key=env.get("API_KEY", "").strip(),
            database_path=resolve_database_path(env),
            uploads_path=resolve_uploads_path(env),
            max_upload_bytes=resolve_max_upload_bytes(env),
            reset_uploads=resolve_reset_uploads(env),
            dashboard_password=env.get("DASHBOARD_PASSWORD", ""),
            dashboard_session_secret=env.get("DASHBOARD_SESSION_SECRET", ""),
            secure_cookies=resolve_secure_cookies(env),
            dashboard_dist_path=resolve_dashboard_dist_path(env),
        )


def resolve_database_path(env: dict) -> Path:
    """Pick the database location, preferring Railway's mounted volume.

    Railway's container filesystem is ephemeral, so anything outside the volume
    is lost on redeploy. `DATABASE_PATH` wins when set explicitly; otherwise the
    database is placed inside `RAILWAY_VOLUME_MOUNT_PATH`. The local `./data`
    fallback only applies when neither is configured (local development).
    """
    explicit = env.get("DATABASE_PATH", "").strip()
    if explicit:
        return Path(explicit)

    volume = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return Path(volume) / "pings.db"

    return Path("data") / "pings.db"


def resolve_uploads_path(env: dict) -> Optional[Path]:
    """Pick the blob directory, or `None` to derive it from the database path.

    Same reasoning as `resolve_database_path`: uploads must land on the Railway
    volume, because everything outside it disappears on redeploy.
    """
    explicit = env.get("UPLOADS_PATH", "").strip()
    if explicit:
        return Path(explicit)

    volume = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return Path(volume) / "uploads"

    return None


def resolve_max_upload_bytes(env: dict) -> int:
    raw = env.get("MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"MAX_UPLOAD_BYTES must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"MAX_UPLOAD_BYTES must not be negative, got {raw!r}")
    return value


def resolve_reset_uploads(env: dict) -> bool:
    """Whether `PHASE3_RESET_UPLOADS` authorizes the one-time uploads reset."""
    return env.get("PHASE3_RESET_UPLOADS", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_secure_cookies(env: dict) -> bool:
    """Whether the dashboard session cookie carries `Secure`.

    Keyed on Railway's own deployment identifiers rather than on whether a
    volume happens to be mounted, so a locally mounted volume cannot make the
    cookie unusable over plain HTTP, and a Railway deployment without one still
    gets the attribute.
    """
    return bool(
        env.get("RAILWAY_ENVIRONMENT_ID", "").strip()
        or env.get("RAILWAY_DEPLOYMENT_ID", "").strip()
    )


def resolve_dashboard_dist_path(env: dict) -> Optional[Path]:
    """Explicit location of the compiled frontend, or `None` for the default."""
    explicit = env.get("DASHBOARD_DIST_PATH", "").strip()
    return Path(explicit) if explicit else None


def rfc3339_utc(moment: datetime) -> str:
    """Render a datetime as a second-precision RFC 3339 UTC timestamp."""
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def normalize_rfc3339_utc(value: str) -> str:
    """Validate an RFC 3339 UTC timestamp and normalize its case/offset."""
    text = value.strip()
    if not RFC3339_UTC_PATTERN.match(text):
        raise ValueError(
            "sent_at must be an RFC 3339 UTC timestamp, e.g. 2026-07-28T18:30:00Z"
        )

    if text[-1] in "Zz":
        body, offset = text[:-1], "Z"
    else:
        body, offset = text[:-6], "Z"

    # Reject values that match the shape but are not real instants (month 13, ...).
    try:
        datetime.fromisoformat(body + "+00:00")
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError("sent_at is not a valid timestamp") from exc

    return body[:10] + "T" + body[11:] + offset


class PingRequest(BaseModel):
    device_id: str
    sent_at: str

    @field_validator("device_id")
    @classmethod
    def _check_device_id(cls, value: str) -> str:
        if not DEVICE_ID_PATTERN.match(value):
            raise ValueError(
                "device_id must be 1-63 characters of letters, numbers, and "
                "internal hyphens, with no leading or trailing hyphen"
            )
        return value

    @field_validator("sent_at")
    @classmethod
    def _check_sent_at(cls, value: str) -> str:
        return normalize_rfc3339_utc(value)


class PingResponse(BaseModel):
    status: str
    device_id: str
    received_at: str


class UploadResponse(BaseModel):
    status: str
    device_id: str
    card_uuid: str
    filename: str
    size: int
    received_at: str


class HealthResponse(BaseModel):
    status: str


def validate_filename(value: str) -> str:
    """Return `value` if it is a safe, in-scope CSV basename, else raise.

    Every rule here has to hold before the value is joined to the volume path:
    a single basename, no separators, no NUL or control characters, not a `.`
    or `..` component, and a `.csv` suffix. The check is a whitelist of shapes,
    not a sanitizer — nothing is stripped or rewritten.
    """
    if not isinstance(value, str):
        raise ValueError("filename must be text")
    if not 1 <= len(value) <= MAX_FILENAME_CHARACTERS:
        raise ValueError(
            f"filename must be 1-{MAX_FILENAME_CHARACTERS} characters"
        )
    if len(value.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise ValueError(
            f"filename must be at most {MAX_FILENAME_BYTES} bytes when UTF-8 encoded"
        )
    if "/" in value or "\\" in value:
        raise ValueError("filename must not contain a path separator")
    if any(character == "\x00" or unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("filename must not contain NUL or control characters")
    if value in (".", ".."):
        raise ValueError("filename must not be a relative path component")
    # Defence in depth: after the rules above these are equalities, not fixes.
    if os.path.basename(value) != value or os.path.dirname(value):
        raise ValueError("filename must be a single basename")
    if not value.lower().endswith(".csv"):
        raise ValueError("filename must end in .csv")
    return value


def validate_card_uuid(value: str) -> str:
    """Return `value` if it is a safe card identity token, else raise.

    Like `validate_filename`, this is a whitelist of shapes rather than a
    sanitizer, and it has to hold before the value becomes a path segment under
    the uploads root. The alphabet (letters, digits, hyphen) admits both
    `blkid` short ids and full RFC 4122 UUIDs while excluding separators, dots,
    NUL, and control characters outright.
    """
    if not isinstance(value, str):
        raise ValueError("card_uuid must be text")
    if not CARD_UUID_PATTERN.match(value):
        raise ValueError(
            f"card_uuid must be 1-{MAX_CARD_UUID_CHARACTERS} characters of letters, "
            "numbers, and hyphens"
        )
    # Defence in depth: after the rule above these are equalities, not fixes.
    if os.path.basename(value) != value or os.path.dirname(value):
        raise ValueError("card_uuid must be a single path segment")
    return value


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a durable SQLite connection.

    `synchronous=FULL` means a committed row has reached the disk before the
    request returns 200, which is what the durability criteria depend on. The
    ping rate (one per 30s) makes the cost irrelevant.
    """
    connection = sqlite3.connect(database_path, timeout=10.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def uploads_schema_state(database_path: Path) -> str:
    """Classify an existing `uploads` table as Phase 2 (`legacy`) or Phase 3."""
    if not Path(database_path).exists():
        return UPLOADS_SCHEMA_ABSENT
    with closing(connect(database_path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(uploads)")]
    if not columns:
        return UPLOADS_SCHEMA_ABSENT
    return UPLOADS_SCHEMA_PHASE3 if "card_uuid" in columns else UPLOADS_SCHEMA_LEGACY


def reset_uploads_state(settings: Settings) -> None:
    """Drop Phase 2 upload state so Phase 3 starts clean.

    Deletes only the contents of the uploads root and only the `uploads` table.
    The `pings` table, the uploads root itself, and everything else on the
    volume are left alone. Any failure propagates: aborting startup is safer
    than serving a half-reset volume.
    """
    uploads_root = settings.uploads_root
    if uploads_root.exists():
        for entry in sorted(uploads_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    with closing(connect(settings.database_path)) as connection:
        connection.execute("DROP TABLE IF EXISTS uploads")
        connection.executescript(UPLOADS_SCHEMA)
        connection.commit()

    logger.warning(
        "PHASE3_RESET_UPLOADS: dropped the Phase 2 uploads table and cleared %s; "
        "ping history was preserved",
        uploads_root,
    )


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def initialize_storage(settings: Settings) -> None:
    """Create the database and the blob directory on the volume.

    A Phase 2 `uploads` table is handled before the Phase 3 schema is applied,
    because `CREATE TABLE IF NOT EXISTS` would silently leave the old table —
    and its `UNIQUE (device_id, filename)` constraint — in place.
    """
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    if uploads_schema_state(settings.database_path) == UPLOADS_SCHEMA_LEGACY:
        if not settings.reset_uploads:
            message = (
                "the uploads table is still on the Phase 2 schema (no card_uuid "
                "column). Phase 3 cannot read it. Set PHASE3_RESET_UPLOADS=1 to "
                "drop the uploads table and clear stored blobs; ping rows are "
                "kept. This deletes every Phase 2 upload permanently."
            )
            # Logged as well as raised: a refused start shows up in a platform
            # healthcheck only as "never became healthy", and the traceback is
            # easy to miss in a deploy log. This line is the one to read.
            logger.critical("REFUSING TO START: %s", message)
            raise RuntimeError(message)
        reset_uploads_state(settings)

    initialize_database(settings.database_path)
    settings.uploads_root.mkdir(parents=True, exist_ok=True)


def insert_ping(
    database_path: Path, device_id: str, sent_at: str, received_at: str
) -> int:
    with closing(connect(database_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO pings (device_id, sent_at, received_at) VALUES (?, ?, ?)",
            (device_id, sent_at, received_at),
        )
        connection.commit()
        return int(cursor.lastrowid)


def find_upload(
    database_path: Path, device_id: str, card_uuid: str, filename: str
) -> Optional[sqlite3.Row]:
    """Return the stored row for a logical identity, or None."""
    with closing(connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT device_id, card_uuid, filename, stored_path, size, received_at "
            "FROM uploads WHERE device_id = ? AND card_uuid = ? AND filename = ?",
            (device_id, card_uuid, filename),
        ).fetchone()


def insert_upload(
    database_path: Path,
    device_id: str,
    card_uuid: str,
    filename: str,
    stored_path: str,
    size: int,
    received_at: str,
) -> int:
    """Claim a logical identity. Raises sqlite3.IntegrityError if already taken."""
    with closing(connect(database_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO uploads (device_id, card_uuid, filename, stored_path, size, "
            "received_at) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, card_uuid, filename, stored_path, size, received_at),
        )
        connection.commit()
        return int(cursor.lastrowid)


def delete_upload(
    database_path: Path, device_id: str, card_uuid: str, filename: str
) -> None:
    """Release a claimed identity after the blob could not be published."""
    with closing(connect(database_path)) as connection:
        connection.execute(
            "DELETE FROM uploads WHERE device_id = ? AND card_uuid = ? AND filename = ?",
            (device_id, card_uuid, filename),
        )
        connection.commit()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(
    settings: Settings = Depends(get_settings),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Reject anything that is not the configured bearer token with a 401."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization:
        raise unauthorized

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized

    if not secrets.compare_digest(token.strip(), settings.api_key):
        logger.warning("Rejected an authenticated request with an invalid bearer token")
        raise unauthorized


class _FileTooLarge(Exception):
    """The `file` part exceeded `max_upload_bytes` while being read."""


def _form_text(form, field: str) -> str:
    """Read a required text field, rejecting a missing value or a file part."""
    value = form.get(field)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} is required",
        )
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be a text field, not a file part",
        )
    return value


def _upload_response(status_value: str, row) -> UploadResponse:
    return UploadResponse(
        status=status_value,
        device_id=row["device_id"],
        card_uuid=row["card_uuid"],
        filename=row["filename"],
        size=int(row["size"]),
        received_at=row["received_at"],
    )


async def _write_temp_blob(source: UploadFile, directory: Path, limit: int) -> tuple[Path, int]:
    """Stream `source` into a temporary file in `directory`, enforcing `limit`.

    The completed file is fsynced and closed before it is returned, so the
    caller only ever publishes bytes that are already on the disk. Any failure
    removes the temporary file rather than leaving debris on the volume.
    """
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=".incoming-", suffix=".part")
    temp_path = Path(temp_name)
    size = 0
    try:
        with os.fdopen(handle, "wb") as destination:
            while True:
                chunk = await source.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _FileTooLarge()
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, size


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    resolved = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail fast rather than start an unauthenticated server.
        if not app.state.settings.api_key:
            raise RuntimeError("API_KEY environment variable is required")
        dashboard.validate_dashboard_settings(app.state.settings)
        initialize_storage(app.state.settings)
        logger.info(
            "Storage ready: database=%s uploads=%s max_upload_bytes=%s",
            app.state.settings.database_path,
            app.state.settings.uploads_root,
            app.state.settings.max_upload_bytes,
        )
        yield

    app = FastAPI(title="Pi Ingest Server", version="4.0.0", lifespan=lifespan)
    app.state.settings = resolved
    dashboard.register_dashboard(app)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Unauthenticated liveness check for the FastAPI process only."""
        return HealthResponse(status="ok")

    @app.post("/ping", response_model=PingResponse, dependencies=[Depends(require_api_key)])
    def ping(
        payload: PingRequest, settings: Settings = Depends(get_settings)
    ) -> PingResponse:
        received_at = rfc3339_utc(datetime.now(timezone.utc))
        try:
            row_id = insert_ping(
                settings.database_path, payload.device_id, payload.sent_at, received_at
            )
        except sqlite3.Error:
            logger.exception("Failed to persist ping for device %s", payload.device_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not persist ping",
            )

        logger.info(
            "Acknowledged ping id=%s device_id=%s received_at=%s",
            row_id,
            payload.device_id,
            received_at,
        )
        return PingResponse(
            status="acknowledged",
            device_id=payload.device_id,
            received_at=received_at,
        )

    @app.post(
        "/upload",
        response_model=UploadResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def upload(
        request: Request, settings: Settings = Depends(get_settings)
    ) -> UploadResponse:
        """Store one CSV under the identity `(device_id, card_uuid, filename)`.

        Delivery from the Pi is at least once, so this handler is idempotent:
        the `UNIQUE (device_id, card_uuid, filename)` constraint decides which
        attempt stores the blob, and every later attempt gets `already_stored`
        with the existing metadata. The endpoint takes a raw `Request` rather
        than `UploadFile` parameters so authentication and the size ceiling are
        both applied before the multipart body is parsed.
        """
        limit = settings.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Content-Length is not a valid integer",
                )
            if declared_bytes > limit + MULTIPART_ENVELOPE_ALLOWANCE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"file must be at most {limit} bytes",
                )

        try:
            form = await request.form()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request body must be valid multipart/form-data",
            )

        try:
            device_id = _form_text(form, "device_id")
            if not DEVICE_ID_PATTERN.match(device_id):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "device_id must be 1-63 characters of letters, numbers, and "
                        "internal hyphens, with no leading or trailing hyphen"
                    ),
                )

            card_uuid = _form_text(form, "card_uuid")
            try:
                validate_card_uuid(card_uuid)
            except ValueError as exc:
                # The rejected value is not echoed back or logged verbatim.
                logger.warning("Rejected an invalid card_uuid from %s", device_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"invalid card_uuid: {exc}",
                )

            filename = _form_text(form, "filename")
            try:
                validate_filename(filename)
            except ValueError as exc:
                # The rejected value is not echoed back or logged verbatim.
                logger.warning("Rejected an out-of-scope filename from %s", device_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"invalid filename: {exc}",
                )

            source = form.get("file")
            if not isinstance(source, UploadFile):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="file must be present as a file part",
                )

            # Cheap path for a retry whose acknowledgement was lost: answer from
            # the ledger without rewriting bytes that are already stored.
            try:
                existing = find_upload(
                    settings.database_path, device_id, card_uuid, filename
                )
            except sqlite3.Error:
                logger.exception("Could not read the uploads table")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not read stored uploads",
                )
            if existing is not None:
                logger.info(
                    "Upload already stored: device_id=%s card_uuid=%s filename=%s size=%s",
                    device_id,
                    card_uuid,
                    filename,
                    existing["size"],
                )
                return _upload_response("already_stored", existing)

            # The card segment mirrors the Pi's queue layout, for the identical
            # reason: without it, two cards' same-named files collide.
            card_directory = settings.uploads_root / device_id / card_uuid
            final_path = card_directory / filename
            try:
                card_directory.mkdir(parents=True, exist_ok=True)
                temp_path, size = await _write_temp_blob(source, card_directory, limit)
            except _FileTooLarge:
                logger.warning(
                    "Rejected oversized upload: device_id=%s card_uuid=%s "
                    "filename=%s limit=%s",
                    device_id,
                    card_uuid,
                    filename,
                    limit,
                )
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"file must be at most {limit} bytes",
                )
            except OSError:
                logger.exception("Could not buffer upload for device %s", device_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not write the uploaded file",
                )

            received_at = rfc3339_utc(datetime.now(timezone.utc))

            # Claim the identity before publishing the blob, so a concurrent
            # duplicate can never overwrite a file that is already final.
            try:
                insert_upload(
                    settings.database_path,
                    device_id,
                    card_uuid,
                    filename,
                    str(final_path),
                    size,
                    received_at,
                )
            except sqlite3.IntegrityError:
                temp_path.unlink(missing_ok=True)
                stored = find_upload(
                    settings.database_path, device_id, card_uuid, filename
                )
                if stored is None:  # pragma: no cover - defensive
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Could not persist the upload",
                    )
                return _upload_response("already_stored", stored)
            except sqlite3.Error:
                temp_path.unlink(missing_ok=True)
                logger.exception("Could not record upload for device %s", device_id)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not persist the upload",
                )

            try:
                os.replace(temp_path, final_path)
            except OSError:
                # The row would otherwise point at a blob that does not exist.
                logger.exception("Could not publish blob for device %s", device_id)
                try:
                    delete_upload(settings.database_path, device_id, card_uuid, filename)
                finally:
                    temp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not store the uploaded file",
                )

            logger.info(
                "Stored upload: device_id=%s card_uuid=%s filename=%s size=%s "
                "received_at=%s",
                device_id,
                card_uuid,
                filename,
                size,
                received_at,
            )
            return UploadResponse(
                status="stored",
                device_id=device_id,
                card_uuid=card_uuid,
                filename=filename,
                size=size,
                received_at=received_at,
            )
        finally:
            # Releases Starlette's own spooled temporary files.
            await form.close()

    return app


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()
