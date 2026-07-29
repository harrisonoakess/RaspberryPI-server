"""Phase 1 connectivity server: POST /ping and GET /health.

Persists one row per accepted ping into a SQLite database that lives on a
Railway volume, so rows survive process restarts, container restarts, and
redeployments. See prd/phase-1-connection.md.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, field_validator

logger = logging.getLogger("piserver")

# Hostname-style identifier: 1-63 chars, alphanumeric start and end, internal hyphens.
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# RFC 3339 timestamp restricted to UTC ("Z" or a zero offset).
RFC3339_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]00:00)$"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the server."""

    api_key: str
    database_path: Path

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "Settings":
        env = os.environ if env is None else env
        return Settings(
            api_key=env.get("API_KEY", "").strip(),
            database_path=resolve_database_path(env),
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


class HealthResponse(BaseModel):
    status: str


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


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


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
        logger.warning("Rejected /ping with an invalid bearer token")
        raise unauthorized


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    resolved = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail fast rather than start an unauthenticated server.
        if not app.state.settings.api_key:
            raise RuntimeError("API_KEY environment variable is required")
        initialize_database(app.state.settings.database_path)
        logger.info("Ping database ready at %s", app.state.settings.database_path)
        yield

    app = FastAPI(title="Pi Connectivity Server", version="1.0.0", lifespan=lifespan)
    app.state.settings = resolved

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

    return app


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()
