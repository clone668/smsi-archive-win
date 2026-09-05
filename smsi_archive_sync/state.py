from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .models import ArchiveCatalog


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, root: Path) -> None:
        self.path = root / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def save_catalog(self, catalog: ArchiveCatalog) -> None:
        payload = json.dumps(catalog.to_dict(), ensure_ascii=False)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache(cache_key,payload,updated_at) VALUES(?,?,?)",
                ("catalog", payload, utc_now()),
            )

    def load_catalog(self) -> ArchiveCatalog | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT payload FROM cache WHERE cache_key='catalog'").fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return ArchiveCatalog.from_dict(value) if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def event(self, level: str, message: str, detail: str = "") -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO events(created_at,level,message,detail) VALUES(?,?,?,?)",
                (utc_now(), level, message, detail[:4000]),
            )

    def set_runtime(self, key: str, value: Any) -> None:
        """Small durable facts - alert cursors and what alerting watches."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache(cache_key,payload,updated_at) VALUES(?,?,?)",
                (f"runtime:{key}", json.dumps(value, ensure_ascii=False), utc_now()),
            )

    def get_runtime(self, key: str, default: Any = None) -> Any:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT payload FROM cache WHERE cache_key=?", (f"runtime:{key}",)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    def max_event_id(self) -> int:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT MAX(id) FROM events").fetchone()
        return int((row[0] if row else 0) or 0)

    def events_since(
        self, last_id: int, levels: Sequence[str], limit: int = 8
    ) -> list[dict[str, Any]]:
        """Events newer than a cursor, oldest first, for outbound alerting."""
        wanted = [str(item) for item in levels]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT id,created_at,level,message,detail FROM events "
                f"WHERE id > ? AND level IN ({placeholders}) ORDER BY id LIMIT ?",
                (int(last_id), *wanted, max(1, min(int(limit), 50))),
            ).fetchall()
        return [
            {
                "id": row[0], "created_at": row[1], "level": row[2],
                "message": row[3], "detail": row[4],
            }
            for row in rows
        ]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT created_at,level,message,detail FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [
            {"created_at": row[0], "level": row[1], "message": row[2], "detail": row[3]}
            for row in rows
        ]

