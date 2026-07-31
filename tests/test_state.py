"""Ledger tests — the per-card dedup record behind PRD criteria 6 and 11."""

import sqlite3

import pytest

from state import (
    SCHEMA_ABSENT,
    SCHEMA_LEGACY,
    SCHEMA_PHASE3,
    SCHEMA_UNREADABLE,
    STATUS_PENDING,
    STATUS_UPLOADED,
    Ledger,
    rfc3339_utc,
    schema_state,
    summarize,
)

CARD_A = "1234-ABCD"
CARD_B = "5678-EF01"

LEGACY_SCHEMA = """
CREATE TABLE files (
  filename TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
  discovered_at TEXT NOT NULL
);
"""


@pytest.fixture
def ledger(tmp_path):
    instance = Ledger(tmp_path / "state.db")
    instance.initialize()
    return instance


def test_initialize_creates_missing_parent_directories(tmp_path):
    nested = tmp_path / "var" / "lib" / "piuploader" / "state.db"
    Ledger(nested).initialize()
    assert nested.exists()


def test_initialize_is_idempotent(tmp_path):
    instance = Ledger(tmp_path / "state.db")
    instance.initialize()
    instance.record_pending(CARD_A, "a.csv")
    instance.initialize()
    assert instance.status_of(CARD_A, "a.csv") == STATUS_PENDING


def test_recording_a_new_file_reports_that_it_was_new(ledger):
    assert ledger.record_pending(CARD_A, "a.csv") is True
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_PENDING


def test_recording_a_known_file_again_is_a_no_op(ledger):
    ledger.record_pending(CARD_A, "a.csv", "2026-07-29T10:00:00Z")

    assert ledger.record_pending(CARD_A, "a.csv", "2026-07-29T11:00:00Z") is False
    # The original discovery time is preserved.
    with sqlite3.connect(ledger.database_path) as connection:
        discovered_at = connection.execute(
            "SELECT discovered_at FROM files WHERE card_uuid = ? AND filename = ?",
            (CARD_A, "a.csv"),
        ).fetchone()[0]
    assert discovered_at == "2026-07-29T10:00:00Z"


def test_an_uploaded_file_is_never_downgraded_to_pending(ledger):
    """Re-inserting a card must not re-queue a delivered file."""
    ledger.record_pending(CARD_A, "a.csv")
    ledger.mark_uploaded(CARD_A, "a.csv")

    assert ledger.record_pending(CARD_A, "a.csv") is False
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_UPLOADED


def test_known_filenames_covers_both_statuses(ledger):
    """The watcher skips a filename in either status."""
    ledger.record_pending(CARD_A, "pending.csv")
    ledger.record_pending(CARD_A, "done.csv")
    ledger.mark_uploaded(CARD_A, "done.csv")

    assert ledger.known_filenames(CARD_A) == {"pending.csv", "done.csv"}


def test_mark_uploaded_inserts_a_row_that_was_never_recorded_pending(ledger):
    """Covers a crash between the queue rename and the ledger write: the
    uploader must still be able to close the file out."""
    ledger.mark_uploaded(CARD_A, "orphan.csv")

    assert ledger.status_of(CARD_A, "orphan.csv") == STATUS_UPLOADED


def test_status_of_an_unknown_file_is_none(ledger):
    assert ledger.status_of(CARD_A, "missing.csv") is None


def test_filenames_with_status_is_sorted_and_card_scoped(ledger):
    for name in ("c.csv", "a.csv", "b.csv"):
        ledger.record_pending(CARD_A, name)
    ledger.mark_uploaded(CARD_A, "b.csv")
    ledger.record_pending(CARD_B, "z.csv")

    assert ledger.filenames_with_status(CARD_A, STATUS_PENDING) == ["a.csv", "c.csv"]
    assert ledger.filenames_with_status(CARD_A, STATUS_UPLOADED) == ["b.csv"]
    assert ledger.filenames_with_status(CARD_B, STATUS_PENDING) == ["z.csv"]


def test_counts_aggregate_across_every_card(ledger):
    ledger.record_pending(CARD_A, "a.csv")
    ledger.record_pending(CARD_B, "b.csv")
    ledger.mark_uploaded(CARD_B, "b.csv")

    assert ledger.counts() == {STATUS_PENDING: 1, STATUS_UPLOADED: 1}


def test_summarize_reports_zero_for_a_missing_status():
    assert summarize({STATUS_UPLOADED: 2}) == "pending=0 uploaded=2"


def test_rows_survive_reopening_the_database(tmp_path):
    """Two processes share this file, and it must outlive both."""
    path = tmp_path / "state.db"
    first = Ledger(path)
    first.initialize()
    first.record_pending(CARD_A, "a.csv")

    second = Ledger(path)
    assert second.status_of(CARD_A, "a.csv") == STATUS_PENDING


def test_the_schema_rejects_an_unknown_status(ledger):
    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO files (card_uuid, filename, status, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                (CARD_A, "a.csv", "in-flight", "2026-07-29T10:00:00Z"),
            )


def test_discovered_at_defaults_to_an_rfc3339_utc_timestamp(ledger):
    ledger.record_pending(CARD_A, "a.csv")

    with sqlite3.connect(ledger.database_path) as connection:
        discovered_at = connection.execute(
            "SELECT discovered_at FROM files WHERE card_uuid = ? AND filename = ?",
            (CARD_A, "a.csv"),
        ).fetchone()[0]

    assert len(discovered_at) == 20
    assert discovered_at.endswith("Z")


def test_rfc3339_utc_renders_second_precision_utc():
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 29, 18, 30, 0, 987654, tzinfo=timezone.utc)
    assert rfc3339_utc(moment) == "2026-07-29T18:30:00Z"


# --- Criterion 6: per-card identity ------------------------------------------


def test_the_same_filename_on_two_cards_is_two_rows(ledger):
    """The motivating case: a logger that restarts its counter on a new card."""
    assert ledger.record_pending(CARD_A, "logger-0001.csv") is True
    assert ledger.record_pending(CARD_B, "logger-0001.csv") is True

    with sqlite3.connect(ledger.database_path) as connection:
        rows = connection.execute(
            "SELECT card_uuid, filename FROM files ORDER BY card_uuid"
        ).fetchall()
    assert rows == [(CARD_A, "logger-0001.csv"), (CARD_B, "logger-0001.csv")]


def test_known_filenames_is_scoped_to_one_card(ledger):
    ledger.record_pending(CARD_A, "logger-0001.csv")

    assert ledger.known_filenames(CARD_A) == {"logger-0001.csv"}
    # Card B has never been seen, so its identical filename is still new.
    assert ledger.known_filenames(CARD_B) == set()


def test_uploading_one_cards_file_leaves_the_other_cards_copy_pending(ledger):
    ledger.record_pending(CARD_A, "logger-0001.csv")
    ledger.record_pending(CARD_B, "logger-0001.csv")

    ledger.mark_uploaded(CARD_A, "logger-0001.csv")

    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_UPLOADED
    assert ledger.status_of(CARD_B, "logger-0001.csv") == STATUS_PENDING


def test_the_composite_primary_key_is_enforced_by_the_database(ledger):
    ledger.record_pending(CARD_A, "a.csv")

    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO files (card_uuid, filename, status, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                (CARD_A, "a.csv", STATUS_PENDING, "2026-07-29T10:00:00Z"),
            )


# --- Criterion 11: the Phase 2 legacy guard ----------------------------------


def test_schema_state_reports_absent_for_a_missing_database(tmp_path):
    assert schema_state(tmp_path / "nothing.db") == SCHEMA_ABSENT


def test_schema_state_reports_absent_for_a_database_without_the_table(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other (x INTEGER)")

    assert schema_state(path) == SCHEMA_ABSENT


def test_schema_state_detects_a_phase_2_ledger(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)

    assert schema_state(path) == SCHEMA_LEGACY


def test_schema_state_detects_a_phase_3_ledger(tmp_path):
    path = tmp_path / "state.db"
    Ledger(path).initialize()

    assert schema_state(path) == SCHEMA_PHASE3


def test_schema_state_reports_an_unreadable_database(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database")

    assert schema_state(path) == SCHEMA_UNREADABLE


def test_the_schema_state_cli_prints_the_state(tmp_path, capsys):
    from state import main

    path = tmp_path / "state.db"
    Ledger(path).initialize()

    assert main(["--schema-state", str(path)]) == 0
    assert capsys.readouterr().out.strip() == SCHEMA_PHASE3


def test_the_cli_rejects_unknown_arguments(capsys):
    from state import main

    assert main(["--wipe-everything"]) == 2
