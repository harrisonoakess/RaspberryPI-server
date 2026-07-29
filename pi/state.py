#!/usr/bin/env python3
"""Shared SQLite ledger for the Pi's watcher and uploader.

One row per filename the Pi has ever taken responsibility for. The ledger is
what makes re-inserting a never-cleared SD card cheap: the watcher skips any
filename already recorded `pending` or `uploaded` instead of re-copying it.

Dedup is by filename alone, not content, because the logger always writes a new
distinct name and never appends to or reuses one. See prd/phase-2-data-sync.md.

Standard library only.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  filename TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
  discovered_at TEXT NOT NULL
);
"""


def rfc3339_utc(moment: Optional[datetime] = None) -> str:
    moment = datetime.now(timezone.utc) if moment is None else moment
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


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

    def known_filenames(self) -> set:
        """Every filename already recorded, in either status.

        Both statuses mean "the Pi has this file, do not copy it again": the
        watcher's skip rule from the PRD.
        """
        with closing(self._connect()) as connection:
            return {row[0] for row in connection.execute("SELECT filename FROM files")}

    def status_of(self, filename: str) -> Optional[str]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM files WHERE filename = ?", (filename,)
            ).fetchone()
        return None if row is None else row[0]

    def record_pending(self, filename: str, discovered_at: Optional[str] = None) -> bool:
        """Record a queued file. Returns False if the filename was already known.

        Never downgrades an `uploaded` row back to `pending`; a file that has
        already reached the server must not be re-uploaded because the card was
        re-inserted.
        """
        timestamp = discovered_at if discovered_at is not None else rfc3339_utc()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO files (filename, status, discovered_at) "
                "VALUES (?, ?, ?)",
                (filename, STATUS_PENDING, timestamp),
            )
            connection.commit()
            return cursor.rowcount == 1

    def mark_uploaded(self, filename: str, discovered_at: Optional[str] = None) -> None:
        """Mark a file delivered.

        Upserts rather than updates: the uploader must still be able to close
        out a queued file whose `pending` row was lost between the queue rename
        and the ledger write.
        """
        timestamp = discovered_at if discovered_at is not None else rfc3339_utc()
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO files (filename, status, discovered_at) VALUES (?, ?, ?) "
                "ON CONFLICT(filename) DO UPDATE SET status = excluded.status",
                (filename, STATUS_UPLOADED, timestamp),
            )
            connection.commit()

    def filenames_with_status(self, status: str) -> list:
        with closing(self._connect()) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM files WHERE status = ? ORDER BY filename",
                    (status,),
                )
            ]

    def counts(self) -> dict:
        """Row counts per status, for start-up and progress logging."""
        with closing(self._connect()) as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM files GROUP BY status"
                )
            }


def summarize(counts: dict, statuses: Iterable = (STATUS_PENDING, STATUS_UPLOADED)) -> str:
    return " ".join(f"{status}={counts.get(status, 0)}" for status in statuses)
