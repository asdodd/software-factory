from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from factory.ports.context import AdapterContext


class SqliteStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> SqliteStateStore:
        return cls(ctx.base_dir / str(settings.get("path", "var/state.db")))

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (ns TEXT, k TEXT, v TEXT, PRIMARY KEY (ns, k))")
        return self._conn

    def get(self, namespace: str, key: str) -> str | None:
        with self._lock:
            row = self._db().execute("SELECT v FROM kv WHERE ns = ? AND k = ?", (namespace, key)).fetchone()
            return row[0] if row else None

    def put(self, namespace: str, key: str, value: str) -> None:
        with self._lock:
            self._db().execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (namespace, key, value))

    def compare_and_set(self, namespace: str, key: str, expected: str | None, value: str) -> bool:
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT v FROM kv WHERE ns = ? AND k = ?", (namespace, key)).fetchone()
                if (row[0] if row else None) != expected:
                    db.execute("ROLLBACK")
                    return False
                db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (namespace, key, value))
                db.execute("COMMIT")
                return True
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._db().execute("DELETE FROM kv WHERE ns = ? AND k = ?", (namespace, key))
