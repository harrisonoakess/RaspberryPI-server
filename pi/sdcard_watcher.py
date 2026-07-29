#!/usr/bin/env python3
"""Copies new CSV files off the configured SD card into the upload queue.

The card is never cleared, so every insertion presents the full history of
files the logger has ever written. This service copies only the ones the Pi
does not already know about, and it never writes to the card.

The card is mounted read-only by root (a udev rule installed by `setup.sh`
runs `systemd-mount`), so this service needs no privileges: it only reads the
mountpoint and writes to its own queue, ledger, and log.

Standard library only, except for the optional `pyudev` fast path — without it
the service falls back to polling for the card on the same interval.
See prd/phase-2-data-sync.md.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from state import Ledger, rfc3339_utc, summarize

# `uploader` owns the client side of the HTTP contract, including the filename
# rules the server enforces, so the watcher reuses them rather than keeping a
# second copy inside pi/.
from uploader import (
    ConfigError,
    StateLogger,
    build_logger,
    is_safe_filename,
    positive_float,
)

LOGGER_NAME = "piuploader.watcher"

DEFAULT_MAX_UPLOAD_BYTES = 10_485_760  # 10 MiB, matched on the server.
DEFAULT_QUEUE_PATH = Path("/var/lib/piuploader/queue")
DEFAULT_STATE_DB_PATH = Path("/var/lib/piuploader/state.db")
DEFAULT_LOG_PATH = Path("/var/log/piuploader/sdcard-watcher.log")
DEFAULT_BY_UUID_ROOT = Path("/dev/disk/by-uuid")

COPY_CHUNK_BYTES = 262_144

CATEGORY_CARD_ABSENT = "card_absent"
CATEGORY_CARD_NOT_MOUNTED = "card_not_mounted"
CATEGORY_WRONG_CARD = "wrong_card"
CATEGORY_SCAN_FAILED = "scan_failed"


class CardError(Exception):
    """The configured card could not be read."""


class FileTooLarge(Exception):
    """A card file exceeded MAX_UPLOAD_BYTES."""


@dataclass(frozen=True)
class WatcherConfig:
    card_uuid: str
    card_mountpoint: Path
    card_filesystem: str = ""
    queue_path: Path = DEFAULT_QUEUE_PATH
    state_db_path: Path = DEFAULT_STATE_DB_PATH
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    scan_interval_seconds: float = 5.0
    error_log_repeat_seconds: float = 300.0
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 5
    by_uuid_root: Path = DEFAULT_BY_UUID_ROOT

    @property
    def pending_dir(self) -> Path:
        return self.queue_path / "pending"

    @property
    def temp_dir(self) -> Path:
        """Staging area for in-progress copies.

        It sits on the same filesystem as `pending_dir` so publishing a
        finished copy is an atomic rename, never a second copy.
        """
        return self.queue_path / "tmp"

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "WatcherConfig":
        env = os.environ if env is None else env

        card_uuid = env.get("CARD_UUID", "").strip()
        if not card_uuid:
            raise ConfigError(
                "CARD_UUID is required; re-run setup.sh with the card inserted to detect it"
            )

        mountpoint = env.get("CARD_MOUNTPOINT", "").strip()
        if not mountpoint:
            raise ConfigError("CARD_MOUNTPOINT is required")

        max_upload_bytes = int(
            positive_float(env, "MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        )

        return WatcherConfig(
            card_uuid=card_uuid,
            card_mountpoint=Path(mountpoint),
            card_filesystem=env.get("CARD_FILESYSTEM", "").strip(),
            queue_path=Path(env.get("QUEUE_PATH", "").strip() or DEFAULT_QUEUE_PATH),
            state_db_path=Path(
                env.get("STATE_DB_PATH", "").strip() or DEFAULT_STATE_DB_PATH
            ),
            max_upload_bytes=max_upload_bytes,
            scan_interval_seconds=positive_float(env, "CARD_SCAN_INTERVAL_SECONDS", 5.0),
            error_log_repeat_seconds=positive_float(env, "ERROR_LOG_REPEAT_SECONDS", 300.0),
            log_path=Path(env.get("WATCHER_LOG_PATH", "").strip() or DEFAULT_LOG_PATH),
            log_max_bytes=int(positive_float(env, "LOG_MAX_BYTES", 1_048_576)),
            log_backup_count=int(positive_float(env, "LOG_BACKUP_COUNT", 5)),
        )


def card_device(config: WatcherConfig) -> Optional[Path]:
    """The block device for `CARD_UUID`, or None when the card is not inserted.

    `/dev/disk/by-uuid` is maintained by udev and readable without privileges,
    so presence can be checked without running `blkid` as root.
    """
    link = config.by_uuid_root / config.card_uuid
    try:
        if not link.exists():
            return None
        return link.resolve()
    except OSError:
        return None


def card_is_mounted(config: WatcherConfig, device: Path) -> bool:
    """Whether `CARD_MOUNTPOINT` currently holds the filesystem on `device`.

    Compares the mountpoint's device number with the block device's, which
    proves the configured card — and not some other removable drive that
    happened to be mounted there — is what will be scanned.
    """
    try:
        mount_stat = os.stat(config.card_mountpoint)
        device_stat = os.stat(device)
    except OSError:
        return False
    return mount_stat.st_dev == device_stat.st_rdev


def in_scope_files(mountpoint: Path) -> list:
    """Root-level regular `.csv` files on the card, sorted by name.

    Directories, symlinks, and dotfile metadata are out of scope, and nested
    directories are not descended into.
    """
    try:
        entries = list(os.scandir(mountpoint))
    except OSError as exc:
        raise CardError(f"cannot list {mountpoint}: {exc}") from exc

    candidates = []
    for entry in entries:
        try:
            # follow_symlinks=False: a symlink on the card must never be
            # followed off the read-only mount.
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not entry.name.lower().endswith(".csv"):
            continue
        candidates.append(entry.name)
    return sorted(candidates)


def copy_into_queue(source: Path, config: WatcherConfig) -> int:
    """Copy one card file into `queue/pending/`. Returns the byte count.

    The copy is written to `queue/tmp/` and only renamed into `pending/` once
    it is complete and fsynced, so an interrupted copy is never published. The
    card file is opened read-only and left untouched.
    """
    handle, temp_name = tempfile.mkstemp(dir=config.temp_dir, prefix=".copy-", suffix=".part")
    temp_path = Path(temp_name)
    copied = 0
    try:
        with os.fdopen(handle, "wb") as destination, source.open("rb") as origin:
            while True:
                chunk = origin.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > config.max_upload_bytes:
                    raise FileTooLarge(
                        f"{source.name} exceeds {config.max_upload_bytes} bytes"
                    )
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp_path, config.pending_dir / source.name)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return copied


@dataclass
class IngestResult:
    copied: int = 0
    bytes_copied: int = 0
    skipped_known: int = 0
    rejected: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"copied={self.copied} bytes={self.bytes_copied} "
            f"skipped_known={self.skipped_known} rejected={self.rejected} "
            f"failed={self.failed}"
        )


def ingest_card(
    config: WatcherConfig,
    ledger: Ledger,
    logger: logging.Logger,
    now: Callable[[], str] = rfc3339_utc,
) -> IngestResult:
    """Copy every new in-scope file from the card into the queue.

    A rejected or failed file is logged and skipped; later files are still
    processed. Nothing on the card is modified or deleted.
    """
    result = IngestResult()
    names = in_scope_files(config.card_mountpoint)
    known = ledger.known_filenames()
    logger.info("scanning %s: %d in-scope csv file(s)", config.card_mountpoint, len(names))

    for name in names:
        if not is_safe_filename(name):
            # Logged as a repr so a deliberately confusing name cannot forge
            # log lines.
            logger.error("rejecting unsafe or out-of-scope filename %r", name)
            result.rejected += 1
            continue
        if name in known:
            result.skipped_known += 1
            continue

        source = config.card_mountpoint / name
        try:
            size = source.stat().st_size
        except OSError as exc:
            logger.error("cannot stat %r: %s", name, exc)
            result.failed += 1
            continue
        if size > config.max_upload_bytes:
            logger.error(
                "rejecting %r: %d bytes exceeds the %d byte limit",
                name,
                size,
                config.max_upload_bytes,
            )
            result.rejected += 1
            continue

        try:
            copied = copy_into_queue(source, config)
        except FileTooLarge as exc:
            logger.error("rejecting %r: %s", name, exc)
            result.rejected += 1
            continue
        except OSError as exc:
            # Hot-unplug mid-copy lands here: no partial file is published and
            # the ledger stays clean, so the next insertion retries.
            logger.error("copy of %r failed, leaving it unqueued: %s", name, exc)
            result.failed += 1
            continue

        # Recorded after the rename so the ledger never claims a file the queue
        # does not have. A crash in between leaves a queued file with no row;
        # the uploader still delivers it and marks it uploaded.
        ledger.record_pending(name, now())
        result.copied += 1
        result.bytes_copied += copied
        logger.info("queued %r (%d bytes)", name, copied)

    return result


def prepare_directories(config: WatcherConfig) -> None:
    for directory in (config.pending_dir, config.temp_dir):
        directory.mkdir(parents=True, exist_ok=True)


def clear_stale_temp_files(config: WatcherConfig, logger: logging.Logger) -> int:
    """Remove copies interrupted by an earlier crash.

    Anything left in `tmp/` was never published to `pending/`, so it is not
    tracked anywhere and is safe to delete.
    """
    removed = 0
    try:
        entries = list(os.scandir(config.temp_dir))
    except OSError:
        return 0
    for entry in entries:
        try:
            os.unlink(entry.path)
            removed += 1
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not remove stale copy %s: %s", entry.path, exc)
    if removed:
        logger.info("removed %d incomplete copy file(s) from %s", removed, config.temp_dir)
    return removed


def open_udev_monitor(logger: logging.Logger):
    """A started pyudev block-device monitor, or None if pyudev is unavailable.

    The monitor only shortens the latency between inserting the card and
    scanning it. Presence is always confirmed by looking at the filesystem, so
    losing this fast path costs latency, not correctness.
    """
    try:
        import pyudev
    except ImportError:
        logger.warning(
            "pyudev is not installed; falling back to periodic polling for the card"
        )
        return None

    try:
        monitor = pyudev.Monitor.from_netlink(pyudev.Context())
        monitor.filter_by(subsystem="block")
        monitor.start()
        return monitor
    except Exception as exc:  # pragma: no cover - depends on the host's udev
        logger.warning("could not start the udev monitor, polling instead: %s", exc)
        return None


def wait_for_change(
    monitor,
    stop_event: threading.Event,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until a block-device event, `timeout`, or a stop request.

    Polls in short slices so SIGTERM is honoured promptly even while waiting.
    """
    if monitor is None:
        stop_event.wait(timeout)
        return

    deadline = clock() + timeout
    while not stop_event.is_set():
        remaining = deadline - clock()
        if remaining <= 0:
            return
        try:
            device = monitor.poll(timeout=min(remaining, 1.0))
        except Exception:  # pragma: no cover - the loop must survive anything
            stop_event.wait(min(remaining, 1.0))
            return
        if device is not None:
            return


def check_once(
    config: WatcherConfig,
    ledger: Ledger,
    logger: logging.Logger,
    state_logger: StateLogger,
    card_was_present: bool,
) -> bool:
    """Ingest on a transition to "card present". Returns the new presence state.

    Never raises: a failure here must not take the service down, because the
    next insertion has to be picked up.
    """
    device = card_device(config)
    if device is None:
        state_logger.record(
            CATEGORY_CARD_ABSENT,
            f"card {config.card_uuid} is not inserted",
            level=logging.INFO,
        )
        return False

    if not card_is_mounted(config, device):
        # setup.sh's udev rule mounts the card a moment after the device
        # appears, so this is normal for the first tick after insertion.
        state_logger.record(
            CATEGORY_CARD_NOT_MOUNTED,
            f"card {config.card_uuid} ({device}) is not mounted at "
            f"{config.card_mountpoint} yet",
            level=logging.WARNING,
        )
        return False

    if card_was_present:
        return True

    try:
        result = ingest_card(config, ledger, logger)
    except CardError as exc:
        state_logger.record(CATEGORY_SCAN_FAILED, str(exc), level=logging.ERROR)
        return False
    except Exception as exc:
        # Returns True so a persistent, unexplained failure does not re-scan on
        # every tick; removing and reinserting the card retries it.
        state_logger.record(
            CATEGORY_SCAN_FAILED,
            f"unexpected error ingesting the card: {exc!r}",
            level=logging.ERROR,
        )
        return True

    logger.info("card ingest complete: %s", result)
    logger.info("ledger now holds %s", summarize(ledger.counts()))
    return True


def run(
    config: WatcherConfig,
    ledger: Ledger,
    logger: logging.Logger,
    state_logger: StateLogger,
    stop_event: threading.Event,
    monitor=None,
    max_iterations: Optional[int] = None,
) -> int:
    """Watch for the card until stopped. One sequential loop, no overlap."""
    iterations = 0
    card_present = False

    while not stop_event.is_set():
        card_present = check_once(config, ledger, logger, state_logger, card_present)
        iterations += 1

        if max_iterations is not None and iterations >= max_iterations:
            break
        wait_for_change(monitor, stop_event, config.scan_interval_seconds)

    return iterations


def main(argv: Optional[list] = None) -> int:
    try:
        config = WatcherConfig.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logger = build_logger(
        LOGGER_NAME, config.log_path, config.log_max_bytes, config.log_backup_count
    )
    logger.info(
        "sdcard watcher starting: uuid=%s filesystem=%s mountpoint=%s queue=%s "
        "ledger=%s max_bytes=%s scan=%ss",
        config.card_uuid,
        config.card_filesystem or "unrecorded",
        config.card_mountpoint,
        config.queue_path,
        config.state_db_path,
        config.max_upload_bytes,
        config.scan_interval_seconds,
    )

    ledger = Ledger(config.state_db_path)
    try:
        prepare_directories(config)
        ledger.initialize()
    except OSError as exc:
        logger.error("cannot prepare the queue or ledger: %s", exc)
        return 1
    clear_stale_temp_files(config, logger)
    logger.info("ledger holds %s", summarize(ledger.counts()))

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    state_logger = StateLogger(logger, config.error_log_repeat_seconds)
    monitor = open_udev_monitor(logger)
    run(config, ledger, logger, state_logger, stop_event, monitor=monitor)
    logger.info("sdcard watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
