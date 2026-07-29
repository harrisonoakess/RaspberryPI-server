"""SD card watcher tests — PRD success criteria 3 (offline queueing),
5 (uploaded-card reinsertion), and 6 (pending-card reinsertion)."""

import logging
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import sdcard_watcher
from sdcard_watcher import (
    CardError,
    FileTooLarge,
    WatcherConfig,
    card_device,
    card_is_mounted,
    check_once,
    clear_stale_temp_files,
    copy_into_queue,
    in_scope_files,
    ingest_card,
    prepare_directories,
    run,
    wait_for_change,
)
from state import STATUS_PENDING, STATUS_UPLOADED, Ledger
from uploader import ConfigError, StateLogger

CARD_UUID = "1234-ABCD"


@pytest.fixture
def card(tmp_path):
    """Stands in for the read-only mountpoint of the logger's card."""
    mountpoint = tmp_path / "mnt" / "sdcard"
    mountpoint.mkdir(parents=True)
    return mountpoint


@pytest.fixture
def config(tmp_path, card):
    return WatcherConfig(
        card_uuid=CARD_UUID,
        card_mountpoint=card,
        card_filesystem="vfat",
        queue_path=tmp_path / "queue",
        state_db_path=tmp_path / "state.db",
        by_uuid_root=tmp_path / "by-uuid",
        scan_interval_seconds=5.0,
    )


@pytest.fixture
def ledger(config):
    instance = Ledger(config.state_db_path)
    instance.initialize()
    return instance


@pytest.fixture
def watcher_logger(caplog):
    logger = logging.getLogger("piuploader.watcher.test")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="piuploader.watcher.test")
    return logger


@pytest.fixture
def ready(config, ledger):
    prepare_directories(config)
    return config


def write_card_file(card, name, content=b"a,b\n1,2\n"):
    path = card / name
    path.write_bytes(content)
    return path


def queued(config):
    return sorted(entry.name for entry in os.scandir(config.pending_dir))


# --- Configuration ------------------------------------------------------------


def test_config_from_env_applies_documented_defaults():
    config = WatcherConfig.from_env(
        {"CARD_UUID": CARD_UUID, "CARD_MOUNTPOINT": "/mnt/sdcard"}
    )

    assert config.card_uuid == CARD_UUID
    assert config.card_mountpoint == Path("/mnt/sdcard")
    assert config.queue_path == Path("/var/lib/piuploader/queue")
    assert config.state_db_path == Path("/var/lib/piuploader/state.db")
    assert config.max_upload_bytes == 10_485_760
    assert config.scan_interval_seconds == 5.0
    assert config.pending_dir == Path("/var/lib/piuploader/queue/pending")
    assert config.temp_dir == Path("/var/lib/piuploader/queue/tmp")


@pytest.mark.parametrize(
    "env",
    [
        # An unconfigured card must fail loudly rather than fall back to any
        # removable drive that happens to be attached.
        {"CARD_MOUNTPOINT": "/mnt/sdcard"},
        {"CARD_UUID": "", "CARD_MOUNTPOINT": "/mnt/sdcard"},
        {"CARD_UUID": "  ", "CARD_MOUNTPOINT": "/mnt/sdcard"},
        {"CARD_UUID": CARD_UUID},
        {"CARD_UUID": CARD_UUID, "CARD_MOUNTPOINT": ""},
        {"CARD_UUID": CARD_UUID, "CARD_MOUNTPOINT": "/mnt/sdcard", "MAX_UPLOAD_BYTES": "0"},
        {"CARD_UUID": CARD_UUID, "CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_SCAN_INTERVAL_SECONDS": "x"},
    ],
)
def test_invalid_config_is_rejected(env):
    with pytest.raises(ConfigError):
        WatcherConfig.from_env(env)


def test_temp_dir_shares_the_queue_filesystem_so_publishing_is_a_rename(config):
    assert config.temp_dir.parent == config.pending_dir.parent


# --- Card identity ------------------------------------------------------------


def test_card_device_is_none_when_the_card_is_not_inserted(config):
    config.by_uuid_root.mkdir(parents=True)
    assert card_device(config) is None


def test_card_device_is_none_when_the_by_uuid_directory_is_missing(config):
    assert card_device(config) is None


def test_card_device_resolves_the_by_uuid_symlink(config, tmp_path):
    config.by_uuid_root.mkdir(parents=True)
    node = tmp_path / "sda1"
    node.write_bytes(b"")
    (config.by_uuid_root / CARD_UUID).symlink_to(node)

    assert card_device(config) == node.resolve()


def test_a_different_uuid_is_not_the_configured_card(config, tmp_path):
    config.by_uuid_root.mkdir(parents=True)
    node = tmp_path / "sdb1"
    node.write_bytes(b"")
    (config.by_uuid_root / "0000-FFFF").symlink_to(node)

    assert card_device(config) is None


class FakeStat:
    def __init__(self, dev=0, rdev=0):
        self.st_dev = dev
        self.st_rdev = rdev


def test_card_is_mounted_when_the_mountpoint_holds_that_device(config, monkeypatch):
    def fake_stat(path):
        return FakeStat(dev=42) if str(path) == str(config.card_mountpoint) else FakeStat(rdev=42)

    monkeypatch.setattr(sdcard_watcher.os, "stat", fake_stat)
    assert card_is_mounted(config, Path("/dev/sda1")) is True


def test_card_is_not_mounted_when_another_filesystem_is_at_the_mountpoint(config, monkeypatch):
    """Guards against scanning some other removable drive mounted there."""

    def fake_stat(path):
        return FakeStat(dev=7) if str(path) == str(config.card_mountpoint) else FakeStat(rdev=42)

    monkeypatch.setattr(sdcard_watcher.os, "stat", fake_stat)
    assert card_is_mounted(config, Path("/dev/sda1")) is False


def test_card_is_not_mounted_when_the_mountpoint_cannot_be_stated(config):
    assert card_is_mounted(config, Path("/dev/does-not-exist")) is False


# --- Scanning scope -----------------------------------------------------------


def test_only_root_level_csv_files_are_in_scope(card):
    write_card_file(card, "logger-0002.csv")
    write_card_file(card, "logger-0001.csv")
    write_card_file(card, "UPPER.CSV")
    write_card_file(card, "notes.txt")
    write_card_file(card, "archive.csv.gz")
    (card / "System Volume Information").mkdir()
    (card / "nested").mkdir()
    write_card_file(card, "nested/deep.csv")

    assert in_scope_files(card) == ["UPPER.CSV", "logger-0001.csv", "logger-0002.csv"]


def test_a_directory_named_like_a_csv_is_not_in_scope(card):
    (card / "looks-like.csv").mkdir()
    assert in_scope_files(card) == []


def test_a_symlink_is_never_followed_off_the_card(card, tmp_path):
    secret = tmp_path / "outside.csv"
    secret.write_bytes(b"not ours\n")
    (card / "link.csv").symlink_to(secret)

    assert in_scope_files(card) == []


def test_scanning_a_missing_mountpoint_raises_card_error(tmp_path):
    with pytest.raises(CardError):
        in_scope_files(tmp_path / "not-mounted")


# --- Criterion 3: offline queueing --------------------------------------------


def test_new_files_are_copied_into_the_queue_and_recorded_pending(
    ready, card, ledger, watcher_logger
):
    write_card_file(card, "logger-0001.csv", b"a,b\n1,2\n")
    write_card_file(card, "logger-0002.csv", b"c,d\n3,4\n")

    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.rejected, result.failed) == (2, 0, 0)
    assert queued(ready) == ["logger-0001.csv", "logger-0002.csv"]
    assert (ready.pending_dir / "logger-0001.csv").read_bytes() == b"a,b\n1,2\n"
    assert ledger.filenames_with_status(STATUS_PENDING) == [
        "logger-0001.csv",
        "logger-0002.csv",
    ]


def test_ingestion_never_modifies_the_card(ready, card, ledger, watcher_logger):
    source = write_card_file(card, "logger-0001.csv", b"a,b\n1,2\n")
    before = (source.read_bytes(), sorted(os.listdir(card)))

    ingest_card(ready, ledger, watcher_logger)

    assert (source.read_bytes(), sorted(os.listdir(card))) == before


def test_out_of_scope_files_are_left_alone(ready, card, ledger, watcher_logger):
    write_card_file(card, "notes.txt")
    (card / "nested").mkdir()

    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.rejected, result.failed) == (0, 0, 0)
    assert queued(ready) == []


def test_an_empty_csv_is_copied(ready, card, ledger, watcher_logger):
    write_card_file(card, "empty.csv", b"")

    assert ingest_card(ready, ledger, watcher_logger).copied == 1
    assert (ready.pending_dir / "empty.csv").read_bytes() == b""


def test_a_file_exactly_at_the_limit_is_copied(ready, card, ledger, watcher_logger):
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "exact.csv", b"x" * 64)

    assert ingest_card(config, ledger, watcher_logger).copied == 1
    assert (config.pending_dir / "exact.csv").stat().st_size == 64


def test_a_file_one_byte_over_the_limit_is_rejected_and_not_queued(
    ready, card, ledger, watcher_logger
):
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "toobig.csv", b"x" * 65)

    result = ingest_card(config, ledger, watcher_logger)

    assert (result.copied, result.rejected) == (0, 1)
    assert queued(config) == []
    assert ledger.known_filenames() == set()


def test_an_unsafe_filename_is_rejected_and_not_queued(ready, card, ledger, watcher_logger):
    write_card_file(card, "bad\nname.csv")

    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.rejected) == (0, 1)
    assert queued(ready) == []


def test_a_rejected_file_does_not_stop_the_files_after_it(
    ready, card, ledger, watcher_logger
):
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "a-ok.csv", b"x" * 10)
    write_card_file(card, "b-toobig.csv", b"x" * 65)
    write_card_file(card, "c-ok.csv", b"x" * 10)

    result = ingest_card(config, ledger, watcher_logger)

    assert (result.copied, result.rejected) == (2, 1)
    assert queued(config) == ["a-ok.csv", "c-ok.csv"]


def test_an_unreadable_file_is_reported_and_leaves_nothing_behind(
    ready, card, ledger, watcher_logger
):
    """Stands in for a hot-unplug mid-copy: no partial file is published, the
    ledger stays clean, and the next insertion retries."""
    source = write_card_file(card, "unreadable.csv", b"x" * 100)
    source.chmod(0o000)
    try:
        result = ingest_card(ready, ledger, watcher_logger)
    finally:
        source.chmod(0o644)

    assert (result.copied, result.failed) == (0, 1)
    assert queued(ready) == []
    assert ledger.known_filenames() == set()
    assert list(os.scandir(ready.temp_dir)) == []


def test_a_successful_copy_leaves_no_temporary_files(ready, card, ledger, watcher_logger):
    write_card_file(card, "a.csv")
    ingest_card(ready, ledger, watcher_logger)
    assert list(os.scandir(ready.temp_dir)) == []


def test_copy_into_queue_refuses_a_file_over_the_limit_while_reading(ready, card):
    config = replace(ready, max_upload_bytes=8)
    source = write_card_file(card, "big.csv", b"x" * 9)

    with pytest.raises(FileTooLarge):
        copy_into_queue(source, config)

    assert queued(config) == []
    assert list(os.scandir(config.temp_dir)) == []


# --- Criteria 5 and 6: reinsertion of a never-cleared card ---------------------


def test_reinserting_the_card_after_upload_copies_nothing(
    ready, card, ledger, watcher_logger
):
    """Criterion 5: the whole history is on the card, and none of it is re-copied."""
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger)

    # The uploader delivers the file and clears the queued copy.
    ledger.mark_uploaded("logger-0001.csv")
    (ready.pending_dir / "logger-0001.csv").unlink()

    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.skipped_known) == (0, 1)
    assert queued(ready) == []
    assert ledger.status_of("logger-0001.csv") == STATUS_UPLOADED


def test_reinserting_the_card_before_upload_makes_no_duplicate_copy(
    ready, card, ledger, watcher_logger
):
    """Criterion 6: a `pending` row is honoured, so the queue keeps one copy."""
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger)

    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.skipped_known) == (0, 1)
    assert queued(ready) == ["logger-0001.csv"]
    assert ledger.status_of("logger-0001.csv") == STATUS_PENDING


def test_reinsertion_with_new_files_copies_only_the_new_ones(
    ready, card, ledger, watcher_logger
):
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger)
    ledger.mark_uploaded("logger-0001.csv")
    (ready.pending_dir / "logger-0001.csv").unlink()

    # The logger appends nothing; it writes a new, distinct name.
    write_card_file(card, "logger-0002.csv")
    result = ingest_card(ready, ledger, watcher_logger)

    assert (result.copied, result.skipped_known) == (1, 1)
    assert queued(ready) == ["logger-0002.csv"]


# --- Stale temporary files ----------------------------------------------------


def test_stale_temporary_copies_are_cleared_at_start_up(ready, watcher_logger):
    (ready.temp_dir / ".copy-abc.part").write_bytes(b"half a file")

    assert clear_stale_temp_files(ready, watcher_logger) == 1
    assert list(os.scandir(ready.temp_dir)) == []


def test_clearing_temporary_copies_never_touches_the_queue(ready, watcher_logger):
    (ready.pending_dir / "a.csv").write_bytes(b"real")

    clear_stale_temp_files(ready, watcher_logger)

    assert queued(ready) == ["a.csv"]


def test_prepare_directories_is_idempotent(config, ledger):
    prepare_directories(config)
    prepare_directories(config)
    assert config.pending_dir.is_dir() and config.temp_dir.is_dir()


# --- The watch loop -----------------------------------------------------------


def _mounted(config, monkeypatch, device=Path("/dev/sda1")):
    monkeypatch.setattr(sdcard_watcher, "card_device", lambda _config: device)
    monkeypatch.setattr(sdcard_watcher, "card_is_mounted", lambda _config, _device: True)


def test_an_absent_card_is_not_scanned(ready, ledger, watcher_logger, monkeypatch, caplog):
    monkeypatch.setattr(sdcard_watcher, "card_device", lambda _config: None)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state, False) is False
    assert "not inserted" in caplog.text


def test_a_present_but_unmounted_card_is_not_scanned(
    ready, card, ledger, watcher_logger, monkeypatch, caplog
):
    write_card_file(card, "a.csv")
    monkeypatch.setattr(sdcard_watcher, "card_device", lambda _config: Path("/dev/sda1"))
    monkeypatch.setattr(sdcard_watcher, "card_is_mounted", lambda _config, _device: False)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state, False) is False
    assert queued(ready) == []
    assert "not mounted" in caplog.text


def test_insertion_triggers_exactly_one_ingest(
    ready, card, ledger, watcher_logger, monkeypatch
):
    write_card_file(card, "a.csv")
    _mounted(ready, monkeypatch)
    state = StateLogger(watcher_logger, 300.0)

    present = check_once(ready, ledger, watcher_logger, state, False)
    assert present is True
    assert queued(ready) == ["a.csv"]

    # A card that stays inserted is not re-scanned every tick.
    write_card_file(card, "b.csv")
    assert check_once(ready, ledger, watcher_logger, state, present) is True
    assert queued(ready) == ["a.csv"]


def test_removing_and_reinserting_the_card_scans_again(
    ready, card, ledger, watcher_logger, monkeypatch
):
    write_card_file(card, "a.csv")
    _mounted(ready, monkeypatch)
    state = StateLogger(watcher_logger, 300.0)

    check_once(ready, ledger, watcher_logger, state, False)
    write_card_file(card, "b.csv")

    # Removal, then reinsertion: presence goes False, so the next check scans.
    monkeypatch.setattr(sdcard_watcher, "card_device", lambda _config: None)
    assert check_once(ready, ledger, watcher_logger, state, True) is False
    _mounted(ready, monkeypatch)
    assert check_once(ready, ledger, watcher_logger, state, False) is True

    assert queued(ready) == ["a.csv", "b.csv"]


def test_a_scan_failure_does_not_crash_the_loop(ready, ledger, watcher_logger, monkeypatch, caplog):
    monkeypatch.setattr(sdcard_watcher, "card_device", lambda _config: Path("/dev/sda1"))
    monkeypatch.setattr(sdcard_watcher, "card_is_mounted", lambda _config, _device: True)

    def broken(*args, **kwargs):
        raise CardError("cannot list the mountpoint")

    monkeypatch.setattr(sdcard_watcher, "ingest_card", broken)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state, False) is False
    assert "cannot list the mountpoint" in caplog.text


def test_an_unexpected_ingest_error_does_not_crash_the_loop(
    ready, ledger, watcher_logger, monkeypatch, caplog
):
    _mounted(ready, monkeypatch)

    def explode(*args, **kwargs):
        raise ValueError("something unforeseen")

    monkeypatch.setattr(sdcard_watcher, "ingest_card", explode)
    state = StateLogger(watcher_logger, 300.0)

    check_once(ready, ledger, watcher_logger, state, False)
    assert "unexpected error ingesting the card" in caplog.text


def test_run_stops_after_the_requested_iterations(ready, ledger, watcher_logger, monkeypatch):
    _mounted(ready, monkeypatch)
    state = StateLogger(watcher_logger, 300.0)
    quick = replace(ready, scan_interval_seconds=0.01)

    iterations = run(
        quick, ledger, watcher_logger, state, threading.Event(), max_iterations=2
    )

    assert iterations == 2


def test_run_does_nothing_when_already_stopped(ready, ledger, watcher_logger, monkeypatch):
    _mounted(ready, monkeypatch)
    stop_event = threading.Event()
    stop_event.set()

    iterations = run(ready, ledger, watcher_logger, StateLogger(watcher_logger, 300.0), stop_event)

    assert iterations == 0


def test_run_ingests_an_already_inserted_card_on_the_first_tick(
    ready, card, ledger, watcher_logger, monkeypatch
):
    write_card_file(card, "a.csv")
    _mounted(ready, monkeypatch)

    run(
        ready,
        ledger,
        watcher_logger,
        StateLogger(watcher_logger, 300.0),
        threading.Event(),
        max_iterations=1,
    )

    assert queued(ready) == ["a.csv"]


# --- Waiting for udev events --------------------------------------------------


class FakeMonitor:
    """Stands in for a pyudev Monitor; `poll` yields scripted events."""

    def __init__(self, events):
        self.events = list(events)
        self.timeouts = []

    def poll(self, timeout=None):
        self.timeouts.append(timeout)
        return self.events.pop(0) if self.events else None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.25  # each poll consumes a slice of the deadline
        return self.now


def test_wait_for_change_returns_as_soon_as_a_device_event_arrives():
    monitor = FakeMonitor([None, "sda1"])
    wait_for_change(monitor, threading.Event(), 5.0, clock=Clock())

    assert len(monitor.timeouts) == 2


def test_wait_for_change_polls_in_short_slices_so_sigterm_lands_promptly():
    monitor = FakeMonitor([])
    wait_for_change(monitor, threading.Event(), 5.0, clock=Clock())

    assert monitor.timeouts and max(monitor.timeouts) <= 1.0


def test_wait_for_change_returns_immediately_when_stopped():
    monitor = FakeMonitor([])
    stop_event = threading.Event()
    stop_event.set()

    wait_for_change(monitor, stop_event, 5.0, clock=Clock())

    assert monitor.timeouts == []


def test_wait_for_change_falls_back_to_sleeping_without_a_monitor():
    stop_event = threading.Event()
    stop_event.set()  # returns instantly instead of sleeping
    wait_for_change(None, stop_event, 5.0)


def test_missing_pyudev_falls_back_to_polling(watcher_logger, monkeypatch, caplog):
    """The udev fast path is optional: losing it costs latency, not correctness."""
    # A None entry in sys.modules makes `import pyudev` raise ImportError, which
    # is what a Pi with no internet access during setup looks like.
    monkeypatch.setitem(sys.modules, "pyudev", None)

    assert sdcard_watcher.open_udev_monitor(watcher_logger) is None
    assert "falling back to periodic polling" in caplog.text
