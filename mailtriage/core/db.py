from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Database(AbstractContextManager["Database"]):
    conn: sqlite3.Connection

    @classmethod
    def open(cls, path: Path) -> "Database":
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Strong defaults for a local single-writer tool
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return cls(conn=conn)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()

    def exec(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.conn.execute(sql, params)

    def query_one(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def query_value(self, sql: str, params: tuple[object, ...] = ()) -> str | None:
        row = self.query_one(sql, params)
        if row is None:
            return None
        return str(row[0])

    def record_run_window(self, start_utc: str, end_utc: str) -> None:
        # Optional bookkeeping; harmless if unused.
        self.conn.execute(
            "INSERT INTO run_log (start_utc, end_utc, recorded_at_utc) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (start_utc, end_utc),
        )
