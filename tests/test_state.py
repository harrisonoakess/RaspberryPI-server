"""Ledger tests — the dedup record behind PRD success criteria 5 and 6."""

import sqlite3

import pytest

from state import STATUS_PENDING, STATUS_UPLOADED, Ledger, rfc3339_utc, summarize


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
    instance.record_pending("a.csv")
    instance.initialize()
    assert instance.status_of("a.csv") == STATUS_PENDING


def test_recording_a_new_file_reports_that_it_was_new(ledger):
    assert ledger.record_pending("a.csv") is True
    assert ledger.status_of("a.csv") == STATUS_PENDING


def test_recording_a_known_file_again_is_a_no_op(ledger):
    ledger.record_pending("a.csv", "2026-07-29T10:00:00Z")

    assert ledger.record_pending("a.csv", "2026-07-29T11:00:00Z") is False
    # The original discovery time is preserved.
    with sqlite3.connect(ledger.database_path) as connection:
        discovered_at = connection.execute(
            "SELECT discovered_at FROM files WHERE filename = ?", ("a.csv",)
        ).fetchone()[0]
    assert discovered_at == "2026-07-29T10:00:00Z"


def test_an_uploaded_file_is_never_downgraded_to_pending(ledger):
    """Criterion 5: re-inserting the card must not re-queue a delivered file."""
    ledger.record_pending("a.csv")
    ledger.mark_uploaded("a.csv")

    assert ledger.record_pending("a.csv") is False
    assert ledger.status_of("a.csv") == STATUS_UPLOADED


def test_known_filenames_covers_both_statuses(ledger):
    """The watcher skips a filename in either status."""
    ledger.record_pending("pending.csv")
    ledger.record_pending("done.csv")
    ledger.mark_uploaded("done.csv")

    assert ledger.known_filenames() == {"pending.csv", "done.csv"}


def test_mark_uploaded_inserts_a_row_that_was_never_recorded_pending(ledger):
    """Covers a crash between the queue rename and the ledger write: the
    uploader must still be able to close the file out."""
    ledger.mark_uploaded("orphan.csv")

    assert ledger.status_of("orphan.csv") == STATUS_UPLOADED


def test_status_of_an_unknown_file_is_none(ledger):
    assert ledger.status_of("missing.csv") is None


def test_filenames_with_status_is_sorted(ledger):
    for name in ("c.csv", "a.csv", "b.csv"):
        ledger.record_pending(name)
    ledger.mark_uploaded("b.csv")

    assert ledger.filenames_with_status(STATUS_PENDING) == ["a.csv", "c.csv"]
    assert ledger.filenames_with_status(STATUS_UPLOADED) == ["b.csv"]


def test_counts_reports_rows_per_status(ledger):
    ledger.record_pending("a.csv")
    ledger.record_pending("b.csv")
    ledger.mark_uploaded("b.csv")

    assert ledger.counts() == {STATUS_PENDING: 1, STATUS_UPLOADED: 1}


def test_summarize_reports_zero_for_a_missing_status():
    assert summarize({STATUS_UPLOADED: 2}) == "pending=0 uploaded=2"


def test_rows_survive_reopening_the_database(tmp_path):
    """Two processes share this file, and it must outlive both."""
    path = tmp_path / "state.db"
    first = Ledger(path)
    first.initialize()
    first.record_pending("a.csv")

    second = Ledger(path)
    assert second.status_of("a.csv") == STATUS_PENDING


def test_the_schema_rejects_an_unknown_status(ledger):
    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO files (filename, status, discovered_at) VALUES (?, ?, ?)",
                ("a.csv", "in-flight", "2026-07-29T10:00:00Z"),
            )


def test_discovered_at_defaults_to_an_rfc3339_utc_timestamp(ledger):
    ledger.record_pending("a.csv")

    with sqlite3.connect(ledger.database_path) as connection:
        discovered_at = connection.execute(
            "SELECT discovered_at FROM files WHERE filename = ?", ("a.csv",)
        ).fetchone()[0]

    assert len(discovered_at) == 20
    assert discovered_at.endswith("Z")


def test_rfc3339_utc_renders_second_precision_utc():
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 29, 18, 30, 0, 987654, tzinfo=timezone.utc)
    assert rfc3339_utc(moment) == "2026-07-29T18:30:00Z"
