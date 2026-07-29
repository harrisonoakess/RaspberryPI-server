#!/usr/bin/env python3
"""Phase 1 connectivity daemon for the Raspberry Pi.

Runs one non-overlapping poll loop. Every tick it asks the OS whether the
configured WiFi interface is associated with an access point. If it is, it
sends an authenticated HTTPS POST /ping; if it is not, it makes no network
request at all. Errors never stop the loop, and repeated identical states are
rate-limited so an offline Pi cannot flood the journal.

Standard library only, so the Pi needs no package installation.
See prd/phase-1-connection.md.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

LOGGER_NAME = "piuploader"

# Must match the server's device_id validation in server/main.py.
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

DEFAULT_LOG_PATH = Path("/var/log/piuploader/connectivity.log")
DEFAULT_LOG_MAX_BYTES = 1_048_576
DEFAULT_LOG_BACKUP_COUNT = 5

# Log categories. State changes are logged immediately; an unchanged category
# repeats no more than once per ERROR_LOG_REPEAT_SECONDS.
CATEGORY_ACKNOWLEDGED = "acknowledged"
CATEGORY_DISCONNECTED = "wifi_disconnected"
CATEGORY_WIFI_CHECK_FAILED = "wifi_check_failed"


class ConfigError(Exception):
    """Raised when the daemon cannot start with the supplied configuration."""


class WifiCheckError(Exception):
    """Raised when the WiFi association state cannot be determined."""


class PingError(Exception):
    """A ping attempt failed. `category` groups identical failures for logging."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Config:
    server_url: str
    api_key: str
    wifi_interface: str = "wlan0"
    poll_interval_seconds: float = 30.0
    request_timeout_seconds: float = 10.0
    error_log_repeat_seconds: float = 300.0
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    @property
    def ping_url(self) -> str:
        return self.server_url.rstrip("/") + "/ping"

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
            poll_interval_seconds=_positive_float(env, "POLL_INTERVAL_SECONDS", 30.0),
            request_timeout_seconds=_positive_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0),
            error_log_repeat_seconds=_positive_float(env, "ERROR_LOG_REPEAT_SECONDS", 300.0),
            log_path=Path(env.get("LOG_PATH", "").strip() or DEFAULT_LOG_PATH),
            log_max_bytes=int(_positive_float(env, "LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)),
            log_backup_count=int(_positive_float(env, "LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)),
        )


def _positive_float(env: dict, name: str, default: float) -> float:
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

    Phase 1 is one Pi, so provisioning must give it a hostname the server will
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

    try:
        with opener.open(request, timeout=config.request_timeout_seconds) as response:
            status_code = response.status
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise _categorize_http_error(exc) from exc
    except urllib.error.URLError as exc:
        raise _categorize_url_error(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PingError("timeout", f"request timed out: {exc}") from exc
    except ssl.SSLError as exc:
        raise PingError("tls_error", f"TLS failure: {exc}") from exc
    except OSError as exc:
        raise PingError("connection_error", f"connection failed: {exc}") from exc

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


def _categorize_http_error(exc: urllib.error.HTTPError) -> PingError:
    code = exc.code
    if 300 <= code < 400:
        return PingError(
            "redirect_rejected",
            f"server responded with redirect HTTP {code}; refusing to follow it",
        )
    if code == 401:
        return PingError("auth_rejected", "server rejected the API key (HTTP 401)")
    if code in (400, 422):
        return PingError("request_rejected", f"server rejected the request body (HTTP {code})")
    if 500 <= code < 600:
        return PingError("server_error", f"server error (HTTP {code})")
    return PingError(f"unexpected_status_{code}", f"server returned HTTP {code}")


def _categorize_url_error(exc: urllib.error.URLError) -> PingError:
    reason = exc.reason
    if isinstance(reason, ssl.SSLError):
        return PingError("tls_error", f"TLS failure: {reason}")
    if isinstance(reason, socket.gaierror):
        return PingError("dns_error", f"DNS resolution failed: {reason}")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return PingError("timeout", f"request timed out: {reason}")
    return PingError("connection_error", f"connection failed: {reason}")


def poll_once(
    config: Config,
    device_id: str,
    state_logger: StateLogger,
    opener: Optional[urllib.request.OpenerDirector] = None,
    wifi_check: Callable[[str], bool] = is_wifi_connected,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Run exactly one poll tick. Never raises."""
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
            f"WiFi interface {config.wifi_interface} is not connected; skipping ping",
            level=logging.WARNING,
        )
        return

    sent_at = rfc3339_utc(now())
    try:
        acknowledgement = send_ping(config, device_id, sent_at, opener=opener)
    except PingError as exc:
        state_logger.record(
            exc.category,
            f"ping failed [{exc.category}]: {exc}",
            level=logging.ERROR,
        )
        return
    except Exception as exc:  # pragma: no cover - the loop must survive anything
        state_logger.record(
            "unexpected_error",
            f"unexpected error sending ping: {exc!r}",
            level=logging.ERROR,
        )
        return

    state_logger.record(
        CATEGORY_ACKNOWLEDGED,
        "ping acknowledged: device_id={} sent_at={} received_at={}".format(
            acknowledgement.get("device_id"), sent_at, acknowledgement.get("received_at")
        ),
        level=logging.INFO,
        always=True,
    )


def run(
    config: Config,
    device_id: str,
    state_logger: StateLogger,
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

    while not stop_event.is_set():
        poll_once(config, device_id, state_logger, opener=opener, wifi_check=wifi_check)
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


def configure_logging(config: Config) -> logging.Logger:
    """Log to stdout (captured by the journal) and to a size-capped file."""
    logger = logging.getLogger(LOGGER_NAME)
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
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_path,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # Missing log directory must not take the daemon down; setup.sh creates it.
        logger.error("File logging disabled, cannot write %s: %s", config.log_path, exc)

    return logger


def main(argv: Optional[list] = None) -> int:
    try:
        config = Config.from_env()
        device_id = resolve_device_id()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config)
    logger.info(
        "connectivity daemon starting: device_id=%s server=%s interface=%s "
        "poll=%ss timeout=%ss error_repeat=%ss",
        device_id,
        config.server_url,
        config.wifi_interface,
        config.poll_interval_seconds,
        config.request_timeout_seconds,
        config.error_log_repeat_seconds,
    )

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    state_logger = StateLogger(logger, config.error_log_repeat_seconds)
    run(config, device_id, state_logger, stop_event)
    logger.info("connectivity daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
