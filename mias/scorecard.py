"""Alert scorecard: every directional alert is logged with what the stock (and a matching option) did after it.

Logged: tier-1 pullback entries and strong alignment, tier-2 trend changes, options flips and wall breaks, EMA
day-trade entries, one-sided unusual flow, and unusual flow that next-day open interest confirmed as opening.
Each gets the stock price 1 hour later (capped at the close), 1 trading day later and 5 trading days later, from
the recorded checks (metrics), signed in the alert's direction. Alerts that name a contract also get the
contract's live value at each horizon. `python -m mias scorecard --backfill` replays the recorded checks through
the current tiered rules to seed the card with history (stock moves only).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from statistics import median
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, rule TEXT NOT NULL,
    dir TEXT NOT NULL, price REAL NOT NULL, contract TEXT, premium REAL, source TEXT NOT NULL DEFAULT 'live',
    outcome TEXT NOT NULL DEFAULT '{}', done INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS signals_open ON signals(done, ts);
"""
HORIZONS = ("1h", "1d", "5d")
DEDUPE_S = 30 * 60
OPTION_FRESH_S = 30 * 60      # an option value is only taken this close to the horizon (it can't be looked up later)
NAMES = {"pullback_entry": "Pullback entry", "strong_alignment": "Strong alignment", "trend_change": "Trend change",
         "options_flip": "Options flip", "wall_break": "Wall break", "ema_entry": "EMA day trade",
         "flow": "One-sided flow", "flow_opened": "Flow confirmed by OI"}


def ensure(store) -> None:
    store.db.executescript(SCHEMA)


def record(store, ts: float, symbol: str, rule: str, direction: str, price: float | None,
           contract: str | None = None, premium: float | None = None, source: str = "live") -> bool:
    """Log one directional alert. Neutral alerts and repeats within 30 minutes are skipped."""
    if direction not in ("bull", "bear") or not price:
        return False
    ensure(store)
    dup = store.db.execute("SELECT 1 FROM signals WHERE symbol=? AND rule=? AND dir=? AND source=? AND ts>? "
                           "AND ts<=?", (symbol, rule, direction, source, ts - DEDUPE_S, ts)).fetchone()
    if dup:
        return False
    store.db.execute("INSERT INTO signals(ts, symbol, rule, dir, price, contract, premium, source) "
                     "VALUES(?,?,?,?,?,?,?,?)", (ts, symbol, rule, direction, price, contract, premium, source))
    return True


def _add_trading_days(d, n):
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def due(ts: float, tz: ZoneInfo) -> dict[str, float]:
    t = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
    close = datetime.combine(t.date(), time(16, 0), tz).timestamp()
    out = {"1h": min(ts + 3600, close)}
    for h, n in (("1d", 1), ("5d", 5)):
        out[h] = datetime.combine(_add_trading_days(t.date(), n), t.timetz()).timestamp()
    return out


def price_at(store, symbol: str, t: float) -> tuple[float, float] | None:
    """(ts, price) of the first recorded check at or after t, within 4 days."""
    for ts, data in store.db.execute("SELECT ts, data FROM metrics WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts "
                                     "LIMIT 3", (symbol, t, t + 4 * 86400)):
        p = json.loads(data).get("price")
        if p:
            return ts, p
    return None


def _mid(snap: dict) -> float | None:
    q = snap.get("last_quote") or {}
    if q.get("bid") and q.get("ask"):
        return (q["bid"] + q["ask"]) / 2
    return q.get("midpoint")


async def update(client, store, tz: ZoneInfo, now: datetime) -> int:
    """Fill every horizon that has come due. Returns how many were filled."""
    ensure(store)
    now_ts = now.timestamp()
    rows = store.db.execute("SELECT id, ts, symbol, dir, price, contract, premium, outcome FROM signals "
                            "WHERE done=0 AND ts>?", (now_ts - 12 * 86400,)).fetchall()
    pending, need = [], set()
    for sid, ts, sym, d, price, contract, premium, outcome in rows:
        out = json.loads(outcome)
        for h, t in due(ts, tz).items():
            if h in out or t > now_ts:
                continue
            pending.append((sid, h, t, sym, d, price, contract, premium, out))
            if contract and premium and now_ts - t <= OPTION_FRESH_S:
                need.add(contract)
    quotes = {}
    if need and client is not None:
        try:
            snaps = await client.snapshots(sorted(need))
            quotes = {k: _mid(v) for k, v in snaps.items()}
        except Exception as exc:
            log.warning("scorecard option quotes failed: %s", exc)
    filled = 0
    outs: dict[int, dict] = {}
    for sid, h, t, sym, d, price, contract, premium, out in pending:
        out = outs.setdefault(sid, out)
        got = price_at(store, sym, t)
        if not got:
            if now_ts - t > 4 * 86400:
                out[h] = {"missing": True}
                filled += 1
            continue
        sign = 1 if d == "bull" else -1
        res = {"price": got[1], "move": round(sign * 100 * (got[1] / price - 1), 3)}
        v = quotes.get(contract) if contract else None
        if v and premium and now_ts - t <= OPTION_FRESH_S:
            res["opt"] = round(v, 2)
            res["opt_ret"] = round(100 * (v / premium - 1), 1)
        out[h] = res
        filled += 1
    for sid, out in outs.items():
        store.db.execute("UPDATE signals SET outcome=?, done=? WHERE id=?",
                         (json.dumps(out), int(all(h in out for h in HORIZONS)), sid))
    return filled


# ---- report --------------------------------------------------------------------------------------

def stats(store, since_ts: float, source: str = "live") -> dict[str, dict]:
    ensure(store)
    out: dict[str, dict] = {}
    for rule, outcome in store.db.execute("SELECT rule, outcome FROM signals WHERE ts>=? AND source=?",
                                          (since_ts, source)):
        o = json.loads(outcome)
        s = out.setdefault(rule, {"n": 0, **{h: [] for h in HORIZONS}, **{f"opt_{h}": [] for h in HORIZONS}})
        s["n"] += 1
        for h in HORIZONS:
            if "move" in (o.get(h) or {}):
                s[h].append(o[h]["move"])
            if "opt_ret" in (o.get(h) or {}):
                s[f"opt_{h}"].append(o[h]["opt_ret"])
    return out


def _cell(vals: list[float], unit: str = "%") -> str:
    if not vals:
        return "–"
    hit = 100 * sum(v > 0 for v in vals) / len(vals)
    return f"{hit:.0f}% right, median {median(vals):+.{1 if unit == '%' and abs(median(vals)) < 10 else 0}f}{unit}"


def report(store, now: datetime, days: int = 7, source: str = "live") -> str:
    since = now.timestamp() - days * 86400
    st = stats(store, since, source)
    title = "replayed history" if source == "replay" else f"last {days} days"
    lines = [f"📈 <b>Alert scorecard</b> ({title})"]
    if not st:
        lines.append("Nothing logged yet: alerts are scored once they fire (1 hour, 1 day and 5 days later).")
        return "\n".join(lines)
    lines.append("<i>Right = the stock moved the alert's way. Option = the named contract's return.</i>")
    for rule, s in sorted(st.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"\n<b>{NAMES.get(rule, rule)}</b> ({s['n']})")
        lines.append("  stock: " + " · ".join(f"{h} {_cell(s[h])}" for h in HORIZONS))
        opt = [f"{h} {_cell(s['opt_' + h])}" for h in HORIZONS if s["opt_" + h]]
        if opt:
            lines.append("  option: " + " · ".join(opt))
    lines.append("\n<i>Small samples swing a lot: trust a rule after ~30 scored alerts.</i>")
    return "\n".join(lines)


# ---- backfill from recorded checks ---------------------------------------------------------------

def backfill(store, cfg) -> int:
    """Replay the recorded checks through the tiered rules and score them from the same history."""
    from . import signals
    from .trend import label_for
    ensure(store)
    store.db.execute("DELETE FROM signals WHERE source='replay'")
    rows = store.db.execute("SELECT ts, symbol, data FROM metrics ORDER BY ts").fetchall()
    w = cfg.trend_profiles.get("intraday", {})
    states: dict[str, dict] = {}
    n = 0
    for ts, sym, data in rows:
        d = json.loads(data)
        tfs = d.get("timeframes")
        if d.get("stock_score") is None or not tfs or not d.get("price"):
            continue
        used = {tf: x for tf, x in w.items() if tf in tfs}
        intra = sum(tfs[tf]["score"] * x for tf, x in used.items()) / sum(used.values()) if used else 0.0
        stock = NS(score=d["stock_score"], label=d["stock_label"], timeframes=tfs,
                   composites={"swing": (d["stock_score"], d["stock_label"]),
                               "intraday": (intra, label_for(intra, cfg.trend_strong, cfg.trend_bias))})
        opts = d.get("options") or {}
        bias = NS(score=d["options_score"], label=d["options_label"], components=d.get("options_components"),
                  session_net_premium=d.get("session_net_premium") or 0) if d.get("options_score") is not None else None
        r = NS(symbol=sym, stock=stock, bias=bias, price=d["price"], price_is_live=d.get("price_live"),
               day_change_pct=d.get("day_change_pct"),
               options=NS(thin=bool(opts.get("thin")), atm_iv_front=opts.get("atm_iv_front"),
                          atm_iv_month=opts.get("atm_iv_month"), call_wall=opts.get("call_wall"),
                          put_wall=opts.get("put_wall")) if opts else None)
        st = states.setdefault(sym, {})
        try:
            t1, t2 = signals.evaluate_tiered(r, st, cfg, ts, [])
        except Exception:
            continue
        for x in t1 + [x for x in t2 if x.rule in ("trend_change", "options_flip", "wall_break")]:
            n += record(store, ts, sym, x.rule, x.direction, d["price"], source="replay")
    for sid, ts, sym, dr, price in store.db.execute(
            "SELECT id, ts, symbol, dir, price FROM signals WHERE source='replay'").fetchall():
        out = {}
        for h, t in due(ts, cfg.tz).items():
            got = price_at(store, sym, t)
            if got:
                sign = 1 if dr == "bull" else -1
                out[h] = {"price": got[1], "move": round(sign * 100 * (got[1] / price - 1), 3)}
        store.db.execute("UPDATE signals SET outcome=?, done=? WHERE id=?",
                         (json.dumps(out), int(len(out) == len(HORIZONS)), sid))
    return n
