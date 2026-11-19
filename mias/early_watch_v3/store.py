"""V3 storage: its own SQLite file (data/early_watch_v3/v3.db; replays use their own file). Never a V1 / V2 table or
file. Idempotent: a Watch row is inserted once (a restart that replays the morning never duplicates or re-sends), and a
lifecycle end is written only while the Watch is still open."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS v3_bars (
    day TEXT NOT NULL, sym TEXT NOT NULL, s INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL, source TEXT,
    src_rowid INTEGER, received_at REAL, seen_at REAL, gap INTEGER, PRIMARY KEY (sym, s));
CREATE TABLE IF NOT EXISTS v3_candidate_log (
    cand_id TEXT, day TEXT, family TEXT, sym TEXT, side TEXT, leg_id TEXT, candidate_at INTEGER, candidate_price REAL,
    status TEXT, PRIMARY KEY (cand_id, candidate_at));
CREATE TABLE IF NOT EXISTS v3_candidates (
    cand_id TEXT PRIMARY KEY, day TEXT, family TEXT, sym TEXT, side TEXT, leg_id TEXT, candidate_at INTEGER,
    candidate_price REAL, end_status TEXT, end_at INTEGER, ema TEXT, leg_start TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS v3_watches (
    watch_id TEXT PRIMARY KEY, cand_id TEXT, day TEXT, family TEXT, families TEXT, lane TEXT, sym TEXT, side TEXT,
    leg_id TEXT, is_index INTEGER, candidate_at INTEGER, candidate_price REAL, watch_at INTEGER, watch_price REAL,
    level REAL, pre_ema INTEGER, ema_watch TEXT, leg_start TEXT, context TEXT,
    atr REAL, vwap REAL,
    chk_vwap TEXT, chk_ema TEXT, chk_sma200 TEXT, chk_spy TEXT, chk_volume TEXT, chk_options TEXT,
    n_support INTEGER, n_against INTEGER, n_neutral INTEGER,
    vwap_dist_pct REAL, vwap_dist_atr REAL, vwap_marginal INTEGER,
    inval_kind TEXT, inval_level REAL, inval_dist_pct REAL, inval_dist_atr REAL, tight INTEGER,
    rel_vol REAL, low_conviction INTEGER, late_session INTEGER, minutes_to_close INTEGER,
    options_status TEXT, options_snapshot_ts REAL, options_age_s REAL, options_stock TEXT, options_market TEXT,
    options_overall TEXT, market_mismatch TEXT,
    view TEXT, text TEXT, render_error TEXT,
    v2_would_send INTEGER, v2_verdict TEXT, v3_verdict TEXT,
    telegram_sent INTEGER NOT NULL DEFAULT 0, telegram_queued_at REAL, telegram_sent_at REAL, telegram_message_id TEXT,
    telegram_error TEXT, telegram_group TEXT, telegram_text TEXT,
    bar_received_at REAL, bar_seen_at REAL, created_at REAL,
    state TEXT, end_at INTEGER, end_reason TEXT, upgrade_source TEXT, upgrade_at INTEGER, upgrade_price REAL,
    out_5 REAL, out_10 REAL, out_15 REAL, out_30 REAL, out_60 REAL, mfe_pct REAL, mae_pct REAL, mfe_atr REAL,
    mae_atr REAL, outcomes_at REAL, rules_version TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS v3_lifecycle (
    watch_id TEXT, event TEXT, at INTEGER, reason TEXT, recorded_at REAL, PRIMARY KEY (watch_id, event));
CREATE TABLE IF NOT EXISTS v3_ema_cross (day TEXT, sym TEXT, side TEXT, at INTEGER, PRIMARY KEY (sym, at));
CREATE TABLE IF NOT EXISTS v3_confirms (
    day TEXT, sym TEXT, side TEXT, ts REAL, source TEXT, PRIMARY KEY (sym, side, ts, source));
CREATE TABLE IF NOT EXISTS v3_feed (ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
"""

VIEW_COLS = ["atr", "vwap", "n_support", "n_against", "n_neutral", "vwap_dist_pct", "vwap_dist_atr", "vwap_marginal",
             "inval_kind", "inval_level", "inval_dist_pct", "inval_dist_atr", "tight", "rel_vol", "low_conviction",
             "late_session", "minutes_to_close", "options_status", "options_snapshot_ts", "options_age_s",
             "options_stock", "options_market", "options_overall", "market_mismatch", "is_index"]
WCOLS = ["watch_id", "cand_id", "day", "family", "lane", "sym", "side", "leg_id", "candidate_at", "candidate_price",
         "watch_at", "watch_price", "level", "pre_ema", "ema_watch", "leg_start", "context", "state", "rules_version",
         "mode", "bar_received_at", "bar_seen_at", "created_at"]
JSON = ("ema_watch", "leg_start", "context", "view", "families")
HORIZONS = (5, 10, 15, 30, 60)


class V3DB:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- kv / feed ------------------------------------------------------------------------------

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))

    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def feed(self, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO v3_feed VALUES (?, ?, ?)", (time.time(), kind, detail[:500]))

    def bar(self, day: str, sym: str, b, source: str, src_rowid=None, received_at=None, seen_at=None,
            gap=False) -> None:
        self.db.execute("INSERT OR IGNORE INTO v3_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (day, sym, b.t, b.o, b.h, b.l, b.c, b.v, source, src_rowid, received_at, seen_at, int(gap)))

    def confirm(self, day: str, ev: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO v3_confirms VALUES (?,?,?,?,?)",
                        (day, ev["sym"], ev["side"], float(ev["ts"]), ev["source"]))

    # ---- detector events ------------------------------------------------------------------------

    def apply(self, events: list[dict], mode: str, meta: dict | None = None) -> list[str]:
        """Persist V3 detector events. ``meta`` (watch_id -> extra columns: view, timings) is merged into new
        Watch rows. Returns the ids of NEW Watches."""
        new, meta = [], meta or {}
        now = time.time()
        for ev in events:
            k = ev["kind"]
            if k == "v3_candidate":
                self.db.execute("INSERT OR IGNORE INTO v3_candidate_log VALUES (?,?,?,?,?,?,?,?,?)",
                                (ev["cand_id"], ev["day"], ev["family"], ev["sym"], ev["side"], ev["leg_id"],
                                 ev["candidate_at"], ev["candidate_price"], ev["status"]))
                if ev["status"] == "pending":
                    self.db.execute("INSERT OR IGNORE INTO v3_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (ev["cand_id"], ev["day"], ev["family"], ev["sym"], ev["side"], ev["leg_id"],
                                     ev["candidate_at"], ev["candidate_price"], None, None, json.dumps(ev["ema"]),
                                     json.dumps(ev["leg_start"]), mode))
            elif k == "v3_candidate_end":
                self.db.execute("UPDATE v3_candidates SET end_status=?, end_at=? WHERE cand_id=? AND end_status IS NULL",
                                (ev["status"], ev["end_at"], ev["cand_id"]))
            elif k == "v3_watch":
                m = meta.get(ev["watch_id"], {})
                row = {c: ev.get(c) for c in WCOLS}
                row.update({c: m.get(c) for c in ("bar_received_at", "bar_seen_at", "created_at")})
                row["pre_ema"] = int(bool(ev["pre_ema"]))
                row["mode"] = mode
                for c in JSON:
                    if c in row:
                        row[c] = json.dumps(row[c], sort_keys=True, default=str)
                cur = self.db.execute(f"INSERT OR IGNORE INTO v3_watches ({','.join(WCOLS)}) "
                                      f"VALUES ({','.join('?' * len(WCOLS))})", [row[c] for c in WCOLS])
                if cur.rowcount:
                    new.append(ev["watch_id"])
                    self.lifecycle(ev["watch_id"], "WATCH", ev["watch_at"], ev["family"])
                    if m.get("view") is not None or m.get("render_error"):
                        self.set_view(ev["watch_id"], m.get("view"), m.get("text"), m.get("render_error"))
            elif k == "v3_end":
                cur = self.db.execute("UPDATE v3_watches SET state=?, end_at=?, end_reason=?, upgrade_source=?, "
                                      "upgrade_at=?, upgrade_price=? WHERE watch_id=? AND state='WATCH'",
                                      (ev["state"], ev["end_at"], ev["end_reason"], ev["upgrade_source"],
                                       ev["upgrade_at"], ev["upgrade_price"], ev["watch_id"]))
                if cur.rowcount:
                    self.lifecycle(ev["watch_id"], ev["state"], ev["end_at"], ev["end_reason"], now)
            elif k == "v3_ema_cross":
                self.db.execute("INSERT OR IGNORE INTO v3_ema_cross VALUES (?,?,?,?)",
                                (ev["day"], ev["sym"], ev["side"], ev["at"]))
        return new

    def lifecycle(self, watch_id: str, event: str, at: int, reason: str | None, recorded: float | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO v3_lifecycle VALUES (?,?,?,?,?)",
                        (watch_id, event, at, reason, recorded or time.time()))

    def set_view(self, watch_id: str, view: dict | None, text: str | None, error: str | None = None) -> None:
        cols, vals = ["view", "text", "render_error", "families"], [
            json.dumps(view, sort_keys=True, default=str) if view else None, text, error,
            json.dumps((view or {}).get("families")) if view else None]
        if view:
            for c in ("vwap", "ema", "sma200", "spy", "volume", "options"):
                cols.append(f"chk_{c}")
                vals.append(view["checks"][c]["state"])
            for c in VIEW_COLS:
                cols.append(c)
                x = view.get(c)
                vals.append(int(x) if isinstance(x, bool) else x)
        self.db.execute(f"UPDATE v3_watches SET {', '.join(c + '=?' for c in cols)} WHERE watch_id=?",
                        vals + [watch_id])

    def set_policy(self, watch_id: str, v3_verdict: str, v2_verdict: str | None, v2_would_send: bool) -> None:
        self.db.execute("UPDATE v3_watches SET v3_verdict=?, v2_verdict=?, v2_would_send=? WHERE watch_id=?",
                        (v3_verdict, v2_verdict, int(bool(v2_would_send)), watch_id))

    def mark_queued(self, watch_id: str, at: float, group: str | None) -> None:
        self.db.execute("UPDATE v3_watches SET telegram_queued_at=?, telegram_group=? WHERE watch_id=?",
                        (at, group, watch_id))

    def mark_telegram(self, watch_id: str, sent: bool, ids, error, group, at, text=None) -> None:
        self.db.execute("UPDATE v3_watches SET telegram_sent=?, telegram_message_id=?, telegram_sent_at=?, "
                        "telegram_error=?, telegram_group=?, telegram_text=COALESCE(?, telegram_text) "
                        "WHERE watch_id=?",
                        (int(sent), ",".join(ids or []) or None, at if sent else None, error, group, text, watch_id))

    def set_outcomes(self, watch_id: str, o: dict) -> None:
        cols = [f"out_{h}" for h in HORIZONS] + ["mfe_pct", "mae_pct", "mfe_atr", "mae_atr"]
        self.db.execute(f"UPDATE v3_watches SET {', '.join(c + '=?' for c in cols)}, outcomes_at=? WHERE watch_id=?",
                        [o.get(c) for c in cols] + [time.time(), watch_id])

    # ---- reads ------------------------------------------------------------------------------------

    def watches(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM v3_watches WHERE {where} ORDER BY watch_at, watch_id", args)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for c in JSON:
                d[c] = json.loads(d[c]) if d.get(c) else None
            out.append(d)
        return out

    def bars(self, day: str, sym: str) -> list[tuple]:
        return self.db.execute("SELECT s, o, h, l, c, v FROM v3_bars WHERE day=? AND sym=? ORDER BY s",
                               (day, sym)).fetchall()
