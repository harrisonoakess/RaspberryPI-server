"""SD card watcher tests — PRD criteria 4 (watcher discovery), 5 (mount-change
safety), 6 (per-card local identity), and the Phase 2 delivery guarantees that
carry forward per card."""

import logging
import os
import stat as stat_module
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import sdcard_watcher
from sdcard_watcher import (
    CardChanged,
    CardError,
    CardIdentity,
    FileTooLarge,
    WatcherConfig,
    check_once,
    clear_stale_temp_files,
    copy_into_queue,
    discover_card,
    in_scope_files,
    ingest_card,
    is_mount_boundary,
    prepare_directories,
    run,
    wait_for_change,
)
from state import STATUS_PENDING, STATUS_UPLOADED, Ledger
from uploader import ConfigError, StateLogger

CARD_A = "1234-ABCD"
CARD_B = "5678-EF01"
DEVICE_A = 4242
DEVICE_B = 4343

MOUNTED_A = CardIdentity(uuid=CARD_A, device_number=DEVICE_A)
MOUNTED_B = CardIdentity(uuid=CARD_B, device_number=DEVICE_B)


@pytest.fixture
def card(tmp_path):
    """Stands in for the read-only mountpoint of a logger's card."""
    mountpoint = tmp_path / "mnt" / "sdcard"
    mountpoint.mkdir(parents=True)
    return mountpoint


@pytest.fixture
def config(tmp_path, card):
    return WatcherConfig(
        card_mountpoint=card,
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


@pytest.fixture
def mounted(monkeypatch):
    """Pin what `discover_card` reports, one value or a scripted sequence."""

    def install(*identities):
        remaining = list(identities)
        last = remaining[-1] if remaining else None

        def fake_discover(_config):
            return remaining.pop(0) if remaining else last

        monkeypatch.setattr(sdcard_watcher, "discover_card", fake_discover)

    return install


def write_card_file(card, name, content=b"a,b\n1,2\n"):
    path = card / name
    path.write_bytes(content)
    return path


def queued(config, card_uuid=CARD_A):
    return sorted(entry.name for entry in os.scandir(config.pending_dir_for(card_uuid)))


def queued_cards(config):
    return sorted(entry.name for entry in os.scandir(config.pending_dir))


class FakeStat:
    def __init__(self, dev=0, rdev=0, mode=stat_module.S_IFBLK):
        self.st_dev = dev
        self.st_rdev = rdev
        self.st_mode = mode


def stat_table(entries):
    """An `os.stat` replacement backed by a `{path: FakeStat}` table."""

    def fake_stat(path):
        try:
            return entries[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    return fake_stat


# --- Configuration ------------------------------------------------------------


def test_config_from_env_applies_documented_defaults():
    config = WatcherConfig.from_env({"CARD_MOUNTPOINT": "/mnt/sdcard"})

    assert config.card_mountpoint == Path("/mnt/sdcard")
    assert config.queue_path == Path("/var/lib/piuploader/queue")
    assert config.state_db_path == Path("/var/lib/piuploader/state.db")
    assert config.max_upload_bytes == 20_971_520
    assert config.scan_interval_seconds == 5.0
    assert config.pending_dir == Path("/var/lib/piuploader/queue/pending")
    assert config.temp_dir == Path("/var/lib/piuploader/queue/tmp")


def test_no_card_specific_configuration_is_required():
    """Phase 3 removes CARD_UUID and CARD_FILESYSTEM; leftovers are ignored."""
    config = WatcherConfig.from_env(
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_UUID": "stale", "CARD_FILESYSTEM": "vfat"}
    )

    assert not hasattr(config, "card_uuid")
    assert not hasattr(config, "card_filesystem")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CARD_MOUNTPOINT": ""},
        {"CARD_MOUNTPOINT": "   "},
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "MAX_UPLOAD_BYTES": "0"},
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_SCAN_INTERVAL_SECONDS": "x"},
    ],
)
def test_invalid_config_is_rejected(env):
    with pytest.raises(ConfigError):
        WatcherConfig.from_env(env)


def test_temp_dir_shares_the_queue_filesystem_so_publishing_is_a_rename(config):
    assert config.temp_dir.parent == config.pending_dir.parent


def test_the_queue_is_namespaced_per_card(config):
    assert config.pending_dir_for(CARD_A) == config.pending_dir / CARD_A
    assert config.pending_dir_for(CARD_B) != config.pending_dir_for(CARD_A)


# --- Criterion 4: watcher discovery -------------------------------------------


def test_nothing_mounted_means_no_card(config, monkeypatch):
    """The mountpoint shares its device number with its parent directory."""
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=1),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
            }
        ),
    )

    assert is_mount_boundary(config.card_mountpoint) is False
    assert discover_card(config) is None


def test_a_non_boundary_mountpoint_never_matches_the_root_filesystem(config, monkeypatch):
    """Without the boundary guard, "nothing mounted" would resolve to the Pi's
    own root filesystem UUID and the boot disk would be scanned."""
    root_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=1),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
                str(config.by_uuid_root / root_uuid): FakeStat(rdev=1),
            }
        ),
    )
    monkeypatch.setattr(sdcard_watcher.os, "listdir", lambda _path: [root_uuid])

    assert discover_card(config) is None


def test_a_mounted_card_returns_its_uuid_dynamically(config, monkeypatch):
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=DEVICE_A),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
                str(config.by_uuid_root / CARD_A): FakeStat(rdev=DEVICE_A),
                str(config.by_uuid_root / CARD_B): FakeStat(rdev=DEVICE_B),
            }
        ),
    )
    monkeypatch.setattr(sdcard_watcher.os, "listdir", lambda _path: [CARD_B, CARD_A])

    assert discover_card(config) == MOUNTED_A


def test_a_mounted_device_with_no_matching_uuid_is_not_a_card(config, monkeypatch):
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=DEVICE_A),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
                str(config.by_uuid_root / CARD_B): FakeStat(rdev=DEVICE_B),
            }
        ),
    )
    monkeypatch.setattr(sdcard_watcher.os, "listdir", lambda _path: [CARD_B])

    assert discover_card(config) is None


@pytest.mark.parametrize(
    "unsafe", ["", ".", "..", "has/slash", "has space", "a" * 65, "with_underscore", "uuid\n"]
)
def test_an_unsafe_uuid_is_never_adopted_as_a_card_identity(config, monkeypatch, unsafe):
    """An unusable UUID would become a queue path segment and a payload field,
    so such a card reads as absent rather than being sanitized."""
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=DEVICE_A),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
                str(config.by_uuid_root / unsafe): FakeStat(rdev=DEVICE_A),
            }
        ),
    )
    monkeypatch.setattr(sdcard_watcher.os, "listdir", lambda _path: [unsafe])

    assert discover_card(config) is None


def test_a_by_uuid_entry_that_is_not_a_block_device_is_ignored(config, monkeypatch):
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=DEVICE_A),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
                str(config.by_uuid_root / CARD_A): FakeStat(
                    rdev=DEVICE_A, mode=stat_module.S_IFREG
                ),
            }
        ),
    )
    monkeypatch.setattr(sdcard_watcher.os, "listdir", lambda _path: [CARD_A])

    assert discover_card(config) is None


def test_a_missing_by_uuid_directory_means_no_card(config, monkeypatch):
    monkeypatch.setattr(
        sdcard_watcher.os,
        "stat",
        stat_table(
            {
                str(config.card_mountpoint): FakeStat(dev=DEVICE_A),
                str(config.card_mountpoint.parent): FakeStat(dev=1),
            }
        ),
    )
    assert discover_card(config) is None


def test_a_missing_mountpoint_means_no_card(config):
    missing = replace(config, card_mountpoint=config.card_mountpoint / "gone")
    assert discover_card(missing) is None


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


def test_appledouble_sidecars_are_not_in_scope(card):
    """A card read on a Mac comes back with `._name.csv` resource forks.

    They are metadata, not flight data, and match every other rule, so only
    the dotfile check keeps them out of the ledger.
    """
    write_card_file(card, "log_20260202_125908_KPIH.csv")
    write_card_file(card, "._log_20260202_125908_KPIH.csv")
    write_card_file(card, ".DS_Store.csv")

    assert in_scope_files(card) == ["log_20260202_125908_KPIH.csv"]


# --- Scan subdirectory --------------------------------------------------------


def test_scan_root_defaults_to_the_card_root(config):
    assert config.scan_root == config.card_mountpoint


def test_scan_root_descends_into_the_configured_subdirectory(config):
    scoped = replace(config, card_scan_subdir="data_log")
    assert scoped.scan_root == config.card_mountpoint / "data_log"


def test_only_the_configured_subdirectory_is_scanned(config, card):
    """Garmin cards keep logs under `data_log/` and junk at the root."""
    (card / "data_log").mkdir()
    write_card_file(card, "data_log/log_20260202_125908_KPIH.csv")
    write_card_file(card, "data_log/._log_20260202_125908_KPIH.csv")
    write_card_file(card, "root-level.csv")

    scoped = replace(config, card_scan_subdir="data_log")

    assert in_scope_files(scoped.scan_root) == ["log_20260202_125908_KPIH.csv"]


def test_a_missing_scan_subdirectory_raises_card_error(config):
    scoped = replace(config, card_scan_subdir="data_log")
    with pytest.raises(CardError):
        in_scope_files(scoped.scan_root)


def test_scan_subdir_defaults_to_empty_when_unset():
    assert sdcard_watcher.resolve_card_scan_subdir({}) == ""
    assert sdcard_watcher.resolve_card_scan_subdir({"CARD_SCAN_SUBDIR": "  "}) == ""


def test_scan_subdir_strips_surrounding_slashes():
    assert sdcard_watcher.resolve_card_scan_subdir(
        {"CARD_SCAN_SUBDIR": "/data_log/"}
    ) == "data_log"


def test_scan_subdir_accepts_a_nested_relative_path():
    assert sdcard_watcher.resolve_card_scan_subdir(
        {"CARD_SCAN_SUBDIR": "Garmin/data_log"}
    ) == "Garmin/data_log"


@pytest.mark.parametrize(
    "value",
    ["..", "../outside", "data_log/../..", ".", "data\\log", "data_log\x00"],
)
def test_scan_subdir_refuses_to_escape_the_card(value):
    with pytest.raises(ConfigError):
        sdcard_watcher.resolve_card_scan_subdir({"CARD_SCAN_SUBDIR": value})


# --- Offline queueing ---------------------------------------------------------


def test_new_files_are_copied_into_the_cards_queue_and_recorded_pending(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    write_card_file(card, "logger-0001.csv", b"a,b\n1,2\n")
    write_card_file(card, "logger-0002.csv", b"c,d\n3,4\n")

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.rejected, result.failed) == (2, 0, 0)
    assert queued(ready) == ["logger-0001.csv", "logger-0002.csv"]
    assert (ready.pending_dir_for(CARD_A) / "logger-0001.csv").read_bytes() == b"a,b\n1,2\n"
    assert ledger.filenames_with_status(CARD_A, STATUS_PENDING) == [
        "logger-0001.csv",
        "logger-0002.csv",
    ]


def test_the_per_card_directory_is_created_on_demand(ready, card, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    assert queued_cards(ready) == []

    write_card_file(card, "a.csv")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert queued_cards(ready) == [CARD_A]


def test_ingestion_never_modifies_the_card(ready, card, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    source = write_card_file(card, "logger-0001.csv", b"a,b\n1,2\n")
    before = (source.read_bytes(), sorted(os.listdir(card)))

    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (source.read_bytes(), sorted(os.listdir(card))) == before


def test_out_of_scope_files_are_left_alone(ready, card, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    write_card_file(card, "notes.txt")
    (card / "nested").mkdir()

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.rejected, result.failed) == (0, 0, 0)
    assert queued_cards(ready) == []


def test_an_empty_csv_is_copied(ready, card, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    write_card_file(card, "empty.csv", b"")

    assert ingest_card(ready, ledger, watcher_logger, MOUNTED_A).copied == 1
    assert (ready.pending_dir_for(CARD_A) / "empty.csv").read_bytes() == b""


def test_a_file_exactly_at_the_limit_is_copied(ready, card, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "exact.csv", b"x" * 64)

    assert ingest_card(config, ledger, watcher_logger, MOUNTED_A).copied == 1
    assert (config.pending_dir_for(CARD_A) / "exact.csv").stat().st_size == 64


def test_a_file_one_byte_over_the_limit_is_rejected_and_not_queued(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "toobig.csv", b"x" * 65)

    result = ingest_card(config, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.rejected) == (0, 1)
    assert queued_cards(config) == []
    assert ledger.known_filenames(CARD_A) == set()


def test_files_in_the_scan_subdirectory_are_copied_into_the_queue(
    ready, card, ledger, watcher_logger, mounted
):
    """End to end for a Garmin-shaped card: logs nested, junk at the root."""
    mounted(MOUNTED_A)
    (card / "data_log").mkdir()
    write_card_file(card, "data_log/log_20260202_125908_KPIH.csv", b"x" * 10)
    write_card_file(card, "data_log/._log_20260202_125908_KPIH.csv", b"junk")
    write_card_file(card, "safetaxi.csv", b"x" * 10)
    config = replace(ready, card_scan_subdir="data_log")

    result = ingest_card(config, ledger, watcher_logger, MOUNTED_A)

    assert result.copied == 1
    assert queued(config) == ["log_20260202_125908_KPIH.csv"]
    assert (
        config.pending_dir_for(CARD_A) / "log_20260202_125908_KPIH.csv"
    ).stat().st_size == 10
    assert ledger.known_filenames(CARD_A) == {"log_20260202_125908_KPIH.csv"}


def test_an_unsafe_filename_is_rejected_and_not_queued(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    write_card_file(card, "bad\nname.csv")

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.rejected) == (0, 1)
    assert queued_cards(ready) == []


def test_a_rejected_file_does_not_stop_the_files_after_it(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    config = replace(ready, max_upload_bytes=64)
    write_card_file(card, "a-ok.csv", b"x" * 10)
    write_card_file(card, "b-toobig.csv", b"x" * 65)
    write_card_file(card, "c-ok.csv", b"x" * 10)

    result = ingest_card(config, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.rejected) == (2, 1)
    assert queued(config) == ["a-ok.csv", "c-ok.csv"]


def test_an_unreadable_file_is_reported_and_leaves_nothing_behind(
    ready, card, ledger, watcher_logger, mounted
):
    """Stands in for a hot-unplug mid-copy: no partial file is published, the
    ledger stays clean, and the next scan retries."""
    mounted(MOUNTED_A)
    source = write_card_file(card, "unreadable.csv", b"x" * 100)
    source.chmod(0o000)
    try:
        result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)
    finally:
        source.chmod(0o644)

    assert (result.copied, result.failed) == (0, 1)
    assert queued_cards(ready) == []
    assert ledger.known_filenames(CARD_A) == set()
    assert list(os.scandir(ready.temp_dir)) == []


def test_a_successful_copy_leaves_no_temporary_files(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    write_card_file(card, "a.csv")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)
    assert list(os.scandir(ready.temp_dir)) == []


def test_copy_into_queue_refuses_a_file_over_the_limit_while_reading(ready, card, mounted):
    mounted(MOUNTED_A)
    config = replace(ready, max_upload_bytes=8)
    source = write_card_file(card, "big.csv", b"x" * 9)

    with pytest.raises(FileTooLarge):
        copy_into_queue(source, config, MOUNTED_A)

    assert queued_cards(config) == []
    assert list(os.scandir(config.temp_dir)) == []


# --- Criterion 5: mount-change safety -----------------------------------------


def test_a_card_swap_during_a_copy_publishes_nothing(
    ready, card, ledger, watcher_logger, mounted
):
    """Card A's bytes must never be filed under card B's identity."""
    write_card_file(card, "logger-0001.csv", b"card a data\n")
    # Present when the file is checked, replaced by the time the copy finishes.
    mounted(MOUNTED_A, MOUNTED_B)

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert result.aborted is True
    assert result.copied == 0
    assert queued_cards(ready) == []
    assert ledger.known_filenames(CARD_A) == set()
    assert ledger.known_filenames(CARD_B) == set()
    assert list(os.scandir(ready.temp_dir)) == []


def test_a_card_removal_during_a_copy_publishes_nothing(
    ready, card, ledger, watcher_logger, mounted
):
    write_card_file(card, "logger-0001.csv")
    mounted(MOUNTED_A, None)

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert result.aborted is True
    assert queued_cards(ready) == []
    assert ledger.known_filenames(CARD_A) == set()


def test_a_device_number_change_alone_aborts_the_copy(
    ready, card, ledger, watcher_logger, mounted
):
    """Same UUID, different device: a re-plug mid-copy is still a change."""
    write_card_file(card, "logger-0001.csv")
    mounted(MOUNTED_A, CardIdentity(uuid=CARD_A, device_number=DEVICE_B))

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert result.aborted is True
    assert queued_cards(ready) == []


def test_copy_into_queue_raises_card_changed_and_cleans_up(ready, card, mounted):
    mounted(MOUNTED_B)
    source = write_card_file(card, "logger-0001.csv")

    with pytest.raises(CardChanged):
        copy_into_queue(source, ready, MOUNTED_A)

    assert queued_cards(ready) == []
    assert list(os.scandir(ready.temp_dir)) == []


def test_a_swap_between_files_stops_the_scan_before_the_next_copy(
    ready, card, ledger, watcher_logger, mounted
):
    write_card_file(card, "a.csv")
    write_card_file(card, "b.csv")
    # a.csv: pre-check A, post-copy A. b.csv: pre-check B -> stop.
    mounted(MOUNTED_A, MOUNTED_A, MOUNTED_B)

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.aborted) == (1, True)
    assert queued(ready) == ["a.csv"]
    assert ledger.known_filenames(CARD_A) == {"a.csv"}


def test_the_aborted_file_is_queued_on_a_later_scan(
    ready, card, ledger, watcher_logger, mounted
):
    write_card_file(card, "logger-0001.csv", b"card a data\n")
    mounted(MOUNTED_A, MOUNTED_B)
    assert ingest_card(ready, ledger, watcher_logger, MOUNTED_A).aborted is True

    mounted(MOUNTED_A)
    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert result.copied == 1
    assert (ready.pending_dir_for(CARD_A) / "logger-0001.csv").read_bytes() == b"card a data\n"


# --- Criterion 6: per-card local identity -------------------------------------


def test_the_same_filename_from_two_cards_produces_two_queued_files(
    ready, card, ledger, watcher_logger, mounted
):
    """The motivating scenario: both files must survive and both must ship."""
    write_card_file(card, "logger-0001.csv", b"card a data\n")
    mounted(MOUNTED_A)
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    # Card A removed, card B inserted with a same-named, different file.
    (card / "logger-0001.csv").write_bytes(b"card b data\n")
    mounted(MOUNTED_B)
    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_B)

    assert result.copied == 1
    assert queued_cards(ready) == sorted([CARD_A, CARD_B])
    assert (ready.pending_dir_for(CARD_A) / "logger-0001.csv").read_bytes() == b"card a data\n"
    assert (ready.pending_dir_for(CARD_B) / "logger-0001.csv").read_bytes() == b"card b data\n"
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_PENDING
    assert ledger.status_of(CARD_B, "logger-0001.csv") == STATUS_PENDING


def test_reinserting_a_card_after_upload_copies_nothing(
    ready, card, ledger, watcher_logger, mounted
):
    """The whole history is on the card, and none of it is re-copied."""
    mounted(MOUNTED_A)
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    # The uploader delivers the file and clears the queued copy.
    ledger.mark_uploaded(CARD_A, "logger-0001.csv")
    (ready.pending_dir_for(CARD_A) / "logger-0001.csv").unlink()

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.skipped_known) == (0, 1)
    assert queued(ready) == []
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_UPLOADED


def test_reinserting_a_card_before_upload_makes_no_duplicate_copy(
    ready, card, ledger, watcher_logger, mounted
):
    """A `pending` row is honoured, so the queue keeps one copy."""
    mounted(MOUNTED_A)
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.skipped_known) == (0, 1)
    assert queued(ready) == ["logger-0001.csv"]
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_PENDING


def test_reinsertion_with_new_files_copies_only_the_new_ones(
    ready, card, ledger, watcher_logger, mounted
):
    mounted(MOUNTED_A)
    write_card_file(card, "logger-0001.csv")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)
    ledger.mark_uploaded(CARD_A, "logger-0001.csv")
    (ready.pending_dir_for(CARD_A) / "logger-0001.csv").unlink()

    # The logger appends nothing; it writes a new, distinct name.
    write_card_file(card, "logger-0002.csv")
    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_A)

    assert (result.copied, result.skipped_known) == (1, 1)
    assert queued(ready) == ["logger-0002.csv"]


def test_dedup_from_one_card_never_suppresses_another_cards_file(
    ready, card, ledger, watcher_logger, mounted
):
    """Card A's uploaded filename must not make card B's look already known."""
    mounted(MOUNTED_A)
    write_card_file(card, "logger-0001.csv", b"card a data\n")
    ingest_card(ready, ledger, watcher_logger, MOUNTED_A)
    ledger.mark_uploaded(CARD_A, "logger-0001.csv")
    (ready.pending_dir_for(CARD_A) / "logger-0001.csv").unlink()

    (card / "logger-0001.csv").write_bytes(b"card b data\n")
    mounted(MOUNTED_B)
    result = ingest_card(ready, ledger, watcher_logger, MOUNTED_B)

    assert (result.copied, result.skipped_known) == (1, 0)
    assert (ready.pending_dir_for(CARD_B) / "logger-0001.csv").read_bytes() == b"card b data\n"


# --- Stale temporary files ----------------------------------------------------


def test_stale_temporary_copies_are_cleared_at_start_up(ready, watcher_logger):
    (ready.temp_dir / ".copy-abc.part").write_bytes(b"half a file")

    assert clear_stale_temp_files(ready, watcher_logger) == 1
    assert list(os.scandir(ready.temp_dir)) == []


def test_clearing_temporary_copies_never_touches_the_queue(ready, watcher_logger):
    ready.pending_dir_for(CARD_A).mkdir(parents=True)
    (ready.pending_dir_for(CARD_A) / "a.csv").write_bytes(b"real")

    clear_stale_temp_files(ready, watcher_logger)

    assert queued(ready) == ["a.csv"]


def test_prepare_directories_is_idempotent(config, ledger):
    prepare_directories(config)
    prepare_directories(config)
    assert config.pending_dir.is_dir() and config.temp_dir.is_dir()


# --- The watch loop -----------------------------------------------------------


def test_an_absent_card_is_not_scanned(ready, ledger, watcher_logger, mounted, caplog):
    mounted(None)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state) is False
    assert "no card is mounted" in caplog.text


def test_a_mount_without_a_resolvable_uuid_is_not_scanned(
    ready, card, ledger, watcher_logger, monkeypatch, caplog
):
    """Criterion 4: a mounted device whose UUID does not resolve safely is not
    ingested, and nothing is written."""
    write_card_file(card, "a.csv")
    monkeypatch.setattr(sdcard_watcher, "discover_card", lambda _config: None)
    monkeypatch.setattr(sdcard_watcher, "is_mount_boundary", lambda _mountpoint: True)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state) is False
    assert queued_cards(ready) == []
    assert ledger.counts() == {}
    assert "no safe card UUID" in caplog.text


def test_a_mounted_card_is_scanned_on_every_tick(ready, card, ledger, watcher_logger, mounted):
    """Phase 3 drops insertion-transition state: each tick re-scans, and ledger
    dedup makes the repeat free."""
    mounted(MOUNTED_A)
    write_card_file(card, "a.csv")
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state) is True
    assert queued(ready) == ["a.csv"]

    # A file added while the card stays in place is picked up without a re-plug.
    write_card_file(card, "b.csv")
    assert check_once(ready, ledger, watcher_logger, state) is True
    assert queued(ready) == ["a.csv", "b.csv"]


def test_swapping_cards_between_ticks_ingests_both(
    ready, card, ledger, watcher_logger, mounted
):
    state = StateLogger(watcher_logger, 300.0)
    write_card_file(card, "logger-0001.csv", b"card a data\n")
    mounted(MOUNTED_A)
    check_once(ready, ledger, watcher_logger, state)

    (card / "logger-0001.csv").write_bytes(b"card b data\n")
    mounted(MOUNTED_B)
    check_once(ready, ledger, watcher_logger, state)

    assert queued(ready, CARD_A) == ["logger-0001.csv"]
    assert queued(ready, CARD_B) == ["logger-0001.csv"]


def test_a_scan_failure_does_not_crash_the_loop(
    ready, ledger, watcher_logger, mounted, monkeypatch, caplog
):
    mounted(MOUNTED_A)

    def broken(*args, **kwargs):
        raise CardError("cannot list the mountpoint")

    monkeypatch.setattr(sdcard_watcher, "ingest_card", broken)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state) is False
    assert "cannot list the mountpoint" in caplog.text


def test_an_unexpected_ingest_error_does_not_crash_the_loop(
    ready, ledger, watcher_logger, mounted, monkeypatch, caplog
):
    mounted(MOUNTED_A)

    def explode(*args, **kwargs):
        raise ValueError("something unforeseen")

    monkeypatch.setattr(sdcard_watcher, "ingest_card", explode)
    state = StateLogger(watcher_logger, 300.0)

    assert check_once(ready, ledger, watcher_logger, state) is False
    assert "unexpected error ingesting card" in caplog.text


def test_run_stops_after_the_requested_iterations(ready, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    state = StateLogger(watcher_logger, 300.0)
    quick = replace(ready, scan_interval_seconds=0.01)

    iterations = run(
        quick, ledger, watcher_logger, state, threading.Event(), max_iterations=2
    )

    assert iterations == 2


def test_run_does_nothing_when_already_stopped(ready, ledger, watcher_logger, mounted):
    mounted(MOUNTED_A)
    stop_event = threading.Event()
    stop_event.set()

    iterations = run(ready, ledger, watcher_logger, StateLogger(watcher_logger, 300.0), stop_event)

    assert iterations == 0


def test_run_ingests_a_card_that_was_already_mounted_at_start_up(
    ready, card, ledger, watcher_logger, mounted
):
    """Criterion 3: a card present at boot needs no insertion event."""
    mounted(MOUNTED_A)
    write_card_file(card, "a.csv")

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
