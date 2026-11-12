"""Confirmed events and options context from existing MIAS data, READ-ONLY (sqlite mode=ro; nothing is written).

Confirmed events (the "UPGRADED" link), per day:
  * paper-strategy entries B / C / D / E7 / E21 / Simple: closed trades from the strategy logs (via
    mias.paperscore.trades) plus trades still open in each strategy's day state. Strategy A (retired) and the close
    fade are not confirmations.
  * swing_trend and strong_alignment alerts from the MIAS alert outbox (side from the alert text).
  * historical days with no forward-paper log: entries from the 4-year replay export (B, C, D, Simple), timestamped at
    their entry minute.
Options bias at a moment: the latest MIAS cycle metrics row for the symbol at or before that moment (same day).
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIRM_CODES = {"B", "C", "D", "E7", "E21", "Simple"}


class ReadOnlyStore:
    """The subset of mias.store.Store that paperscore.trades needs (get), on a read-only connection."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=5000")

    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def close(self) -> None:
        self.db.close()


def _side_from_text(rule: str, text: str) -> str | None:
    plain = re.sub(r"<[^>]+>", "", text or "")
    if rule == "swing_trend":
        m = re.search(r"SWING TREND (STRONG UP|UP|STRONG DOWN|DOWN)", plain)
        return None if not m else ("bull" if "UP" in m.group(1) else "bear")
    if rule == "strong_alignment":
        if "STRONG BULLISH ALIGNMENT" in plain:
            return "bull"
        if "STRONG BEARISH ALIGNMENT" in plain:
            return "bear"
    return None


class MiasSource:
    def __init__(self, db_path: Path, cfg, replay_csv: Path | None = None):
        self.db_path, self.cfg, self.replay_csv = db_path, cfg, replay_csv
        self._csv: dict[str, list[dict]] | None = None

    def _ro(self) -> ReadOnlyStore:
        return ReadOnlyStore(self.db_path)

    def confirms(self, day: date, symbols: list[str]) -> list[dict]:
        tz: ZoneInfo = self.cfg.tz
        out: list[dict] = []
        syms = set(symbols)
        st = self._ro()
        try:
            from .. import emacross, paperscore, simple
            logged = [r for r in paperscore.trades(st, self.cfg)
                      if r["date"] == day and r["code"] in CONFIRM_CODES and r["symbol"] in syms and r["ts"]]
            for r in logged:
                out.append({"sym": r["symbol"], "side": r["side"], "ts": float(r["ts"]), "source": r["code"]})
            codes = {k: paperscore.CODES.get(k, k) for k in emacross.STRATEGIES}
            for strat, meta in emacross.STRATEGIES.items():
                if codes.get(strat) not in CONFIRM_CODES:
                    continue
                state = st.get(meta["key"]) or {}
                if state.get("date") != day.isoformat():
                    continue
                for d in (state.get("open") or {}).values():
                    if d.get("symbol") in syms:
                        out.append({"sym": d["symbol"], "side": d["side"], "ts": d["opened_t"] / 1000,
                                    "source": codes[strat]})
            sst = st.get(simple.KEY) or {}
            if sst.get("date") == day.isoformat():
                for d in (sst.get("open") or {}).values():
                    if d.get("symbol") in syms:
                        out.append({"sym": d["symbol"], "side": d["side"], "ts": d["opened_t"] / 1000,
                                    "source": "Simple"})
            t0 = datetime.combine(day, datetime.min.time(), tz).timestamp()
            rows = st.db.execute("SELECT ts, symbol, rule, text FROM alerts WHERE rule IN "
                                 "('swing_trend','strong_alignment') AND ts >= ? AND ts < ?",
                                 (t0, t0 + 86400)).fetchall()
            for ts, sym, rule, text in rows:
                side = _side_from_text(rule, text)
                if side and sym in syms:
                    out.append({"sym": sym, "side": side, "ts": float(ts), "source": rule})
            have_log = bool(logged) or any(r["date"] == day for r in paperscore.trades(st, self.cfg))
        finally:
            st.close()
        if not have_log:
            out += self._replay_entries(day, syms)
        # same trade seen both closed (minute precision) and open (exact): keep one per source / side / minute
        uniq: dict = {}
        for ev in sorted(out, key=lambda x: (x["ts"], x["sym"], x["source"])):
            uniq.setdefault((ev["sym"], ev["side"], ev["source"], int(ev["ts"] // 60)), ev)
        return sorted(uniq.values(), key=lambda x: (x["ts"], x["sym"], x["source"]))

    def _replay_entries(self, day: date, syms: set) -> list[dict]:
        if not self.replay_csv or not self.replay_csv.exists():
            return []
        if self._csv is None:
            self._csv = {}
            with self.replay_csv.open() as fh:
                for r in csv.DictReader(fh):
                    if r["strategy"] in CONFIRM_CODES:
                        self._csv.setdefault(r["date"], []).append(r)
        tz = self.cfg.tz
        out = []
        for r in self._csv.get(day.isoformat(), []):
            if r["symbol"] not in syms:
                continue
            hh, mm = (int(x) for x in r["entry_time"].split(":"))
            ts = datetime.combine(day, datetime.min.time().replace(hour=hh, minute=mm), tz).timestamp()
            out.append({"sym": r["symbol"], "side": r["side"], "ts": ts, "source": r["strategy"] + "(replay)"})
        return out

    def options_context(self, sym: str, ts_ms: int) -> dict:
        """Options bias at or before ts from the MIAS cycle metrics (same day). Informational only."""
        ts = ts_ms / 1000
        st = self._ro()
        try:
            day0 = datetime.fromtimestamp(ts, self.cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)
            r = st.db.execute("SELECT ts, data FROM metrics WHERE symbol = ? AND ts <= ? AND ts >= ? "
                              "ORDER BY ts DESC LIMIT 1", (sym, ts, day0.timestamp())).fetchone()
        finally:
            st.close()
        if not r:
            return {"options_bias": None}
        d = json.loads(r[1])
        return {"options_bias": d.get("options_label"), "options_score": d.get("options_score"),
                "options_at": round(r[0])}
