"""SD card mounter tests — PRD criteria 1 (candidate filtering), 2 (mount
reconciliation), and 3 (a card already present at boot)."""

import logging
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import sdcard_mounter
from sdcard_mounter import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_ENUMERATION_FAILED,
    OUTCOME_IDLE,
    OUTCOME_MOUNT_FAILED,
    OUTCOME_MOUNTED,
    OUTCOME_UNCHANGED,
    OUTCOME_UNMOUNT_FAILED,
    OUTCOME_UNMOUNTED,
    BlockDevice,
    MounterConfig,
    MounterError,
    list_block_devices,
    mount_options,
    mounted_source,
    parse_lsblk,
    reconcile,
    root_disk_name,
    run,
    select_candidates,
)
from uploader import ConfigError, StateLogger

CARD_UUID = "1234-ABCD"
ROOT_SOURCE = "/dev/mmcblk0p2"


def lsblk_line(
    path,
    name=None,
    kind="part",
    removable="1",
    hotplug="1",
    filesystem="vfat",
    uuid=CARD_UUID,
    parent="",
):
    fields = [
        ("PATH", path),
        ("NAME", name if name is not None else path.rsplit("/", 1)[-1]),
        ("TYPE", kind),
        ("RM", removable),
        ("HOTPLUG", hotplug),
        ("FSTYPE", filesystem),
        ("UUID", uuid),
        ("PKNAME", parent),
    ]
    return " ".join(f'{key}="{value}"' for key, value in fields)


# The Pi's own boot media: a disk with a vfat boot partition and the ext4 root.
PI_DISK = [
    lsblk_line("/dev/mmcblk0", kind="disk", removable="0", hotplug="0", filesystem="", uuid=""),
    lsblk_line(
        "/dev/mmcblk0p1",
        removable="0",
        hotplug="0",
        filesystem="vfat",
        uuid="AAAA-BBBB",
        parent="mmcblk0",
    ),
    lsblk_line(
        "/dev/mmcblk0p2",
        removable="0",
        hotplug="0",
        filesystem="ext4",
        uuid="11111111-2222-3333-4444-555555555555",
        parent="mmcblk0",
    ),
]

# A USB card reader holding one vfat card.
CARD_READER = [
    lsblk_line("/dev/sda", kind="disk", removable="1", filesystem="", uuid=""),
    lsblk_line("/dev/sda1", parent="sda"),
]


class FakeRunner:
    """Scripts the four external commands the mounter runs."""

    def __init__(
        self,
        devices=None,
        root_source=ROOT_SOURCE,
        mounted=None,
        lsblk_status=0,
        mount_status=0,
        umount_status=0,
        lazy_umount_status=0,
        raises=None,
    ):
        self.devices = list(PI_DISK if devices is None else devices)
        self.root_source = root_source
        self.mounted = mounted
        self.lsblk_status = lsblk_status
        self.mount_status = mount_status
        self.umount_status = umount_status
        self.lazy_umount_status = lazy_umount_status
        self.raises = raises
        self.calls = []

    def __call__(self, argv, capture_output=None, text=None, timeout=None):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises

        if argv[0] == "lsblk":
            return self._result(self.lsblk_status, "\n".join(self.devices) + "\n")
        if argv[0] == "findmnt":
            if "--mountpoint" in argv:
                if self.mounted is None:
                    return self._result(1, "")
                return self._result(0, self.mounted + "\n")
            return self._result(0, f"{self.root_source}\n" if self.root_source else "")
        if argv[0] == "umount":
            lazy = "-l" in argv
            status = self.lazy_umount_status if lazy else self.umount_status
            if status == 0:
                self.mounted = None
            return self._result(status, "" if status == 0 else "target is busy")
        if argv[0] == "mount":
            if self.mount_status == 0:
                self.mounted = argv[-2]
            return self._result(
                self.mount_status, "" if self.mount_status == 0 else "wrong fs type"
            )
        raise AssertionError(f"unexpected command {argv!r}")  # pragma: no cover

    @staticmethod
    def _result(returncode, stdout):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    @property
    def commands(self):
        return [argv[0] for argv in self.calls]

    def command(self, name):
        """The last invocation of `name`, or None."""
        matches = [argv for argv in self.calls if argv[0] == name]
        return matches[-1] if matches else None


@pytest.fixture
def mountpoint(tmp_path):
    path = tmp_path / "mnt" / "sdcard"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def config(mountpoint):
    return MounterConfig(card_mountpoint=mountpoint, mount_interval_seconds=0.01)


@pytest.fixture
def mounter_logger(caplog):
    logger = logging.getLogger("piuploader.mounter.test")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="piuploader.mounter.test")
    return logger


@pytest.fixture
def state(mounter_logger):
    return StateLogger(mounter_logger, 300.0)


def candidates_for(devices, root_disk="mmcblk0"):
    return select_candidates(parse_lsblk("\n".join(devices)), root_disk)


def rejection_reasons(devices, root_disk="mmcblk0"):
    _, rejections = candidates_for(devices, root_disk)
    return {device.path: reason for device, reason in rejections}


# --- Configuration ------------------------------------------------------------


def test_config_from_env_applies_documented_defaults():
    config = MounterConfig.from_env({"CARD_MOUNTPOINT": "/mnt/sdcard"})

    assert config.card_mountpoint == Path("/mnt/sdcard")
    assert config.mount_interval_seconds == 2.0
    assert config.error_log_repeat_seconds == 300.0
    assert config.log_path == Path("/var/log/piuploader/sdcard-mounter.log")


def test_config_reads_the_mount_interval():
    config = MounterConfig.from_env(
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_MOUNT_INTERVAL_SECONDS": "5"}
    )
    assert config.mount_interval_seconds == 5.0


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CARD_MOUNTPOINT": ""},
        {"CARD_MOUNTPOINT": "relative/path"},
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_MOUNT_INTERVAL_SECONDS": "0"},
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_MOUNT_INTERVAL_SECONDS": "-1"},
        {"CARD_MOUNTPOINT": "/mnt/sdcard", "CARD_MOUNT_INTERVAL_SECONDS": "soon"},
    ],
)
def test_invalid_config_is_rejected(env):
    with pytest.raises(ConfigError):
        MounterConfig.from_env(env)


# --- lsblk parsing ------------------------------------------------------------


def test_lsblk_pairs_are_parsed_into_devices():
    devices = parse_lsblk(lsblk_line("/dev/sda1", parent="sda"))

    assert devices == [
        BlockDevice(
            path="/dev/sda1",
            name="sda1",
            kind="part",
            removable=True,
            hotplug=True,
            filesystem="vfat",
            uuid=CARD_UUID,
            parent="sda",
        )
    ]


def test_blank_and_unparsable_lines_are_ignored():
    assert parse_lsblk("\n   \nnot a pair line\n") == []


def test_a_failing_lsblk_raises_rather_than_reporting_no_devices():
    """Reporting "no devices" would unmount a perfectly good card."""
    with pytest.raises(MounterError):
        list_block_devices(runner=FakeRunner(lsblk_status=1))


def test_a_missing_lsblk_binary_raises():
    with pytest.raises(MounterError):
        list_block_devices(runner=FakeRunner(raises=FileNotFoundError("lsblk")))


def test_the_root_disk_is_resolved_through_the_parent_chain():
    runner = FakeRunner(devices=PI_DISK + CARD_READER)
    devices = list_block_devices(runner=runner)

    assert root_disk_name(devices, runner=runner) == "mmcblk0"


def test_an_undeterminable_root_disk_is_reported_as_empty():
    runner = FakeRunner(devices=PI_DISK, root_source="")
    assert root_disk_name(list_block_devices(runner=runner), runner=runner) == ""


def test_an_undeterminable_root_disk_is_said_out_loud(config, state, caplog):
    """Filter 3 silently stops applying; that must be visible, not hidden."""
    runner = FakeRunner(devices=PI_DISK + CARD_READER, root_source="")

    reconcile(config, state, runner=runner)

    assert "root-disk exclusion is inactive" in caplog.text


# --- Criterion 1: candidate filtering -----------------------------------------


def test_a_removable_partition_with_a_supported_filesystem_is_eligible():
    candidates, _ = candidates_for(PI_DISK + CARD_READER)

    assert [device.path for device in candidates] == ["/dev/sda1"]


def test_a_removable_whole_disk_filesystem_is_eligible():
    """A card formatted without a partition table, e.g. /dev/sda directly."""
    whole_disk = [lsblk_line("/dev/sda", kind="disk", filesystem="exfat")]
    candidates, _ = candidates_for(PI_DISK + whole_disk)

    assert [device.path for device in candidates] == ["/dev/sda"]


def test_a_whole_disk_filesystem_shadowed_by_a_partition_is_rejected():
    """Two plausible readings of the same card: neither is trusted."""
    shadowed = [
        lsblk_line("/dev/sda", kind="disk", filesystem="vfat", uuid="DEAD-BEEF"),
        lsblk_line("/dev/sda1", parent="sda"),
    ]
    candidates, _ = candidates_for(PI_DISK + shadowed)

    assert [device.path for device in candidates] == ["/dev/sda1"]
    assert "shadowed" in rejection_reasons(PI_DISK + shadowed)["/dev/sda"]


def test_the_root_and_boot_partitions_are_never_candidates():
    candidates, _ = candidates_for(PI_DISK)
    assert candidates == []


def test_a_removable_partition_on_the_root_disk_is_rejected():
    """Some Pi setups report the boot media as hotplug; it is still the root
    disk and must never be treated as a data card."""
    devices = PI_DISK + [
        lsblk_line("/dev/mmcblk0p3", removable="1", filesystem="vfat", parent="mmcblk0")
    ]
    candidates, _ = candidates_for(devices)

    assert candidates == []
    assert "same physical disk as /" in rejection_reasons(devices)["/dev/mmcblk0p3"]


def test_a_non_removable_non_hotplug_device_is_rejected():
    devices = [lsblk_line("/dev/sdb1", removable="0", hotplug="0", parent="sdb")]
    candidates, _ = candidates_for(devices)

    assert candidates == []
    assert "neither removable nor hotplug" in rejection_reasons(devices)["/dev/sdb1"]


def test_a_hotplug_only_device_is_accepted():
    """Some USB card readers report RM=0 and only set HOTPLUG=1."""
    candidates, _ = candidates_for([lsblk_line("/dev/sdb1", removable="0", hotplug="1")])
    assert [device.path for device in candidates] == ["/dev/sdb1"]


@pytest.mark.parametrize("filesystem", ["ntfs", "btrfs", "xfs", "squashfs", "iso9660"])
def test_an_unsupported_filesystem_is_rejected(filesystem):
    devices = [lsblk_line("/dev/sdb1", filesystem=filesystem)]
    candidates, _ = candidates_for(devices)

    assert candidates == []
    assert "unsupported filesystem" in rejection_reasons(devices)["/dev/sdb1"]


@pytest.mark.parametrize(
    "filesystem", ["vfat", "exfat", "ext2", "ext3", "ext4"]
)
def test_every_supported_filesystem_is_accepted(filesystem):
    candidates, _ = candidates_for([lsblk_line("/dev/sdb1", filesystem=filesystem)])
    assert len(candidates) == 1


@pytest.mark.parametrize(
    "uuid", ["", "has space", "has/slash", "a" * 65, "under_score", "dot.separated"]
)
def test_a_missing_or_unsafe_uuid_is_rejected(uuid):
    devices = [lsblk_line("/dev/sdb1", uuid=uuid)]
    candidates, _ = candidates_for(devices)

    assert candidates == []
    assert "UUID" in rejection_reasons(devices)["/dev/sdb1"]


@pytest.mark.parametrize("uuid", ["A", "1234-ABCD", "a" * 64, "11111111-2222-3333-4444-555555555555"])
def test_uuid_boundaries_are_accepted(uuid):
    candidates, _ = candidates_for([lsblk_line("/dev/sdb1", uuid=uuid)])
    assert len(candidates) == 1


def test_a_device_without_a_filesystem_is_not_even_a_candidate():
    """A bare disk is not "rejected"; it is simply not a card."""
    candidates, rejections = candidates_for(
        [lsblk_line("/dev/sdb", kind="disk", filesystem="", uuid="")]
    )
    assert (candidates, rejections) == ([], [])


def test_a_loop_device_with_a_filesystem_is_rejected():
    devices = [lsblk_line("/dev/loop0", kind="loop", filesystem="ext4")]
    candidates, _ = candidates_for(devices)

    assert candidates == []
    assert "device type loop" in rejection_reasons(devices)["/dev/loop0"]


def test_rejection_reasons_are_logged_with_the_device_and_filesystem(config, state, caplog):
    runner = FakeRunner(devices=PI_DISK + [lsblk_line("/dev/sdb1", filesystem="ntfs")])

    reconcile(config, state, runner=runner)

    assert "not mounting:" in caplog.text
    assert "/dev/sdb1" in caplog.text
    assert "fstype=ntfs" in caplog.text
    assert "unsupported filesystem" in caplog.text


def test_a_newly_rejected_device_is_reported_without_waiting_for_the_repeat_window(
    config, mounter_logger, caplog
):
    """Rate limiting must not hide a device that has just appeared."""
    clock = _FakeClock()
    state = StateLogger(mounter_logger, 300.0, clock=clock)
    runner = FakeRunner(devices=PI_DISK)
    reconcile(config, state, runner=runner)

    caplog.clear()
    clock.advance(2.0)
    runner.devices = PI_DISK + [lsblk_line("/dev/sdb1", filesystem="ntfs")]
    reconcile(config, state, runner=runner)

    assert "unsupported filesystem" in caplog.text


# --- Criterion 2: mount reconciliation ----------------------------------------


def test_one_candidate_is_mounted_read_only(config, state):
    runner = FakeRunner(devices=PI_DISK + CARD_READER)

    assert reconcile(config, state, runner=runner) == OUTCOME_MOUNTED
    assert runner.command("mount") == [
        "mount",
        "-t",
        "vfat",
        "-o",
        "ro,nodev,nosuid,noexec",
        "/dev/sda1",
        str(config.card_mountpoint),
    ]


@pytest.mark.parametrize(
    "filesystem,expected",
    [
        ("vfat", "ro,nodev,nosuid,noexec"),
        ("exfat", "ro,nodev,nosuid,noexec"),
        ("ext2", "ro,nodev,nosuid,noexec"),
        # noload: mounting must never replay a journal onto the card.
        ("ext3", "ro,nodev,nosuid,noexec,noload"),
        ("ext4", "ro,nodev,nosuid,noexec,noload"),
    ],
)
def test_mount_options_are_filesystem_specific(filesystem, expected):
    assert mount_options(filesystem) == expected


@pytest.mark.parametrize("filesystem", ["ext3", "ext4"])
def test_a_journalled_card_is_mounted_with_noload(config, state, filesystem):
    runner = FakeRunner(
        devices=PI_DISK + [lsblk_line("/dev/sda1", filesystem=filesystem, uuid="abc-123")]
    )

    reconcile(config, state, runner=runner)

    assert "noload" in runner.command("mount")[4]


def test_an_already_correct_mount_is_left_alone(config, state):
    runner = FakeRunner(devices=PI_DISK + CARD_READER, mounted="/dev/sda1")

    assert reconcile(config, state, runner=runner) == OUTCOME_UNCHANGED
    assert "mount" not in runner.commands
    assert "umount" not in runner.commands


def test_a_stale_mount_is_cleared_when_no_card_is_present(config, state):
    runner = FakeRunner(devices=PI_DISK, mounted="/dev/sda1")

    assert reconcile(config, state, runner=runner) == OUTCOME_UNMOUNTED
    assert runner.command("umount") == ["umount", str(config.card_mountpoint)]
    assert "mount" not in runner.commands


def test_no_card_and_no_mount_changes_nothing(config, state):
    runner = FakeRunner(devices=PI_DISK)

    assert reconcile(config, state, runner=runner) == OUTCOME_IDLE
    assert runner.commands.count("umount") == 0
    assert "mount" not in runner.commands


def test_a_different_card_replaces_the_current_mount(config, state):
    """The operator swapped cards: unmount the old one, then mount the new."""
    runner = FakeRunner(
        devices=PI_DISK + [lsblk_line("/dev/sdb1", uuid="5678-EF01")], mounted="/dev/sda1"
    )

    assert reconcile(config, state, runner=runner) == OUTCOME_MOUNTED
    assert runner.commands.index("umount") < runner.commands.index("mount")
    assert runner.command("mount")[-2] == "/dev/sdb1"


def test_more_than_one_eligible_card_changes_nothing(config, state, caplog):
    runner = FakeRunner(
        devices=PI_DISK
        + [lsblk_line("/dev/sda1", uuid="1111-AAAA"), lsblk_line("/dev/sdb1", uuid="2222-BBBB")],
        mounted="/dev/sda1",
    )

    assert reconcile(config, state, runner=runner) == OUTCOME_AMBIGUOUS
    assert "mount" not in runner.commands
    assert "umount" not in runner.commands
    assert "more than one eligible card" in caplog.text


def test_ambiguous_devices_do_not_disturb_an_existing_mount(config, state):
    runner = FakeRunner(
        devices=PI_DISK
        + [lsblk_line("/dev/sda1", uuid="1111-AAAA"), lsblk_line("/dev/sdb1", uuid="2222-BBBB")],
        mounted="/dev/sda1",
    )
    reconcile(config, state, runner=runner)

    assert runner.mounted == "/dev/sda1"


# --- Criterion 2: failures are retried, never fatal ---------------------------


def test_a_failed_mount_is_logged_and_retried_on_the_next_tick(config, state, caplog):
    runner = FakeRunner(devices=PI_DISK + CARD_READER, mount_status=32)

    assert reconcile(config, state, runner=runner) == OUTCOME_MOUNT_FAILED
    assert "could not mount" in caplog.text

    runner.mount_status = 0
    assert reconcile(config, state, runner=runner) == OUTCOME_MOUNTED


def test_a_failed_unmount_falls_back_to_a_lazy_unmount(config, state):
    """A yanked card leaves a dead mount that a plain umount can refuse."""
    runner = FakeRunner(devices=PI_DISK, mounted="/dev/sda1", umount_status=32)

    assert reconcile(config, state, runner=runner) == OUTCOME_UNMOUNTED
    assert ["umount", "-l", str(config.card_mountpoint)] in runner.calls


def test_an_unmount_that_fails_entirely_is_logged_and_retried(config, state, caplog):
    runner = FakeRunner(
        devices=PI_DISK, mounted="/dev/sda1", umount_status=32, lazy_umount_status=32
    )

    assert reconcile(config, state, runner=runner) == OUTCOME_UNMOUNT_FAILED
    assert "could not unmount" in caplog.text

    runner.umount_status = 0
    assert reconcile(config, state, runner=runner) == OUTCOME_UNMOUNTED


def test_a_stale_mount_that_cannot_be_cleared_blocks_the_new_mount(config, state):
    """Mounting over a mount would hide the old one instead of replacing it."""
    runner = FakeRunner(
        devices=PI_DISK + [lsblk_line("/dev/sdb1", uuid="5678-EF01")],
        mounted="/dev/sda1",
        umount_status=32,
        lazy_umount_status=32,
    )

    assert reconcile(config, state, runner=runner) == OUTCOME_UNMOUNT_FAILED
    assert "mount" not in runner.commands


def test_a_failing_lsblk_changes_no_mount_state(config, state, caplog):
    runner = FakeRunner(devices=PI_DISK + CARD_READER, mounted="/dev/sda1", lsblk_status=1)

    assert reconcile(config, state, runner=runner) == OUTCOME_ENUMERATION_FAILED
    assert "umount" not in runner.commands
    assert runner.mounted == "/dev/sda1"
    assert "lsblk failed" in caplog.text


def test_a_mountpoint_that_cannot_be_created_is_reported(config, state, tmp_path, caplog):
    blocked = tmp_path / "file-in-the-way"
    blocked.write_bytes(b"")
    runner = FakeRunner(devices=PI_DISK + CARD_READER)

    outcome = reconcile(replace(config, card_mountpoint=blocked / "sdcard"), state, runner=runner)

    assert outcome == OUTCOME_MOUNT_FAILED
    assert "could not mount" in caplog.text


def test_mounted_source_reports_nothing_when_the_mountpoint_is_empty(mountpoint):
    assert mounted_source(mountpoint, runner=FakeRunner(mounted=None)) is None


def test_mounted_source_reports_the_current_device(mountpoint):
    assert mounted_source(mountpoint, runner=FakeRunner(mounted="/dev/sda1")) == "/dev/sda1"


# --- Criterion 3: a card already present at boot ------------------------------


def test_the_first_reconciliation_mounts_a_card_that_is_already_inserted(config, state):
    """No add event ever arrives for a card present at boot; polling covers it."""
    runner = FakeRunner(devices=PI_DISK + CARD_READER)

    iterations = run(config, state, threading.Event(), runner=runner, max_iterations=1)

    assert iterations == 1
    assert runner.mounted == "/dev/sda1"


def test_the_loop_stops_when_asked(config, state):
    stop_event = threading.Event()
    stop_event.set()

    assert run(config, state, stop_event, runner=FakeRunner(), max_iterations=5) == 0


def test_the_loop_survives_repeated_failures(config, state):
    runner = FakeRunner(devices=PI_DISK + CARD_READER, mount_status=32)

    assert run(config, state, threading.Event(), runner=runner, max_iterations=3) == 3


def test_an_insertion_between_ticks_is_picked_up(config, state):
    runner = FakeRunner(devices=PI_DISK)
    run(config, state, threading.Event(), runner=runner, max_iterations=1)
    assert runner.mounted is None

    runner.devices = PI_DISK + CARD_READER
    run(config, state, threading.Event(), runner=runner, max_iterations=1)

    assert runner.mounted == "/dev/sda1"


def test_a_removal_between_ticks_clears_the_mount(config, state):
    runner = FakeRunner(devices=PI_DISK + CARD_READER)
    run(config, state, threading.Event(), runner=runner, max_iterations=1)
    assert runner.mounted == "/dev/sda1"

    runner.devices = PI_DISK
    run(config, state, threading.Event(), runner=runner, max_iterations=1)

    assert runner.mounted is None


def test_repeated_unchanged_states_are_rate_limited(config, mounter_logger, caplog):
    """An idle Pi must not fill the journal at one line every two seconds."""
    clock = _FakeClock()
    state = StateLogger(mounter_logger, 300.0, clock=clock)
    runner = FakeRunner(devices=PI_DISK)

    for _ in range(10):
        reconcile(config, state, runner=runner)
        clock.advance(2.0)

    assert len([r for r in caplog.records if "no eligible card" in r.message]) == 1


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
