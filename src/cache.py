from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ResponseCache:
    """작은 JSON 응답을 저장하는 SQLite 캐시입니다."""

    def __init__(self, path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "cache.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS response_cache (
                    source TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (source, cache_key)
                )
                """
            )

    def get(
        self,
        source: str,
        cache_key: str,
        max_age: timedelta | None = None,
    ) -> Any | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload, fetched_at
                FROM response_cache
                WHERE source = ? AND cache_key = ?
                """,
                (source, cache_key),
            ).fetchone()
        if row is None:
            return None
        payload, fetched_at = row
        if max_age is not None:
            fetched = datetime.fromisoformat(fetched_at)
            if datetime.now(timezone.utc) - fetched > max_age:
                return None
        return json.loads(payload)

    def set(self, source: str, cache_key: str, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO response_cache(source, cache_key, payload, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (source, cache_key, encoded, fetched_at),
            )

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM response_cache")

