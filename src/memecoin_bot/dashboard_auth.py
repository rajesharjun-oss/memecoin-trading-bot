"""Local, revocable browser sessions for the paper dashboard.

Only SHA-256 digests of high-entropy random session IDs are persisted. The
pairing code and CSRF tokens remain process memory only.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class RememberedSessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, session_id: str, *, now: int, expires_at: int, idle_seconds: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS remembered_sessions (
                    session_hash TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "DELETE FROM remembered_sessions WHERE expires_at <= ? OR last_seen_at <= ?",
                (now, now - idle_seconds),
            )
            connection.execute(
                "INSERT INTO remembered_sessions VALUES (?, ?, ?, ?)",
                (_digest(session_id), now, now, expires_at),
            )
            connection.commit()

    def get(self, session_id: str, *, now: int, idle_seconds: int) -> tuple[int, int] | None:
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            row = connection.execute(
                "SELECT expires_at, last_seen_at FROM remembered_sessions WHERE session_hash = ?",
                (_digest(session_id),),
            ).fetchone()
        if row is None:
            return None
        expires_at, last_seen_at = int(row[0]), int(row[1])
        if expires_at <= now or last_seen_at <= now - idle_seconds:
            return None
        return expires_at, last_seen_at

    def touch(self, session_id: str, *, now: int) -> bool:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            cursor = connection.execute(
                "UPDATE remembered_sessions SET last_seen_at = ? WHERE session_hash = ?",
                (now, _digest(session_id)),
            )
            connection.commit()
            return cursor.rowcount == 1

    def revoke(self, session_id: str) -> None:
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.execute(
                "DELETE FROM remembered_sessions WHERE session_hash = ?", (_digest(session_id),)
            )
            connection.commit()

    def revoke_all(self) -> None:
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.execute("DELETE FROM remembered_sessions")
            connection.commit()
