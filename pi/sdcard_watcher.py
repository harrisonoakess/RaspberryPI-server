#!/usr/bin/env python3
"""Copies new CSV files off whatever SD card is currently mounted.

A card is never cleared, so every insertion presents the full history of files
the logger has ever written. This service copies only the ones the Pi does not
already know about *for that card*, and it never writes to the card.

Phase 3 removes the single pinned card. The card identity is derived live, on
every tick, from whatever filesystem is mounted at `CARD_MOUNTPOINT`: its
filesystem UUID becomes the namespace for both the ledger and the queue
directory, so two different cards carrying the same filename stay distinct.

The card is mounted read-only by `sdcard_mounter.py`, which runs as root, so
this service needs no privileges: it only reads the mountpoint and writes to its
own queue, ledger, and log.

Standard library only, except for the optional `pyudev` fast path — without it
the service falls back to polling for the card on the same interval.
See prd/phase-3-multi-card-ingestion.md.
"""

from __future__ import annotations

import logging
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from state import Ledger, rfc3339_utc, summarize

# `uploader` owns the client side of the HTTP contract, including the filename
# and card_uuid rules the server enforces, so the watcher reuses them rather
# than keeping a second copy inside pi/.
from uploader import (
    ConfigError,
    StateLogger,
    build_logger,
    is_safe_card_uuid,
    is_safe_filename,
    positive_float,
)

LOGGER_NAME = "piuploader.watcher"

DEFAULT_MAX_UPLOAD_BYTES = 20_971_520  # 20 MiB, matched on the server.
DEFAULT_QUEUE_PATH = Path("/var/lib/piuploader/queue")
DEFAULT_STATE_DB_PATH = Path("/var/lib/piuploader/state.db")
DEFAULT_LOG_PATH = Path("/var/log/piuploader/sdcard-watcher.log")
DEFAULT_BY_UUID_ROOT = Path("/dev/disk/by-uuid")

COPY_CHUNK_BYTES = 262_144

CATEGORY_CARD_ABSENT = "card_absent"
CATEGORY_CARD_NOT_MOUNTED = "card_not_mounted"
CATEGORY_SCAN_FAILED = "scan_failed"


def resolve_card_scan_subdir(env: dict) -> str:
    """Read and validate `CARD_SCAN_SUBDIR`, or "" for the card root.

    A whitelist of shapes, like the filename rules: the value either passes as
    given or is rejected, because it is joined to the mountpoint before any
    file is read. An absolute path or a `..` component would escape the
    read-only mount, so both are refused outright rather than normalized away.
    """
    raw = env.get("CARD_SCAN_SUBDIR", "").strip().strip("/")
    if not raw:
        return ""
    if "\\" in raw:
        raise ConfigError("CARD_SCAN_SUBDIR must use / as its separator")
    if any(character == "\x00" or unicodedata.category(character) == "Cc" for character in raw):
        raise ConfigError("CARD_SCAN_SUBDIR must not contain NUL or control characters")
    segments = [segment for segment in raw.split("/") if segment]
    if not segments or any(segment in (".", "..") for segment in segments):
        raise ConfigError(
            f"CARD_SCAN_SUBDIR must be a relative path inside the card, got {raw!r}"
        )
    return "/".join(segments)


class CardError(Exception):
    """The mounted card could not be read."""


class CardChanged(Exception):
    """The mounted card changed while a file was being copied."""


class FileTooLarge(Exception):
    """A card file exceeded MAX_UPLOAD_BYTES."""


@dataclass(frozen=True)
class CardIdentity:
    """The card currently mounted: its filesystem UUID and device number.

    Both halves are compared before a copy is published. The UUID alone would
    not notice a swap to a cloned filesystem, and the device number alone is
    reused by the kernel across insertions.
    """

    uuid: str
    device_number: int


@dataclass(frozen=True)
class WatcherConfig:
    card_mountpoint: Path
    queue_path: Path = DEFAULT_QUEUE_PATH
    state_db_path: Path = DEFAULT_STATE_DB_PATH
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    scan_interval_seconds: float = 5.0
    error_log_repeat_seconds: float = 300.0
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 5
    by_uuid_root: Path = DEFAULT_BY_UUID_ROOT
    # Subdirectory of the card holding the CSVs, "" for the card root. Garmin
    # avionics cards keep flight logs under `data_log/` rather than at the root,
    # and the card is read-only media that cannot be rearranged to suit us.
    card_scan_subdir: str = ""

    @property
    def scan_root(self) -> Path:
        """Directory scanned for CSVs: the mountpoint, or a subdirectory of it.

        Only this path is scanned — the search is still one level deep, so a
        card whose logs sit two directories down needs the full relative path
        in `CARD_SCAN_SUBDIR`.
        """
        if not self.card_scan_subdir:
            return self.card_mountpoint
        return self.card_mountpoint / self.card_scan_subdir

    @property
    def pending_dir(self) -> Path:
        return self.queue_path / "pending"

    def pending_dir_for(self, card_uuid: str) -> Path:
        """Per-card queue directory. `card_uuid` is validated before this call.

        Without the card segment, two cards' same-named files would overwrite
        each other in the queue even though the ledger treats them as distinct.
        """
        return self.pending_dir / card_uuid

    @property
    def temp_dir(self) -> Path:
        """Staging area for in-progress copies.

        It sits on the same filesystem as `pending_dir` so publishing a
        finished copy is an atomic rename, never a second copy. It stays flat:
        `tempfile.mkstemp` already generates names independent of the source.
        """
        return self.queue_path / "tmp"

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "WatcherConfig":
        env = os.environ if env is None else env

        mountpoint = env.get("CARD_MOUNTPOINT", "").strip()
        if not mountpoint:
            raise ConfigError("CARD_MOUNTPOINT is required")

        max_upload_bytes = int(
            positive_float(env, "MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        )

        return WatcherConfig(
            card_mountpoint=Path(mountpoint),
            card_scan_subdir=resolve_card_scan_subdir(env),
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


def is_mount_boundary(mountpoint: Path) -> bool:
    """Whether `mountpoint` really holds a filesystem of its own.

    Without this guard, "nothing is mounted" would fall through to matching the
    root filesystem's own UUID and the watcher would scan the Pi's boot disk.
    """
    try:
        mount_stat = os.stat(mountpoint)
        parent_stat = os.stat(Path(mountpoint).parent)
    except OSError:
        return False
    return mount_stat.st_dev != parent_stat.st_dev


def discover_card(config: WatcherConfig) -> Optional[CardIdentity]:
    """The `(uuid, device_number)` mounted at `CARD_MOUNTPOINT`, or None.

    Generalizes Phase 2's single-UUID check: instead of asking whether one
    configured UUID is mounted here, it asks which UUID is. `/dev/disk/by-uuid`
    is maintained by udev and readable without privileges, so this needs no
    root and no `blkid` call.
    """
    if not is_mount_boundary(config.card_mountpoint):
        return None

    try:
        device_number = os.stat(config.card_mountpoint).st_dev
        names = os.listdir(config.by_uuid_root)
    except OSError:
        return None

    for name in sorted(names):
        # An unsafe UUID is not usable as a queue path segment or in the upload
        # payload, so such a card is treated as absent rather than sanitized.
        if not is_safe_card_uuid(name):
            continue
        try:
            device_stat = os.stat(config.by_uuid_root / name)
        except OSError:
            continue
        if not stat.S_ISBLK(device_stat.st_mode):
            continue
        if device_stat.st_rdev == device_number:
            return CardIdentity(uuid=name, device_number=device_number)
    return None


def in_scope_files(scan_root: Path) -> list:
    """Regular `.csv` files directly in `scan_root`, sorted by name.

    Directories, symlinks, and dotfile metadata are out of scope, and nested
    directories are not descended into.

    The dotfile rule is not cosmetic. A card that has been read on a Mac comes
    back carrying AppleDouble sidecars — `._log_1.csv` beside `log_1.csv` —
    which are resource-fork metadata, not flight data, yet match every other
    rule here. Uploading them would put junk rows in the ledger under names
    that look legitimate.
    """
    try:
        entries = list(os.scandir(scan_root))
    except OSError as exc:
        raise CardError(f"cannot list {scan_root}: {exc}") from exc

    candidates = []
    for entry in entries:
        try:
            # follow_symlinks=False: a symlink on the card must never be
            # followed off the read-only mount.
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if entry.name.startswith("."):
            continue
        if not entry.name.lower().endswith(".csv"):
            continue
        candidates.append(entry.name)
    return sorted(candidates)


def copy_into_queue(source: Path, config: WatcherConfig, card: CardIdentity) -> int:
    """Copy one card file into `queue/pending/<card_uuid>/`. Returns the bytes.

    The copy is written to `queue/tmp/` and only renamed into the per-card
    queue directory once it is complete, fsynced, and the mounted card has been
    confirmed to still be the one the bytes came from. An interrupted copy, or
    one that spanned a card swap, is never published. The card file is opened
    read-only and left untouched.
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

        # Re-resolved immediately before publishing: these bytes are only this
        # card's file if this card is still the one mounted.
        if discover_card(config) != card:
            raise CardChanged(
                f"card {card.uuid} was replaced while copying {source.name!r}"
            )

        # Created here, not earlier, so the window in which the uploader could
        # remove the emptied directory between mkdir and rename is negligible.
        destination_dir = config.pending_dir_for(card.uuid)
        destination_dir.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, destination_dir / source.name)
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
    aborted: bool = False

    def __str__(self) -> str:
        return (
            f"copied={self.copied} bytes={self.bytes_copied} "
            f"skipped_known={self.skipped_known} rejected={self.rejected} "
            f"failed={self.failed} aborted={self.aborted}"
        )


def ingest_card(
    config: WatcherConfig,
    ledger: Ledger,
    logger: logging.Logger,
    card: CardIdentity,
    now: Callable[[], str] = rfc3339_utc,
) -> IngestResult:
    """Copy every new in-scope file from the mounted card into its queue.

    A rejected or failed file is logged and skipped; later files are still
    processed. If the card changes mid-scan the pass stops without writing a
    ledger row for the file in flight, and the next tick starts over. Nothing
    on the card is modified or deleted.
    """
    result = IngestResult()
    names = in_scope_files(config.scan_root)
    known = ledger.known_filenames(card.uuid)
    logger.info(
        "scanning %s (card %s): %d in-scope csv file(s)",
        config.scan_root,
        card.uuid,
        len(names),
    )

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

        # Checked before each copy as well as after, so a swap is noticed even
        # if it happens between two files.
        if discover_card(config) != card:
            logger.warning("card %s is no longer mounted; stopping this scan", card.uuid)
            result.aborted = True
            break

        source = config.scan_root / name
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
            copied = copy_into_queue(source, config, card)
        except FileTooLarge as exc:
            logger.error("rejecting %r: %s", name, exc)
            result.rejected += 1
            continue
        except CardChanged as exc:
            # The temporary copy is already gone and no row was written, so the
            # file is simply retried whenever that card is next mounted.
            logger.warning("%s; nothing was queued for it", exc)
            result.aborted = True
            break
        except OSError as exc:
            # Hot-unplug mid-copy lands here: no partial file is published and
            # the ledger stays clean, so the next insertion retries.
            logger.error("copy of %r failed, leaving it unqueued: %s", name, exc)
            result.failed += 1
            continue

        # Recorded after the rename so the ledger never claims a file the queue
        # does not have. A crash in between leaves a queued file with no row;
        # the uploader still delivers it and marks it uploaded.
        ledger.record_pending(card.uuid, name, now())
        result.copied += 1
        result.bytes_copied += copied
        logger.info("queued %s/%r (%d bytes)", card.uuid, name, copied)

    return result


def prepare_directories(config: WatcherConfig) -> None:
    """Create the queue skeleton. Per-card directories are created on demand."""
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

    The monitor only shortens the latency between inserting a card and scanning
    it. Presence is always confirmed by looking at the filesystem, so losing
    this fast path costs latency, not correctness.
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
) -> bool:
    """Scan whatever card is mounted. Returns whether one was ingested.

    Every tick scans, rather than only the tick after an insertion: ledger
    dedup makes a repeat scan free at this volume, and it removes the need to
    track insertion transitions, which is what made Phase 2 depend on a single
    known card.

    Never raises: a failure here must not take the service down, because the
    next insertion has to be picked up.
    """
    card = discover_card(config)
    if card is None:
        if is_mount_boundary(config.card_mountpoint):
            state_logger.record(
                CATEGORY_CARD_NOT_MOUNTED,
                f"{config.card_mountpoint} is mounted but no safe card UUID "
                "resolves to it; not scanning",
                level=logging.WARNING,
            )
        else:
            state_logger.record(
                CATEGORY_CARD_ABSENT,
                f"no card is mounted at {config.card_mountpoint}",
                level=logging.INFO,
            )
        return False

    try:
        result = ingest_card(config, ledger, logger, card)
    except CardError as exc:
        state_logger.record(CATEGORY_SCAN_FAILED, str(exc), level=logging.ERROR)
        return False
    except Exception as exc:
        state_logger.record(
            CATEGORY_SCAN_FAILED,
            f"unexpected error ingesting card {card.uuid}: {exc!r}",
            level=logging.ERROR,
        )
        return False

    if result.copied or result.rejected or result.failed or result.aborted:
        logger.info("card %s ingest complete: %s", card.uuid, result)
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
    """Watch for a card until stopped. One sequential loop, no overlap."""
    iterations = 0

    while not stop_event.is_set():
        check_once(config, ledger, logger, state_logger)
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
        "sdcard watcher starting: mountpoint=%s queue=%s ledger=%s max_bytes=%s scan=%ss",
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
