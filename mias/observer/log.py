"""The observer's own SQLite database (data/observer/observer.db; replays use their own file). Writes are idempotent so a
restart that replays the morning never duplicates a watch: a watch row is inserted once (keeping the first
received_at) and its end is written only while it is still open."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_bars (
    sym TEXT NOT NULL, s INTEGER NOT NULL, e INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL, vw REAL,
    a REAL, av REAL, op REAL, z REAL, received_at REAL NOT NULL, status TEXT NOT NULL, conn INTEGER);
CREATE INDEX IF NOT EXISTS ws_bars_sym_s ON ws_bars(sym, s);
CREATE TABLE IF NOT EXISTS bars1 (
    day TEXT NOT NULL, sym TEXT NOT NULL, s INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,
    source TEXT NOT NULL, PRIMARY KEY (sym, s));
CREATE TABLE IF NOT EXISTS feed_events (ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS watches (
    watch_id TEXT PRIMARY KEY, day TEXT, lane TEXT, sym TEXT, side TEXT, leg_id TEXT, created_at INTEGER,
    received_at REAL, price REAL, vwap REAL, ema9 REAL, ema21 REAL, spread REAL, spread_ref REAL, atr REAL,
    structure_type TEXT, level REAL, or_high REAL, or_low REAL, or_break INTEGER, minutes_since_open REAL,
    opening_shadow_only INTEGER, context TEXT, state TEXT, end_at INTEGER, end_reason TEXT, upgrade_source TEXT,
    upgrade_at INTEGER, upgrade_price REAL, sent INTEGER NOT NULL DEFAULT 0, rules_version TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS structure (
    day TEXT, sym TEXT, kind TEXT, price REAL, bar_t INTEGER, at INTEGER, label TEXT,
    PRIMARY KEY (sym, kind, bar_t, at));
CREATE TABLE IF NOT EXISTS suppressed (
    day TEXT, lane TEXT, sym TEXT, side TEXT, leg_id TEXT, reason TEXT, at INTEGER,
    PRIMARY KEY (day, lane, sym, side, leg_id, reason));
CREATE TABLE IF NOT EXISTS confirms (
    day TEXT, sym TEXT, side TEXT, ts REAL, source TEXT, PRIMARY KEY (sym, side, ts, source));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
"""

WATCH_COLS = ["watch_id", "day", "lane", "sym", "side", "leg_id", "created_at", "received_at", "price", "vwap",
              "ema9", "ema21", "spread", "spread_ref", "atr", "structure_type", "level", "or_high", "or_low",
              "or_break", "minutes_since_open", "opening_shadow_only", "context", "state", "end_at", "end_reason",
              "upgrade_source", "upgrade_at", "upgrade_price", "rules_version", "mode"]


class ObserverDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(watches)")}
        for col, typ in (("telegram_sent", "INTEGER NOT NULL DEFAULT 0"), ("telegram_message_id", "TEXT"),
                         ("telegram_error", "TEXT"), ("telegram_sent_at", "REAL")):
            if col not in have:
                self.db.execute(f"ALTER TABLE watches ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.db.close()

    # ---- kv / feed ------------------------------------------------------------------------------

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))

    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def feed(self, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO feed_events VALUES (?, ?, ?)", (time.time(), kind, detail[:500]))

    # ---- bars -----------------------------------------------------------------------------------

    def ws_bar(self, ev: dict, received_at: float, status: str, conn: int) -> None:
        self.db.execute("INSERT INTO ws_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev.get("sym"), ev.get("s"), ev.get("e"), ev.get("o"), ev.get("h"), ev.get("l"),
                         ev.get("c"), ev.get("v"), ev.get("vw"), ev.get("a"), ev.get("av"), ev.get("op"),
                         ev.get("z"), received_at, status, conn))

    def bar1(self, day: str, sym: str, b, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO bars1 VALUES (?,?,?,?,?,?,?,?,?)",
                        (day, sym, b.t, b.o, b.h, b.l, b.c, b.v, source))

    # ---- observer events ------------------------------------------------------------------------

    def apply(self, events: list[dict], mode: str) -> list[str]:
        """Persist observer events. Returns the ids of NEW watches (not seen before)."""
        new: list[str] = []
        for ev in events:
            k = ev["kind"]
            if k == "watch":
                row = dict(ev, context=json.dumps(ev["context"], sort_keys=True), mode=mode,
                           or_break=int(ev["or_break"]), opening_shadow_only=int(ev["opening_shadow_only"]))
                cur = self.db.execute(
                    f"INSERT OR IGNORE INTO watches ({','.join(WATCH_COLS)}) VALUES ({','.join('?' * len(WATCH_COLS))})",
                    [row.get(c) for c in WATCH_COLS])
                if cur.rowcount:
                    new.append(ev["watch_id"])
            elif k == "end":
                self.db.execute("UPDATE watches SET state=?, end_at=?, end_reason=?, upgrade_source=?, upgrade_at=?, "
                                "upgrade_price=? WHERE watch_id=? AND state='WATCH'",
                                (ev["state"], ev["end_at"], ev["end_reason"], ev["upgrade_source"],
                                 ev["upgrade_at"], ev["upgrade_price"], ev["watch_id"]))
            elif k == "suppressed":
                self.db.execute("INSERT OR IGNORE INTO suppressed VALUES (?,?,?,?,?,?,?)",
                                (ev["day"], ev["lane"], ev["sym"], ev["side"], ev["leg_id"], ev["reason"], ev["at"]))
            elif k == "pivot":
                self.db.execute("INSERT OR IGNORE INTO structure VALUES (?,?,?,?,?,?,?)",
                                (ev["day"], ev["sym"], "PIVOT_" + ev["pivot"], ev["price"], ev["bar_t"],
                                 ev["confirmed_at"], ev["label"]))
            elif k == "dev":
                self.db.execute("INSERT OR IGNORE INTO structure VALUES (?,?,?,?,?,?,?)",
                                (ev["day"], ev["sym"], ev["event"], ev["level"], ev["anchor_t"], ev["at"], None))
        return new

    def mark_telegram(self, watch_id: str, sent: bool, ids: list[str] | None, error: str | None,
                      at: float) -> None:
        self.db.execute("UPDATE watches SET telegram_sent=?, sent=?, telegram_message_id=?, telegram_error=?, "
                        "telegram_sent_at=? WHERE watch_id=?",
                        (int(sent), int(sent), ",".join(ids or []) or None, error, at if sent else None, watch_id))

    def confirm(self, day: str, ev: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO confirms VALUES (?,?,?,?,?)",
                        (day, ev["sym"], ev["side"], float(ev["ts"]), ev["source"]))

    def watches(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM watches WHERE {where} ORDER BY created_at, watch_id", args)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["context"] = json.loads(d["context"] or "{}")
            out.append(d)
        return out
