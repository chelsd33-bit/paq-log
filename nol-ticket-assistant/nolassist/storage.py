"""SQLite 실행 이력 저장. 개인정보·결제정보는 저장하지 않는다."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .logging_setup import redactor

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    test_mode INTEGER NOT NULL,
    url TEXT NOT NULL,
    request_json TEXT NOT NULL,
    final_stage TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def start_run(self, url: str, test_mode: bool, safe_request: dict) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs(started_at, test_mode, url, request_json) VALUES (?,?,?,?)",
                (_now(), int(test_mode), url, json.dumps(safe_request, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def add_event(self, run_id: int, stage: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(run_id, ts, stage, message) VALUES (?,?,?,?)",
                (run_id, _now(), stage, redactor.redact(message)),
            )

    def finish_run(self, run_id: int, final_stage: str, summary: dict | None = None) -> None:
        payload = redactor.redact(json.dumps(summary, ensure_ascii=False)) if summary else None
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET ended_at=?, final_stage=?, summary_json=? WHERE id=?",
                (_now(), final_stage, payload, run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, ended_at, test_mode, url, final_stage FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["id", "started_at", "ended_at", "test_mode", "url", "final_stage"]
        return [dict(zip(keys, r)) for r in rows]

    def events(self, run_id: int) -> list[tuple[str, str, str]]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, stage, message FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()

    def save_setting(self, key: str, value: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def load_setting(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
