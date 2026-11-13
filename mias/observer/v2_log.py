"""Early Watch v2 research database (data/observer/v2.db live; replays use their own files). Separate from the v1
database; the bars1 / confirms / feed / kv tables have the v1 layout, plus v2 candidates, watches and EMA crosses."""
from __future__ import annotations

import json
from pathlib import Path

from .log import ObserverDB

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_candidates (
    cand_id TEXT PRIMARY KEY, day TEXT, family TEXT, sym TEXT, side TEXT, leg_id TEXT, candidate_at INTEGER,
    candidate_price REAL, status TEXT, end_status TEXT, end_at INTEGER, ema TEXT, leg_start TEXT, received_at REAL,
    mode TEXT);
CREATE TABLE IF NOT EXISTS v2_candidate_log (
    cand_id TEXT, day TEXT, family TEXT, sym TEXT, side TEXT, leg_id TEXT, candidate_at INTEGER, candidate_price REAL,
    status TEXT, PRIMARY KEY (cand_id, candidate_at));
CREATE TABLE IF NOT EXISTS v2_watches (
    watch_id TEXT PRIMARY KEY, cand_id TEXT, day TEXT, family TEXT, lane TEXT, sym TEXT, side TEXT, leg_id TEXT,
    candidate_at INTEGER, candidate_price REAL, watch_at INTEGER, watch_price REAL, level REAL, pre_ema INTEGER,
    ema_candidate TEXT, ema_watch TEXT, leg_start TEXT, context TEXT, received_at REAL, state TEXT, end_at INTEGER,
    end_reason TEXT, upgrade_source TEXT, upgrade_at INTEGER, upgrade_price REAL, telegram_sent INTEGER NOT NULL
    DEFAULT 0, telegram_message_id TEXT, telegram_sent_at REAL, telegram_error TEXT, telegram_group TEXT,
    rules_version TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS v2_ema_cross (day TEXT, sym TEXT, side TEXT, at INTEGER, PRIMARY KEY (sym, at));
"""

JSON = ("ema_candidate", "ema_watch", "leg_start", "context")
WCOLS = ["watch_id", "cand_id", "day", "family", "lane", "sym", "side", "leg_id", "candidate_at", "candidate_price",
         "watch_at", "watch_price", "level", "pre_ema", "ema_candidate", "ema_watch", "leg_start", "context",
         "received_at", "state", "rules_version", "mode"]


class V2DB(ObserverDB):
    def __init__(self, path: Path):
        super().__init__(path)
        self.db.executescript(V2_SCHEMA)

    def apply_v2(self, events: list[dict], mode: str, received_at: float | None = None) -> list[str]:
        """Persist v2 events idempotently. Returns the ids of NEW watches."""
        new = []
        for ev in events:
            k = ev["kind"]
            if k == "v2_candidate":
                self.db.execute("INSERT OR IGNORE INTO v2_candidate_log VALUES (?,?,?,?,?,?,?,?,?)",
                                (ev["cand_id"], ev["day"], ev["family"], ev["sym"], ev["side"], ev["leg_id"],
                                 ev["candidate_at"], ev["candidate_price"], ev["status"]))
                if ev["status"] == "pending":
                    self.db.execute("INSERT OR IGNORE INTO v2_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (ev["cand_id"], ev["day"], ev["family"], ev["sym"], ev["side"], ev["leg_id"],
                                     ev["candidate_at"], ev["candidate_price"], ev["status"], None, None,
                                     json.dumps(ev["ema"]), json.dumps(ev["leg_start"]), ev.get("received_at"), mode))
            elif k == "v2_candidate_end":
                self.db.execute("UPDATE v2_candidates SET end_status=?, end_at=? WHERE cand_id=? AND end_status IS NULL",
                                (ev["status"], ev["end_at"], ev["cand_id"]))
            elif k == "v2_watch":
                row = {c: (json.dumps(ev.get(c), sort_keys=True) if c in JSON else ev.get(c)) for c in WCOLS}
                row["pre_ema"] = int(bool(ev["pre_ema"]))
                row["mode"] = mode
                row["received_at"] = received_at
                cur = self.db.execute(f"INSERT OR IGNORE INTO v2_watches ({','.join(WCOLS)}) "
                                      f"VALUES ({','.join('?' * len(WCOLS))})", [row[c] for c in WCOLS])
                if cur.rowcount:
                    new.append(ev["watch_id"])
            elif k == "v2_end":
                self.db.execute("UPDATE v2_watches SET state=?, end_at=?, end_reason=?, upgrade_source=?, upgrade_at=?, "
                                "upgrade_price=? WHERE watch_id=? AND state='WATCH'",
                                (ev["state"], ev["end_at"], ev["end_reason"], ev["upgrade_source"], ev["upgrade_at"],
                                 ev["upgrade_price"], ev["watch_id"]))
            elif k == "v2_ema_cross":
                self.db.execute("INSERT OR IGNORE INTO v2_ema_cross VALUES (?,?,?,?)",
                                (ev["day"], ev["sym"], ev["side"], ev["at"]))
        self.apply_fresh(events, mode)
        return new

    def mark_v2_telegram(self, watch_id: str, sent: bool, ids, error, group, at) -> None:
        self.db.execute("UPDATE v2_watches SET telegram_sent=?, telegram_message_id=?, telegram_sent_at=?, "
                        "telegram_error=?, telegram_group=? WHERE watch_id=?",
                        (int(sent), ",".join(ids or []) or None, at if sent else None, error, group, watch_id))

    def v2_watches(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM v2_watches WHERE {where} ORDER BY watch_at, watch_id", args)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for c in JSON:
                d[c] = json.loads(d[c]) if d[c] else None
            out.append(d)
        return out
