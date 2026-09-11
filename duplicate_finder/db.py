from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable


class ScanDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    name_lower TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    quick_hash TEXT,
                    full_hash TEXT,
                    extension TEXT NOT NULL,
                    last_seen INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name_lower)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(full_hash)")

    def get_cached_hashes(
        self, path: str, size: int, mtime_ns: int
    ) -> tuple[str | None, str | None]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT quick_hash, full_hash FROM files WHERE path = ? AND size = ? AND mtime_ns = ?",
                (path, size, mtime_ns),
            ).fetchone()
            if not row:
                return None, None
            return row["quick_hash"], row["full_hash"]

    def upsert_file(
        self,
        *,
        path: str,
        name_lower: str,
        size: int,
        mtime_ns: int,
        quick_hash: str | None,
        full_hash: str | None,
        extension: str,
        last_seen: int,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO files(path, name_lower, size, mtime_ns, quick_hash, full_hash, extension, last_seen)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name_lower = excluded.name_lower,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    quick_hash = excluded.quick_hash,
                    full_hash = excluded.full_hash,
                    extension = excluded.extension,
                    last_seen = excluded.last_seen
                """,
                (
                    path,
                    name_lower,
                    size,
                    mtime_ns,
                    quick_hash,
                    full_hash,
                    extension,
                    last_seen,
                ),
            )

    def remove_missing(self, seen_paths: Iterable[str]) -> None:
        seen = set(seen_paths)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT path FROM files").fetchall()
            missing = [(row["path"],) for row in rows if row["path"] not in seen]
            if missing:
                conn.executemany("DELETE FROM files WHERE path = ?", missing)
