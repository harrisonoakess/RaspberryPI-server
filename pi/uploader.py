#!/usr/bin/env python3
"""Upload daemon for the Raspberry Pi.

Evolves Phase 1's connectivity daemon. One non-overlapping poll loop; every
tick it asks the OS whether the configured WiFi interface is associated with an
access point. While connected it:

1. sends the Phase 1 `POST /ping` heartbeat, but only when its own (longer)
   heartbeat interval is due, and
2. uploads every file `sdcard_watcher` has queued, promptly and independently
   of the heartbeat.

The queue is namespaced per card: `queue/pending/<card_uuid>/<filename>`. Each
queued entry is delivered under the logical identity
`(device_id, card_uuid, filename)`, so two cards carrying the same filename are
two distinct files rather than one file uploaded twice.

Delivery is at least once. A queued file is marked `uploaded` and deleted only
after the server returns a `200` whose acknowledgement exactly matches the
`device_id`, `card_uuid`, `filename`, and byte size that were sent. Anything
else leaves the file pending for a later tick. The server's
`UNIQUE (device_id, card_uuid, filename)` constraint turns repeated attempts
into one stored blob and one row.

Errors never stop the loop, and repeated identical states are rate-limited so
an offline Pi cannot flood the journal.

Standard library only. See prd/phase-3-multi-card-ingestion.md.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from state import Ledger, summarize

LOGGER_NAME = "piuploader"

# Must match the server's device_id validation in server/main.py.
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# Must match the server's card_uuid validation in server/main.py. Deliberately
# an opaque safe token rather than a UUID shape: vfat/exfat cards expose short
# blkid-style ids (A1B2-C3D4) while ext2/ext3/ext4 expose full RFC 4122 UUIDs.
# `\A`/`\Z` rather than `^`/`$`, so a trailing newline cannot slip through.
CARD_UUID_PATTERN = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")

# Mirrors server/main.py's filename rules. Deliberately duplicated rather than
# shared: the Pi and the server are independent deployables coupled only by the
# HTTP contract, and the Pi must not send a name the server would reject.
MAX_FILENAME_CHARACTERS = 255
MAX_FILENAME_BYTES = 255

DEFAULT_LOG_PATH = Path("/var/log/piuploader/uploader.log")
DEFAULT_LOG_MAX_BYTES = 1_048_576
DEFAULT_LOG_BACKUP_COUNT = 5

DEFAULT_QUEUE_PATH = Path("/var/lib/piuploader/queue")
DEFAULT_STATE_DB_PATH = Path("/var/lib/piuploader/state.db")
DEFAULT_MAX_UPLOAD_BYTES = 20_971_520  # 20 MiB, matched on the server.

# The `filename` form field is authoritative (PRD §5.3); the file part's own
# filename attribute is informational, so a fixed value is sent and no real
# name is ever interpolated into a header.
MULTIPART_FILE_ATTRIBUTE = "upload.csv"

# Log categories. State changes are logged immediately; an unchanged category
# repeats no more than once per ERROR_LOG_REPEAT_SECONDS.
CATEGORY_ACKNOWLEDGED = "acknowledged"
CATEGORY_DISCONNECTED = "wifi_disconnected"
CATEGORY_WIFI_CHECK_FAILED = "wifi_check_failed"
CATEGORY_UPLOAD_PREFIX = "upload_"
CATEGORY_QUEUE_READ_FAILED = "queue_read_failed"


class ConfigError(Exception):
    """Raised when the daemon cannot start with the supplied configuration."""


class WifiCheckError(Exception):
    """Raised when the WiFi association state cannot be determined."""


class TransportError(Exception):
    """A request failed. `category` groups identical failures for logging."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class PingError(TransportError):
    """A ping attempt failed."""


class UploadError(TransportError):
    """An upload attempt failed, so the file stays pending."""


@dataclass(frozen=True)
class Config:
    server_url: str
    api_key: str
    wifi_interface: str = "wlan0"
    poll_interval_seconds: float = 30.0
    ping_interval_seconds: float = 300.0
    request_timeout_seconds: float = 10.0
    upload_timeout_seconds: float = 120.0
    error_log_repeat_seconds: float = 300.0
    queue_path: Path = DEFAULT_QUEUE_PATH
    state_db_path: Path = DEFAULT_STATE_DB_PATH
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    @property
    def ping_url(self) -> str:
        return self.server_url.rstrip("/") + "/ping"

    @property
    def upload_url(self) -> str:
        return self.server_url.rstrip("/") + "/upload"

    @property
    def pending_dir(self) -> Path:
        return self.queue_path / "pending"

    def pending_dir_for(self, card_uuid: str) -> Path:
        """The per-card queue directory. Callers validate `card_uuid` first."""
        return self.pending_dir / card_uuid

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "Config":
        env = os.environ if env is None else env

        server_url = env.get("SERVER_URL", "").strip()
        if not server_url:
            raise ConfigError("SERVER_URL is required")

        parsed = urlparse(server_url)
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            # Local development only. Every real deployment target is HTTPS.
            pass
        elif parsed.scheme != "https" or not parsed.hostname:
            raise ConfigError(
                "SERVER_URL must be an https:// URL (http:// is allowed only for localhost)"
            )

        api_key = env.get("API_KEY", "").strip()
        if not api_key:
            raise ConfigError("API_KEY is required")

        return Config(
            server_url=server_url,
            api_key=api_key,
            wifi_interface=env.get("WIFI_INTERFACE", "").strip() or "wlan0",
            poll_interval_seconds=positive_float(env, "POLL_INTERVAL_SECONDS", 30.0),
            ping_interval_seconds=positive_float(env, "PING_INTERVAL_SECONDS", 300.0),
            request_timeout_seconds=positive_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0),
            upload_timeout_seconds=positive_float(env, "UPLOAD_TIMEOUT_SECONDS", 120.0),
            error_log_repeat_seconds=positive_float(env, "ERROR_LOG_REPEAT_SECONDS", 300.0),
            queue_path=Path(env.get("QUEUE_PATH", "").strip() or DEFAULT_QUEUE_PATH),
            state_db_path=Path(
                env.get("STATE_DB_PATH", "").strip() or DEFAULT_STATE_DB_PATH
            ),
            max_upload_bytes=int(
                positive_float(env, "MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
            ),
            log_path=Path(env.get("LOG_PATH", "").strip() or DEFAULT_LOG_PATH),
            log_max_bytes=int(positive_float(env, "LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)),
            log_backup_count=int(positive_float(env, "LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)),
        )


def positive_float(env: dict, name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {raw!r}")
    return value


def rfc3339_utc(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def resolve_device_id(hostname: Optional[str] = None) -> str:
    """Derive the device_id from the Pi's hostname.

    There is one Pi, so provisioning must give it a hostname the server will
    accept. An unusable hostname is a configuration error, not a runtime state
    to retry through.
    """
    raw = socket.gethostname() if hostname is None else hostname
    candidate = raw.strip().split(".")[0]
    if not DEVICE_ID_PATTERN.match(candidate):
        raise ConfigError(
            f"hostname {raw!r} does not yield a valid device_id; set a hostname of "
            "1-63 letters, numbers, and internal hyphens"
        )
    return candidate


def is_safe_filename(name: str) -> bool:
    """Whether `name` is a safe, in-scope CSV basename.

    A whitelist of shapes: a name either passes as-is or is rejected. Nothing
    is stripped or rewritten, so a rejected name can never be "fixed" into a
    different file's identity.
    """
    if not isinstance(name, str):
        return False
    if not 1 <= len(name) <= MAX_FILENAME_CHARACTERS:
        return False
    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(character == "\x00" or unicodedata.category(character) == "Cc" for character in name):
        return False
    if name in (".", ".."):
        return False
    if os.path.basename(name) != name or os.path.dirname(name):
        return False
    return name.lower().endswith(".csv")


def is_safe_card_uuid(value: str) -> bool:
    """Whether `value` is a filesystem UUID safe to use as a path segment.

    Same whitelist reasoning as `is_safe_filename`: the token either passes
    as-is or is rejected, and it is the only card-derived value that is ever
    joined to the queue path or sent to the server.
    """
    if not isinstance(value, str):
        return False
    return CARD_UUID_PATTERN.match(value) is not None


class StateLogger:
    """Logs state changes immediately and rate-limits unchanged repeats.

    - A category different from the previous one is logged immediately.
    - The same category repeats at most once per `repeat_seconds`.
    - `always=True` (used for successful acknowledgements) bypasses the limit.
    """

    def __init__(
        self,
        logger: logging.Logger,
        repeat_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._repeat_seconds = repeat_seconds
        self._clock = clock
        self._last_category: Optional[str] = None
        self._last_logged_at: Optional[float] = None
        self.suppressed_count = 0

    @property
    def logger(self) -> logging.Logger:
        """The underlying logger, for messages that are not state transitions."""
        return self._logger

    def record(
        self,
        category: str,
        message: str,
        level: int = logging.INFO,
        always: bool = False,
    ) -> bool:
        now = self._clock()
        changed = category != self._last_category
        due = (
            self._last_logged_at is None
            or now - self._last_logged_at >= self._repeat_seconds
        )

        if changed or always or due:
            if not changed and self.suppressed_count:
                message = f"{message} (suppressed {self.suppressed_count} identical entries)"
            self._logger.log(level, message)
            self._last_category = category
            self._last_logged_at = now
            self.suppressed_count = 0
            return True

        self._last_category = category
        self.suppressed_count += 1
        return False


class IntervalTimer:
    """Fires the first time it is asked, then at most once per interval.

    Used to run the heartbeat on its own five-minute cadence inside the
    30-second upload poll loop. `record()` is called on every *attempt*, so a
    failing server is retried on the heartbeat interval rather than every tick.
    """

    def __init__(
        self, interval_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._last_fired_at: Optional[float] = None

    def due(self) -> bool:
        if self._last_fired_at is None:
            return True
        return self._clock() - self._last_fired_at >= self._interval

    def record(self) -> None:
        self._last_fired_at = self._clock()


def is_wifi_connected(
    interface: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sysfs_root: Path = Path("/sys/class/net"),
) -> bool:
    """Report whether `interface` is associated with an access point.

    Primary source is `iw dev <iface> link`, which speaks directly to the
    association state. If `iw` is unavailable or errors, fall back to the
    kernel's operstate, which wpa_supplicant only raises to "up" once
    association completes.
    """
    try:
        completed = runner(
            ["iw", "dev", interface, "link"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return _operstate_is_up(interface, sysfs_root)

    if completed.returncode != 0:
        return _operstate_is_up(interface, sysfs_root)

    output = (completed.stdout or "").strip().lower()
    if not output:
        return _operstate_is_up(interface, sysfs_root)
    return not output.startswith("not connected")


def _operstate_is_up(interface: str, sysfs_root: Path) -> bool:
    operstate = sysfs_root / interface / "operstate"
    try:
        return operstate.read_text().strip() == "up"
    except OSError as exc:
        raise WifiCheckError(
            f"cannot determine WiFi state for {interface!r}: {exc}"
        ) from exc


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that verifies TLS and refuses to follow redirects.

    Credentials go to the configured SERVER_URL only, so a redirect is treated
    as a failed request rather than followed to another host.
    """

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    # Default context performs hostname and certificate-chain verification.
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), NoRedirect
    )


def _perform_request(
    request: urllib.request.Request,
    timeout: float,
    opener: urllib.request.OpenerDirector,
    error_type,
) -> tuple:
    """Send one request. Raises `error_type` with a stable category on failure."""
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise error_type(*_categorize_http_error(exc)) from exc
    except urllib.error.URLError as exc:
        raise error_type(*_categorize_url_error(exc)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise error_type("timeout", f"request timed out: {exc}") from exc
    except ssl.SSLError as exc:
        raise error_type("tls_error", f"TLS failure: {exc}") from exc
    except OSError as exc:
        raise error_type("connection_error", f"connection failed: {exc}") from exc


def send_ping(
    config: Config,
    device_id: str,
    sent_at: str,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> dict:
    """POST one ping. Raises PingError with a stable category on any failure."""
    opener = opener if opener is not None else _build_opener()
    body = json.dumps({"device_id": device_id, "sent_at": sent_at}).encode("utf-8")
    request = urllib.request.Request(
        config.ping_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # Never logged anywhere in this module.
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        },
    )

    status_code, payload = _perform_request(
        request, config.request_timeout_seconds, opener, PingError
    )

    if status_code != 200:
        raise PingError(
            f"unexpected_status_{status_code}",
            f"server returned HTTP {status_code}",
        )

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PingError("invalid_ack", f"response was not JSON: {exc}") from exc

    if not isinstance(parsed, dict) or parsed.get("status") != "acknowledged":
        raise PingError("invalid_ack", "response did not acknowledge the ping")

    return parsed


def build_multipart(
    device_id: str, card_uuid: str, filename: str, content: bytes
) -> tuple:
    """Build the `POST /upload` body. Returns `(body, content_type)`.

    The boundary is random and re-drawn if it happens to occur in the payload,
    so file contents can never terminate or forge a part.
    """
    while True:
        boundary = "----piuploader" + secrets.token_hex(16)
        if boundary.encode("ascii") not in content:
            break

    parts = []
    for name, value in (
        ("device_id", device_id),
        ("card_uuid", card_uuid),
        ("filename", filename),
    ):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                "utf-8"
            )
            + value.encode("utf-8")
            + b"\r\n"
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{MULTIPART_FILE_ATTRIBUTE}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_upload(
    config: Config,
    device_id: str,
    card_uuid: str,
    filename: str,
    content: bytes,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> dict:
    """POST one file. Returns the acknowledgement, or raises UploadError.

    Only an acknowledgement that matches everything sent is accepted, because
    that acknowledgement is the sole evidence used to delete the local copy.
    """
    if not is_safe_card_uuid(card_uuid):
        raise UploadError("unsafe_card_uuid", f"refusing to send card_uuid {card_uuid!r}")
    if not is_safe_filename(filename):
        raise UploadError("unsafe_filename", f"refusing to send filename {filename!r}")
    if len(content) > config.max_upload_bytes:
        raise UploadError(
            "too_large",
            f"{filename!r} is {len(content)} bytes, over the "
            f"{config.max_upload_bytes} byte limit",
        )

    opener = opener if opener is not None else _build_opener()
    body, content_type = build_multipart(device_id, card_uuid, filename, content)
    request = urllib.request.Request(
        config.upload_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            # Never logged anywhere in this module.
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        },
    )

    status_code, payload = _perform_request(
        request, config.upload_timeout_seconds, opener, UploadError
    )

    if status_code != 200:
        raise UploadError(
            f"unexpected_status_{status_code}",
            f"server returned HTTP {status_code}",
        )

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UploadError("invalid_ack", f"response was not JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise UploadError("invalid_ack", "response was not a JSON object")
    if parsed.get("status") not in ("stored", "already_stored"):
        raise UploadError(
            "invalid_ack", f"unexpected acknowledgement status {parsed.get('status')!r}"
        )
    if parsed.get("device_id") != device_id:
        raise UploadError("mismatched_ack", "acknowledged a different device_id")
    if parsed.get("card_uuid") != card_uuid:
        raise UploadError("mismatched_ack", "acknowledged a different card_uuid")
    if parsed.get("filename") != filename:
        raise UploadError("mismatched_ack", "acknowledged a different filename")
    if parsed.get("size") != len(content):
        raise UploadError(
            "mismatched_ack",
            f"acknowledged {parsed.get('size')!r} bytes, sent {len(content)}",
        )

    return parsed


@dataclass
class UploadBatch:
    attempted: int = 0
    uploaded: int = 0
    already_stored: int = 0
    rejected: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"attempted={self.attempted} uploaded={self.uploaded} "
            f"already_stored={self.already_stored} rejected={self.rejected} "
            f"failed={self.failed}"
        )


@dataclass
class QueueScan:
    """What one pass over `queue/pending/` found.

    `entries` are the `(card_uuid, filename)` pairs to attempt, in stable
    order. `skipped` names anything that is not a two-level regular file, which
    is left untouched on disk and only reported.
    """

    entries: list
    skipped: list


def scan_pending(config: Config) -> QueueScan:
    """Enumerate `queue/pending/<card_uuid>/<filename>` entries.

    Exactly two levels: a per-card directory holding regular files. Symlinks
    are never followed — neither a symlinked card directory nor a symlinked
    queue entry — so nothing outside the queue can be read or sent. A missing
    directory simply means nothing has been queued yet.
    """
    try:
        top_level = list(os.scandir(config.pending_dir))
    except FileNotFoundError:
        return QueueScan(entries=[], skipped=[])

    entries = []
    skipped = []
    for card_entry in top_level:
        try:
            if not card_entry.is_dir(follow_symlinks=False):
                # A flat file here is either debris or Phase 2 queue state; it
                # has no card identity, so it cannot be uploaded.
                skipped.append(card_entry.name)
                continue
            children = list(os.scandir(card_entry.path))
        except OSError:
            skipped.append(card_entry.name)
            continue

        for entry in children:
            try:
                if not entry.is_file(follow_symlinks=False):
                    skipped.append(f"{card_entry.name}/{entry.name}")
                    continue
            except OSError:
                skipped.append(f"{card_entry.name}/{entry.name}")
                continue
            entries.append((card_entry.name, entry.name))

    return QueueScan(entries=sorted(entries), skipped=sorted(skipped))


def queued_entries(config: Config) -> list:
    """The `(card_uuid, filename)` pairs waiting to be uploaded."""
    return scan_pending(config).entries


def upload_pending(
    config: Config,
    device_id: str,
    ledger: Ledger,
    state_logger: StateLogger,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> UploadBatch:
    """Attempt every queued file once. Never raises.

    One failed file does not stop the ones after it; each keeps its `pending`
    status and is retried on a later tick.
    """
    batch = UploadBatch()
    logger = state_logger.logger

    try:
        scan = scan_pending(config)
    except OSError as exc:
        state_logger.record(
            CATEGORY_QUEUE_READ_FAILED,
            f"cannot read the upload queue {config.pending_dir}: {exc}",
            level=logging.ERROR,
        )
        return batch

    for name in scan.skipped:
        # Left in place rather than deleted, because destroying data is worse
        # than a repeated log line.
        state_logger.record(
            CATEGORY_UPLOAD_PREFIX + "malformed_queue_entry",
            f"skipping queue entry {name!r}: not a pending/<card_uuid>/<filename> file",
            level=logging.ERROR,
        )
        batch.rejected += 1

    for card_uuid, name in scan.entries:
        label = f"{card_uuid}/{name}"

        if not is_safe_card_uuid(card_uuid) or not is_safe_filename(name):
            # Cannot have come from the watcher; both path segments are
            # validated before either is joined to a path or sent.
            state_logger.record(
                CATEGORY_UPLOAD_PREFIX + "unsafe_queue_entry",
                f"skipping queued file with an unsafe path {label!r}",
                level=logging.ERROR,
            )
            batch.rejected += 1
            continue

        path = config.pending_dir_for(card_uuid) / name
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            # Already delivered and cleaned up by an earlier tick.
            continue
        except OSError as exc:
            state_logger.record(
                CATEGORY_UPLOAD_PREFIX + "read_failed",
                f"cannot read queued file {label!r}: {exc}",
                level=logging.ERROR,
            )
            batch.failed += 1
            continue

        batch.attempted += 1
        try:
            acknowledgement = send_upload(
                config, device_id, card_uuid, name, content, opener=opener
            )
        except UploadError as exc:
            state_logger.record(
                CATEGORY_UPLOAD_PREFIX + exc.category,
                f"upload of {label!r} failed [{exc.category}]: {exc}",
                level=logging.ERROR,
            )
            if exc.category in ("unsafe_filename", "unsafe_card_uuid", "too_large"):
                batch.rejected += 1
            else:
                batch.failed += 1
            continue
        except Exception as exc:
            state_logger.record(
                CATEGORY_UPLOAD_PREFIX + "unexpected_error",
                f"unexpected error uploading {label!r}: {exc!r}",
                level=logging.ERROR,
            )
            batch.failed += 1
            continue

        # Ledger first, then the file: a crash in between leaves an extra
        # queued copy that is re-sent and answered with `already_stored`,
        # whereas the reverse order could lose the record of a delivered file.
        ledger.mark_uploaded(card_uuid, name)
        try:
            path.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("uploaded %r but could not remove the queued copy: %s", label, exc)
        else:
            _remove_empty_card_dir(path.parent)

        if acknowledgement.get("status") == "already_stored":
            batch.already_stored += 1
        else:
            batch.uploaded += 1
        state_logger.record(
            CATEGORY_UPLOAD_PREFIX + "stored",
            "upload {}: card_uuid={} filename={} size={} received_at={}".format(
                acknowledgement.get("status"),
                card_uuid,
                name,
                acknowledgement.get("size"),
                acknowledgement.get("received_at"),
            ),
            level=logging.INFO,
            always=True,
        )

    return batch


def _remove_empty_card_dir(directory: Path) -> None:
    """Drop a per-card queue directory once its last file is delivered.

    Never forced: a non-empty directory raises OSError and is left alone. The
    watcher re-creates the directory immediately before it publishes a copy, so
    removing it here cannot lose a file.
    """
    try:
        directory.rmdir()
    except OSError:
        pass


def send_heartbeat(
    config: Config,
    device_id: str,
    state_logger: StateLogger,
    opener: Optional[urllib.request.OpenerDirector] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> bool:
    """Attempt one `POST /ping`. Never raises. Returns whether it was acknowledged."""
    sent_at = rfc3339_utc(now())
    try:
        acknowledgement = send_ping(config, device_id, sent_at, opener=opener)
    except PingError as exc:
        state_logger.record(
            exc.category,
            f"ping failed [{exc.category}]: {exc}",
            level=logging.ERROR,
        )
        return False
    except Exception as exc:
        state_logger.record(
            "unexpected_error",
            f"unexpected error sending ping: {exc!r}",
            level=logging.ERROR,
        )
        return False

    state_logger.record(
        CATEGORY_ACKNOWLEDGED,
        "ping acknowledged: device_id={} sent_at={} received_at={}".format(
            acknowledgement.get("device_id"), sent_at, acknowledgement.get("received_at")
        ),
        level=logging.INFO,
        always=True,
    )
    return True


def poll_once(
    config: Config,
    device_id: str,
    state_logger: StateLogger,
    ledger: Ledger,
    ping_timer: IntervalTimer,
    opener: Optional[urllib.request.OpenerDirector] = None,
    wifi_check: Callable[[str], bool] = is_wifi_connected,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Run exactly one poll tick. Never raises.

    While offline, no network request is attempted at all — neither heartbeat
    nor upload.
    """
    try:
        connected = wifi_check(config.wifi_interface)
    except WifiCheckError as exc:
        state_logger.record(CATEGORY_WIFI_CHECK_FAILED, str(exc), level=logging.ERROR)
        return
    except Exception as exc:  # pragma: no cover - the loop must survive anything
        state_logger.record(
            CATEGORY_WIFI_CHECK_FAILED,
            f"unexpected error checking WiFi state: {exc!r}",
            level=logging.ERROR,
        )
        return

    if not connected:
        state_logger.record(
            CATEGORY_DISCONNECTED,
            f"WiFi interface {config.wifi_interface} is not connected; "
            "skipping ping and uploads",
            level=logging.WARNING,
        )
        return

    if ping_timer.due():
        # Recorded whether or not it succeeded, so an unreachable server is
        # retried on the heartbeat interval and not on every upload tick.
        ping_timer.record()
        send_heartbeat(config, device_id, state_logger, opener=opener, now=now)

    # Upload work never waits for the next heartbeat.
    upload_pending(config, device_id, ledger, state_logger, opener=opener)


def run(
    config: Config,
    device_id: str,
    state_logger: StateLogger,
    ledger: Ledger,
    stop_event: threading.Event,
    opener: Optional[urllib.request.OpenerDirector] = None,
    wifi_check: Callable[[str], bool] = is_wifi_connected,
    max_iterations: Optional[int] = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Poll on a fixed cadence until stopped.

    The loop is single-threaded and strictly sequential, so a slow request can
    never overlap the next tick; it only delays it.
    """
    iterations = 0
    next_tick = clock()
    ping_timer = IntervalTimer(config.ping_interval_seconds, clock=clock)

    while not stop_event.is_set():
        poll_once(
            config,
            device_id,
            state_logger,
            ledger,
            ping_timer,
            opener=opener,
            wifi_check=wifi_check,
        )
        iterations += 1

        if max_iterations is not None and iterations >= max_iterations:
            break

        next_tick += config.poll_interval_seconds
        delay = next_tick - clock()
        if delay <= 0:
            # A tick ran long; resynchronize instead of firing back-to-back.
            next_tick = clock()
            delay = 0
        if stop_event.wait(delay):
            break

    return iterations


def build_logger(
    name: str, log_path: Path, max_bytes: int, backup_count: int
) -> logging.Logger:
    """Log to stdout (captured by the journal) and to a size-capped file."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # Missing log directory must not take the daemon down; setup.sh creates it.
        logger.error("File logging disabled, cannot write %s: %s", log_path, exc)

    return logger


def configure_logging(config: Config) -> logging.Logger:
    return build_logger(
        LOGGER_NAME, config.log_path, config.log_max_bytes, config.log_backup_count
    )


def main(argv: Optional[list] = None) -> int:
    try:
        config = Config.from_env()
        device_id = resolve_device_id()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config)
    logger.info(
        "uploader starting: device_id=%s server=%s interface=%s poll=%ss ping=%ss "
        "timeout=%ss upload_timeout=%ss queue=%s ledger=%s max_bytes=%s",
        device_id,
        config.server_url,
        config.wifi_interface,
        config.poll_interval_seconds,
        config.ping_interval_seconds,
        config.request_timeout_seconds,
        config.upload_timeout_seconds,
        config.queue_path,
        config.state_db_path,
        config.max_upload_bytes,
    )

    ledger = Ledger(config.state_db_path)
    try:
        # The watcher normally creates these; the uploader may start first.
        config.pending_dir.mkdir(parents=True, exist_ok=True)
        ledger.initialize()
    except OSError as exc:
        logger.error("cannot prepare the queue or ledger: %s", exc)
        return 1
    logger.info(
        "ledger holds %s; %d file(s) queued",
        summarize(ledger.counts()),
        len(queued_entries(config)),
    )

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    state_logger = StateLogger(logger, config.error_log_repeat_seconds)
    run(config, device_id, state_logger, ledger, stop_event)
    logger.info("uploader stopped")
    return 0


def _categorize_http_error(exc: urllib.error.HTTPError) -> tuple:
    code = exc.code
    if 300 <= code < 400:
        return (
            "redirect_rejected",
            f"server responded with redirect HTTP {code}; refusing to follow it",
        )
    if code == 401:
        return "auth_rejected", "server rejected the API key (HTTP 401)"
    if code == 413:
        return "too_large", f"server rejected the file as too large (HTTP {code})"
    if code in (400, 422):
        return "request_rejected", f"server rejected the request body (HTTP {code})"
    if 500 <= code < 600:
        return "server_error", f"server error (HTTP {code})"
    return f"unexpected_status_{code}", f"server returned HTTP {code}"


def _categorize_url_error(exc: urllib.error.URLError) -> tuple:
    reason = exc.reason
    if isinstance(reason, ssl.SSLError):
        return "tls_error", f"TLS failure: {reason}"
    if isinstance(reason, socket.gaierror):
        return "dns_error", f"DNS resolution failed: {reason}"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout", f"request timed out: {reason}"
    return "connection_error", f"connection failed: {reason}"


if __name__ == "__main__":
    sys.exit(main())
