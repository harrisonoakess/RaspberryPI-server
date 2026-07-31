#!/usr/bin/env python3
"""Mounts whichever qualifying SD card is currently present, read-only.

Phase 2 pinned one card's filesystem UUID into a udev rule at install time, so
accepting a different card meant an SSH session. This daemon replaces that with
a small polling reconciler: every tick it enumerates filesystem-bearing block
devices, applies a safety filter, and converges `CARD_MOUNTPOINT` on the single
eligible card — mounting it, unmounting a stale one, or leaving things alone.

Polling rather than udev events is deliberate: reconciling observed state has
no add/remove ordering or concurrency to get wrong, and it handles a card that
was already inserted at boot with no special case.

This is the only component that needs privileges. It runs as root because
mounting does; the watcher and uploader stay unprivileged and only read the
mountpoint. It never writes to a card, never formats one, and never changes a
card's UUID.

Standard library only. See prd/phase-3-multi-card-ingestion.md.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# `uploader` owns the shared runtime plumbing (config parsing, rate-limited
# logging) and the card_uuid rule the server enforces; all three Pi services
# are installed side by side in /opt/piuploader.
from uploader import (
    ConfigError,
    StateLogger,
    build_logger,
    is_safe_card_uuid,
    positive_float,
)

LOGGER_NAME = "piuploader.mounter"

DEFAULT_LOG_PATH = Path("/var/log/piuploader/sdcard-mounter.log")
DEFAULT_CARD_MOUNTPOINT = Path("/mnt/sdcard")

# Filesystems this daemon will mount. Anything else is declined rather than
# mounted with guessed options.
SUPPORTED_FILESYSTEMS = ("vfat", "exfat", "ext2", "ext3", "ext4")

# Journalling filesystems get `noload`, so mounting never replays a journal
# onto a card this daemon is only allowed to read.
JOURNAL_FILESYSTEMS = ("ext3", "ext4")

BASE_MOUNT_OPTIONS = ("ro", "nodev", "nosuid", "noexec")

COMMAND_TIMEOUT_SECONDS = 30

# `lsblk -P` emits `KEY="value"` pairs. Parsed with a regex rather than eval, so
# no device or filesystem name is ever executed as shell.
LSBLK_PAIR_PATTERN = re.compile(r'(\w+)="([^"]*)"')
LSBLK_COLUMNS = "PATH,NAME,TYPE,RM,HOTPLUG,FSTYPE,UUID,PKNAME"

CATEGORY_ENUMERATION_FAILED = "enumeration_failed"
CATEGORY_NO_CARD = "no_card"
CATEGORY_AMBIGUOUS = "ambiguous_devices"
CATEGORY_MOUNTED = "card_mounted"
CATEGORY_MOUNT_FAILED = "mount_failed"
CATEGORY_UNMOUNTED = "card_unmounted"
CATEGORY_UNMOUNT_FAILED = "unmount_failed"

# Reconciliation outcomes, returned for logging and tests.
OUTCOME_MOUNTED = "mounted"
OUTCOME_UNMOUNTED = "unmounted"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_IDLE = "idle"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_MOUNT_FAILED = "mount_failed"
OUTCOME_UNMOUNT_FAILED = "unmount_failed"
OUTCOME_ENUMERATION_FAILED = "enumeration_failed"


class MounterError(Exception):
    """Block-device state could not be determined this tick."""


@dataclass(frozen=True)
class BlockDevice:
    """One row of `lsblk -P` output."""

    path: str
    name: str
    kind: str  # lsblk TYPE: disk, part, loop, ...
    removable: bool
    hotplug: bool
    filesystem: str
    uuid: str
    parent: str  # lsblk PKNAME

    @property
    def has_filesystem(self) -> bool:
        return bool(self.filesystem)

    def describe(self) -> str:
        return (
            f"{self.path or self.name} (type={self.kind or 'unknown'} "
            f"fstype={self.filesystem or 'none'} uuid={self.uuid or 'none'})"
        )


@dataclass(frozen=True)
class MounterConfig:
    card_mountpoint: Path = DEFAULT_CARD_MOUNTPOINT
    mount_interval_seconds: float = 2.0
    error_log_repeat_seconds: float = 300.0
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 5

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "MounterConfig":
        env = os.environ if env is None else env

        mountpoint = env.get("CARD_MOUNTPOINT", "").strip()
        if not mountpoint:
            raise ConfigError("CARD_MOUNTPOINT is required")
        if not mountpoint.startswith("/"):
            raise ConfigError(
                f"CARD_MOUNTPOINT must be an absolute path, got {mountpoint!r}"
            )

        return MounterConfig(
            card_mountpoint=Path(mountpoint),
            mount_interval_seconds=positive_float(env, "CARD_MOUNT_INTERVAL_SECONDS", 2.0),
            error_log_repeat_seconds=positive_float(env, "ERROR_LOG_REPEAT_SECONDS", 300.0),
            log_path=Path(env.get("MOUNTER_LOG_PATH", "").strip() or DEFAULT_LOG_PATH),
            log_max_bytes=int(positive_float(env, "LOG_MAX_BYTES", 1_048_576)),
            log_backup_count=int(positive_float(env, "LOG_BACKUP_COUNT", 5)),
        )


Runner = Callable[..., subprocess.CompletedProcess]


def run_command(argv: list, runner: Runner = subprocess.run) -> tuple:
    """Run one command. Returns `(returncode, stdout)`; never raises.

    A missing binary or a timeout is reported as a non-zero status, so one
    unavailable tool degrades a tick instead of taking the daemon down.
    """
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return 127, f"{argv[0]} could not be run: {exc}"
    return completed.returncode, (completed.stdout or "")


def parse_lsblk(output: str) -> list:
    """Turn `lsblk -P` output into `BlockDevice` rows."""
    devices = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = {key: value for key, value in LSBLK_PAIR_PATTERN.findall(line)}
        if not fields:
            continue
        devices.append(
            BlockDevice(
                path=fields.get("PATH", ""),
                name=fields.get("NAME", ""),
                kind=fields.get("TYPE", ""),
                removable=fields.get("RM", "") == "1",
                hotplug=fields.get("HOTPLUG", "") == "1",
                filesystem=fields.get("FSTYPE", ""),
                uuid=fields.get("UUID", ""),
                parent=fields.get("PKNAME", ""),
            )
        )
    return devices


def list_block_devices(runner: Runner = subprocess.run) -> list:
    """Every block device the kernel currently reports."""
    status, output = run_command(["lsblk", "-P", "-o", LSBLK_COLUMNS], runner=runner)
    if status != 0:
        raise MounterError(f"lsblk failed (status {status}): {output.strip()}")
    return parse_lsblk(output)


def top_level_disk(device: BlockDevice, by_name: dict) -> str:
    """Walk PKNAME up to the physical disk holding `device`.

    A partition's parent is its disk; a whole-disk device is its own answer.
    The chain is bounded so a cyclic or self-referential PKNAME cannot spin.
    """
    current = device
    for _ in range(8):
        if not current.parent:
            return current.name
        parent = by_name.get(current.parent)
        if parent is None or parent.name == current.name:
            return current.parent
        current = parent
    return current.name  # pragma: no cover - defensive


def root_disk_name(devices: list, runner: Runner = subprocess.run) -> str:
    """The physical disk holding `/`, or "" if it cannot be determined."""
    status, output = run_command(["findmnt", "-n", "-o", "SOURCE", "/"], runner=runner)
    source = output.strip().splitlines()[0].strip() if status == 0 and output.strip() else ""
    if not source:
        return ""

    by_name = {device.name: device for device in devices if device.name}
    for device in devices:
        if device.path and device.path == source:
            return top_level_disk(device, by_name)
    # `findmnt` can report a name rather than a path (e.g. /dev/root aliases).
    basename = source.rsplit("/", 1)[-1]
    device = by_name.get(basename)
    return top_level_disk(device, by_name) if device is not None else ""


def select_candidates(devices: list, root_disk: str) -> tuple:
    """Split filesystem-bearing devices into eligible cards and rejections.

    Returns `(candidates, rejections)`, where each rejection is
    `(device, reason)`. Only devices that actually carry a filesystem are
    considered at all; bare disks and empty partitions are not "rejected", they
    are simply not cards.
    """
    by_name = {device.name: device for device in devices if device.name}
    partition_parents = {
        device.parent
        for device in devices
        if device.kind == "part" and device.has_filesystem and device.parent
    }

    candidates = []
    rejections = []
    for device in devices:
        if not device.has_filesystem:
            continue

        if device.kind == "part":
            pass
        elif device.kind == "disk":
            if device.name in partition_parents:
                # A filesystem signature on the disk *and* a filesystem-bearing
                # partition on it: ambiguous, so neither view is trusted.
                rejections.append(
                    (device, "whole-disk filesystem shadowed by a partition filesystem")
                )
                continue
        else:
            kind = device.kind or "unknown"
            rejections.append((device, f"device type {kind} is not a partition or disk"))
            continue

        if not (device.removable or device.hotplug):
            # Some USB card readers report RM=0 and only set HOTPLUG=1.
            rejections.append((device, "device is neither removable nor hotplug"))
            continue

        if root_disk and top_level_disk(device, by_name) == root_disk:
            rejections.append((device, "device is on the same physical disk as /"))
            continue

        if device.filesystem not in SUPPORTED_FILESYSTEMS:
            rejections.append(
                (
                    device,
                    f"unsupported filesystem {device.filesystem!r}; supported: "
                    + " ".join(SUPPORTED_FILESYSTEMS),
                )
            )
            continue

        if not is_safe_card_uuid(device.uuid):
            rejections.append((device, "missing or unsafe filesystem UUID"))
            continue

        candidates.append(device)

    return candidates, rejections


def mount_options(filesystem: str) -> str:
    """Read-only mount options for `filesystem`."""
    options = list(BASE_MOUNT_OPTIONS)
    if filesystem in JOURNAL_FILESYSTEMS:
        options.append("noload")
    return ",".join(options)


def mounted_source(mountpoint: Path, runner: Runner = subprocess.run) -> Optional[str]:
    """The device mounted exactly at `mountpoint`, or None if nothing is."""
    status, output = run_command(
        ["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(mountpoint)], runner=runner
    )
    if status != 0:
        return None
    source = output.strip()
    return source.splitlines()[0].strip() if source else None


def unmount(mountpoint: Path, runner: Runner = subprocess.run) -> tuple:
    """Unmount `mountpoint`. Returns `(succeeded, message)`.

    Falls back to a lazy unmount, which is what a yanked card needs: its
    filesystem is gone and a plain umount can fail on the dead device.
    """
    status, output = run_command(["umount", str(mountpoint)], runner=runner)
    if status == 0:
        return True, ""
    lazy_status, lazy_output = run_command(["umount", "-l", str(mountpoint)], runner=runner)
    if lazy_status == 0:
        return True, "needed a lazy unmount"
    return False, (lazy_output.strip() or output.strip() or f"umount status {lazy_status}")


def mount_card(
    device: BlockDevice, mountpoint: Path, runner: Runner = subprocess.run
) -> tuple:
    """Mount `device` read-only at `mountpoint`. Returns `(succeeded, message)`."""
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create {mountpoint}: {exc}"

    options = mount_options(device.filesystem)
    status, output = run_command(
        ["mount", "-t", device.filesystem, "-o", options, device.path, str(mountpoint)],
        runner=runner,
    )
    if status == 0:
        return True, options
    return False, (output.strip() or f"mount status {status}")


def _report(
    state_logger: StateLogger,
    category: str,
    message: str,
    notes: list,
    level: int = logging.INFO,
    always: bool = False,
) -> None:
    """Log one tick as one state.

    A tick has exactly one outcome, so it emits exactly one line and the whole
    line is what gets rate-limited. Declined devices ride along in both the
    message and the category, so a device set that changes — a card inserted, a
    different card rejected for a new reason — is a state change and is logged
    at once, while an unchanged Pi logs once per repeat window instead of once
    every couple of seconds.
    """
    if notes:
        joined = "; ".join(notes)
        message = f"{message} | {joined}"
        category = f"{category}|{joined}"
    state_logger.record(category, message, level=level, always=always)


def _notes(root_disk: str, rejections: list) -> list:
    notes = []
    if not root_disk:
        # Filter 3 cannot run. The removable/hotplug filter still stands between
        # this daemon and the Pi's own disk, but say so rather than hide it.
        notes.append(
            "warning: the disk holding / could not be determined, so the "
            "root-disk exclusion is inactive this tick"
        )
    if rejections:
        notes.append(
            "not mounting: "
            + "; ".join(f"{device.describe()}: {reason}" for device, reason in rejections)
        )
    return notes


def reconcile(
    config: MounterConfig,
    state_logger: StateLogger,
    runner: Runner = subprocess.run,
) -> str:
    """Converge the mountpoint on the one eligible card. Never raises.

    Returns an outcome token. A failure is logged and simply retried on the
    next tick; nothing here is allowed to stop the daemon.
    """
    try:
        devices = list_block_devices(runner=runner)
    except MounterError as exc:
        # Deliberately changes no mount state: "I cannot see any devices" must
        # never be read as "the card is gone".
        state_logger.record(CATEGORY_ENUMERATION_FAILED, str(exc), level=logging.ERROR)
        return OUTCOME_ENUMERATION_FAILED

    root_disk = root_disk_name(devices, runner=runner)
    candidates, rejections = select_candidates(devices, root_disk)
    notes = _notes(root_disk, rejections)
    current = mounted_source(config.card_mountpoint, runner=runner)

    if len(candidates) > 1:
        # Explicitly out of scope: one card at a time. Changing nothing is the
        # safe response, since guessing could unmount a card mid-ingest.
        _report(
            state_logger,
            CATEGORY_AMBIGUOUS + ":" + ",".join(device.path for device in candidates),
            "more than one eligible card is present ({}); leaving {} unchanged".format(
                ", ".join(device.describe() for device in candidates),
                config.card_mountpoint,
            ),
            notes,
            level=logging.WARNING,
        )
        return OUTCOME_AMBIGUOUS

    if not candidates:
        if current is None:
            _report(
                state_logger,
                CATEGORY_NO_CARD,
                f"no eligible card is present; {config.card_mountpoint} is empty",
                notes,
            )
            return OUTCOME_IDLE
        succeeded, detail = unmount(config.card_mountpoint, runner=runner)
        if succeeded:
            _report(
                state_logger,
                CATEGORY_UNMOUNTED,
                f"card is gone; unmounted {current} from {config.card_mountpoint}"
                + (f" ({detail})" if detail else ""),
                notes,
                always=True,
            )
            return OUTCOME_UNMOUNTED
        _report(
            state_logger,
            CATEGORY_UNMOUNT_FAILED,
            f"could not unmount {config.card_mountpoint}: {detail}",
            notes,
            level=logging.ERROR,
        )
        return OUTCOME_UNMOUNT_FAILED

    card = candidates[0]
    if current == card.path:
        _report(
            state_logger,
            f"{CATEGORY_MOUNTED}:{card.uuid}",
            f"card {card.uuid} ({card.path}) is mounted at {config.card_mountpoint}",
            notes,
        )
        return OUTCOME_UNCHANGED

    if current is not None:
        succeeded, detail = unmount(config.card_mountpoint, runner=runner)
        if not succeeded:
            # Mounting over the stale mount would hide it rather than replace
            # it, so the new card waits for the next tick.
            _report(
                state_logger,
                CATEGORY_UNMOUNT_FAILED,
                f"could not unmount stale {current} from {config.card_mountpoint}: {detail}",
                notes,
                level=logging.ERROR,
            )
            return OUTCOME_UNMOUNT_FAILED
        state_logger.logger.info(
            "unmounted stale %s from %s", current, config.card_mountpoint
        )

    succeeded, detail = mount_card(card, config.card_mountpoint, runner=runner)
    if not succeeded:
        _report(
            state_logger,
            f"{CATEGORY_MOUNT_FAILED}:{card.path}",
            f"could not mount {card.describe()} at {config.card_mountpoint}: {detail}",
            notes,
            level=logging.ERROR,
        )
        return OUTCOME_MOUNT_FAILED

    _report(
        state_logger,
        f"{CATEGORY_MOUNTED}:{card.uuid}",
        f"mounted card {card.uuid} ({card.path}, {card.filesystem}) at "
        f"{config.card_mountpoint} with {detail}",
        notes,
        always=True,
    )
    return OUTCOME_MOUNTED


def run(
    config: MounterConfig,
    state_logger: StateLogger,
    stop_event: threading.Event,
    runner: Runner = subprocess.run,
    max_iterations: Optional[int] = None,
) -> int:
    """Reconcile on a fixed cadence until stopped. Strictly sequential."""
    iterations = 0

    while not stop_event.is_set():
        reconcile(config, state_logger, runner=runner)
        iterations += 1

        if max_iterations is not None and iterations >= max_iterations:
            break
        if stop_event.wait(config.mount_interval_seconds):
            break

    return iterations


def main(argv: Optional[list] = None) -> int:
    try:
        config = MounterConfig.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logger = build_logger(
        LOGGER_NAME, config.log_path, config.log_max_bytes, config.log_backup_count
    )
    logger.info(
        "sdcard mounter starting: mountpoint=%s interval=%ss filesystems=%s",
        config.card_mountpoint,
        config.mount_interval_seconds,
        " ".join(SUPPORTED_FILESYSTEMS),
    )

    try:
        config.card_mountpoint.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("cannot create %s: %s", config.card_mountpoint, exc)
        return 1

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        # The mount is deliberately left in place: a restart re-reconciles, and
        # unmounting here would interrupt an in-progress scan for no gain.
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    state_logger = StateLogger(logger, config.error_log_repeat_seconds)
    run(config, state_logger, stop_event)
    logger.info("sdcard mounter stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
