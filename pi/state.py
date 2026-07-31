#!/usr/bin/env python3
"""Shared SQLite ledger for the Pi's watcher and uploader.

One row per `(card_uuid, filename)` the Pi has ever taken responsibility for.
The ledger is what makes re-inserting a never-cleared SD card cheap: the watcher
skips any filename already recorded `pending` or `uploaded` *for that card*
instead of re-copying it.

Dedup is by name, not content, because the logger always writes a new distinct
name and never appends to or reuses one. Phase 3 scopes that name to the card's
filesystem UUID: two different cards may each carry a `logger-0001.csv`, and
those are two distinct files that must both be delivered.

Standard library only. See prd/phase-3-multi-card-ingestion.md.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  card_uuid TEXT NOT NULL,
  filename TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
  discovered_at TEXT NOT NULL,
  PRIMARY KEY (card_uuid, filename)
);
"""

# Schema states `setup.sh` distinguishes before it will touch local state.
SCHEMA_ABSENT = "absent"
SCHEMA_LEGACY = "legacy"
SCHEMA_PHASE3 = "phase3"
SCHEMA_UNREADABLE = "unreadable"


def rfc3339_utc(moment: Optional[datetime] = None) -> str:
    moment = datetime.now(timezone.utc) if moment is None else moment
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def schema_state(database_path: Path) -> str:
    """Classify an existing ledger file as Phase 2 (`legacy`) or Phase 3.

    Phase 3 changes the primary key, so a Phase 2 database must never be read
    with the new queries. `setup.sh` calls this through the `--schema-state`
    entry point below and refuses to continue on `legacy` without an explicit
    reset.
    """
    path = Path(database_path)
    if not path.exists():
        return SCHEMA_ABSENT
    try:
        with closing(sqlite3.connect(path, timeout=10.0)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(files)")]
    except sqlite3.Error:
        return SCHEMA_UNREADABLE
    if not columns:
        return SCHEMA_ABSENT
    return SCHEMA_PHASE3 if "card_uuid" in columns else SCHEMA_LEGACY


class Ledger:
    """Thin SQLite wrapper. Every call opens and closes its own connection.

    Two independent processes share this database, so no connection is held
    open between calls; WAL plus a busy timeout lets them interleave. The write
    volume (a handful of rows per card insertion) makes the cost irrelevant.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        # A recorded row must survive a power cut, which is the whole point of
        # the ledger; the row rate makes full synchronisation free in practice.
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def known_filenames(self, card_uuid: str) -> set:
        """Every filename already recorded for one card, in either status.

        Both statuses mean "the Pi has this file, do not copy it again": the
        watcher's skip rule from the PRD. Another card's identical filename is
        deliberately not in this set.
        """
        with closing(self._connect()) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM files WHERE card_uuid = ?", (card_uuid,)
                )
            }

    def status_of(self, card_uuid: str, filename: str) -> Optional[str]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM files WHERE card_uuid = ? AND filename = ?",
                (card_uuid, filename),
            ).fetchone()
        return None if row is None else row[0]

    def record_pending(
        self, card_uuid: str, filename: str, discovered_at: Optional[str] = None
    ) -> bool:
        """Record a queued file. Returns False if the identity was already known.

        Never downgrades an `uploaded` row back to `pending`; a file that has
        already reached the server must not be re-uploaded because its card was
        re-inserted.
        """
        timestamp = discovered_at if discovered_at is not None else rfc3339_utc()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO files (card_uuid, filename, status, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                (card_uuid, filename, STATUS_PENDING, timestamp),
            )
            connection.commit()
            return cursor.rowcount == 1

    def mark_uploaded(
        self, card_uuid: str, filename: str, discovered_at: Optional[str] = None
    ) -> None:
        """Mark a file delivered.

        Upserts rather than updates: the uploader must still be able to close
        out a queued file whose `pending` row was lost between the queue rename
        and the ledger write.
        """
        timestamp = discovered_at if discovered_at is not None else rfc3339_utc()
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO files (card_uuid, filename, status, discovered_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(card_uuid, filename) DO UPDATE SET status = excluded.status",
                (card_uuid, filename, STATUS_UPLOADED, timestamp),
            )
            connection.commit()

    def filenames_with_status(self, card_uuid: str, status: str) -> list:
        with closing(self._connect()) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM files WHERE card_uuid = ? AND status = ? "
                    "ORDER BY filename",
                    (card_uuid, status),
                )
            ]

    def counts(self) -> dict:
        """Row counts per status across every card, for progress logging."""
        with closing(self._connect()) as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM files GROUP BY status"
                )
            }


def summarize(counts: dict, statuses: Iterable = (STATUS_PENDING, STATUS_UPLOADED)) -> str:
    return " ".join(f"{status}={counts.get(status, 0)}" for status in statuses)


def main(argv: Optional[list] = None) -> int:
    """`state.py --schema-state <path>`, used by setup.sh's Phase 2 guard."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--schema-state":
        print(schema_state(Path(args[1])))
        return 0
    print("usage: state.py --schema-state <database-path>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
