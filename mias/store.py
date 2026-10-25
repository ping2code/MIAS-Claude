"""SQLite persistence: metric history, per-symbol state, alert outbox. One writer (this process)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
    ts REAL NOT NULL, symbol TEXT NOT NULL, session TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metrics_symbol_ts ON metrics(symbol, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT, rule TEXT NOT NULL,
    text TEXT NOT NULL, delivered REAL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE INDEX IF NOT EXISTS alerts_pending ON alerts(delivered, ts);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        if "sent_to" not in cols:     # added for multi-chat delivery
            self.db.execute("ALTER TABLE alerts ADD COLUMN sent_to TEXT")

    def close(self) -> None:
        self.db.close()

    # key/value state -------------------------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT INTO kv(key, value, updated) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                        (key, json.dumps(value, separators=(",", ":"), default=str), time.time()))

    # metrics ---------------------------------------------------------------------------------
    def add_metrics(self, ts: float, session: str, rows: dict[str, dict]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO metrics(ts, symbol, session, data) VALUES(?,?,?,?)",
                                [(ts, s, session, json.dumps(d, separators=(",", ":"), default=str))
                                 for s, d in rows.items()])

    def history(self, symbol: str, since_ts: float) -> list[tuple[float, dict]]:
        return [(ts, json.loads(d)) for ts, d in self.db.execute(
            "SELECT ts, data FROM metrics WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since_ts))]

    def prune(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        self.db.execute("DELETE FROM metrics WHERE ts<?", (cutoff,))
        self.db.execute("DELETE FROM alerts WHERE ts<? AND delivered IS NOT NULL", (cutoff,))

    # alert outbox ----------------------------------------------------------------------------
    def enqueue(self, rule: str, text: str, symbol: str | None = None) -> int:
        cur = self.db.execute("INSERT INTO alerts(ts, symbol, rule, text) VALUES(?,?,?,?)",
                              (time.time(), symbol, rule, text))
        return cur.lastrowid

    def pending(self, max_age_s: float = 3600, max_attempts: int = 20) -> list[tuple[int, str, str]]:
        return self.db.execute(
            "SELECT id, rule, text, sent_to FROM alerts WHERE delivered IS NULL AND ts>=? AND attempts<? ORDER BY id",
            (time.time() - max_age_s, max_attempts)).fetchall()

    def mark_delivered(self, alert_id: int) -> None:
        self.db.execute("UPDATE alerts SET delivered=?, attempts=attempts+1 WHERE id=?", (time.time(), alert_id))

    def mark_failed(self, alert_id: int, error: str, sent_to: str | None = None) -> None:
        self.db.execute("UPDATE alerts SET attempts=attempts+1, last_error=?, sent_to=COALESCE(?, sent_to) WHERE id=?",
                        (error[:300], sent_to, alert_id))

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT ts, symbol, rule, text, delivered FROM alerts ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        return [dict(ts=r[0], symbol=r[1], rule=r[2], text=r[3], delivered=r[4]) for r in rows]
