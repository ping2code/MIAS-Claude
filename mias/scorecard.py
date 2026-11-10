"""Alert scorecard: every directional alert is logged with what the stock (and a matching option) did after it.

Logged: tier-1 pullback entries and strong alignment, tier-2 trend changes, options flips and wall breaks, EMA
day-trade entries, one-sided unusual flow (also split by whether price confirmed it: the stock's side of VWAP and
the market), and unusual flow that next-day open interest confirmed as opening.
Each gets the stock price 15 and 30 minutes and 1 hour later (capped at the close; a check within 20 minutes of the
horizon is needed), 1 trading day later and 5 trading days later, from
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
HORIZONS = ("15m", "30m", "1h", "1d", "5d")
INTRADAY = {"15m": 900, "30m": 1800, "1h": 3600}
INTRADAY_GAP_S = 20 * 60
DEDUPE_S = 30 * 60
OPTION_FRESH_S = 30 * 60      # an option value is only taken this close to the horizon (it can't be looked up later)
NAMES = {"pullback_entry": "Pullback entry", "strong_alignment": "Strong alignment", "swing_trend": "Swing trend turn",
         "trend_change": "Trend change",
         "options_flip": "Options flip", "wall_break": "Wall break", "ema_entry": "EMA day trade (retest)",
         "ema_cross_entry": "EMA day trade (cross)", "ema_qual_entry": "EMA day trade (qualified, experimental)", "ema_d_entry": "EMA day trade (D, experimental)",
         "flow": "One-sided flow", "flow_confirmed": "Flow, price confirms",
         "flow_unconfirmed": "Flow, price disagrees", "flow_opened": "Flow confirmed by OI"}


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
    out = {h: min(ts + secs, close) for h, secs in INTRADAY.items()}
    for h, n in (("1d", 1), ("5d", 5)):
        out[h] = datetime.combine(_add_trading_days(t.date(), n), t.timetz()).timestamp()
    return out


def _within(h: str) -> float:
    return INTRADAY_GAP_S if h in INTRADAY else 4 * 86400


def price_at(store, symbol: str, t: float, within: float = 4 * 86400) -> tuple[float, float] | None:
    """(ts, price) of the first recorded check at or after t, within `within` seconds (default 4 days)."""
    for ts, data in store.db.execute("SELECT ts, data FROM metrics WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts "
                                     "LIMIT 3", (symbol, t, t + within)):
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
        got = price_at(store, sym, t, _within(h))
        if not got:
            if now_ts - t > _within(h):
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


# ---- fair measurement (report only: nothing here feeds an alert, a threshold or a strategy) -------
#
# For an alert on symbol X at time t0, scored at horizon time t1 (due()):
#   ret    = 100 * (P_X(t1) / P_X(t0) - 1)            raw stock return, unsigned
#   spy    = 100 * (P_SPY(t1) / P_SPY(t0) - 1)        SPY return over the same interval
#   raw    = sign * ret                                direction-adjusted raw move (the stored "move")
#   excess = sign * (ret - spy)                        direction-adjusted excess vs SPY
# with sign = +1 for a bullish alert and -1 for a bearish one, so a positive raw / excess is always the favourable
# outcome (bullish: X went up / beat SPY; bearish: X went down / lagged SPY). P_X(t0) is the alert's price; every
# other price is the first recorded check at or after the time (the same lookup the stock score uses; alerts are
# logged at their check's timestamp). Alerts on SPY itself have no excess: "N/A — benchmark is SPY".
# A "right" alert has raw > 0. Option returns stay separate from stock returns and are never blended in.

BENCH = "SPY"
REGIME_FLAT_PCT = 0.25        # SPY's close-to-close day change within ±0.25% is a FLAT day (fixed, not tuned)
REGIMES = ("UP", "DOWN", "FLAT")
EVIDENCE_DAYS = ((20, "DEVELOPING"), (10, "EARLY"), (5, "LIMITED"), (0, "VERY LIMITED"))
EVIDENCE_MIN_ALERTS = 10      # fewer alerts than this drops the day-based label one step
CI_MIN_DAYS, CI_MIN_ALERTS = 5, 10
_T95 = {4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
        25: 2.060, 30: 2.042}
SHORT = {"pullback_entry": "Pullback", "strong_alignment": "Strong align", "swing_trend": "Swing turn",
         "trend_change": "Trend change",
         "options_flip": "Options flip", "wall_break": "Wall break", "ema_entry": "EMA retest",
         "ema_cross_entry": "EMA cross", "ema_qual_entry": "EMA qual (C)", "ema_d_entry": "EMA D",
         "flow": "Flow", "flow_confirmed": "Flow px agrees", "flow_unconfirmed": "Flow px disagrees",
         "flow_opened": "Flow OI-confirmed"}
ET = ZoneInfo("America/New_York")


def excess(direction: str, ret: float, spy_ret: float) -> float:
    """Direction-adjusted excess return vs SPY, in percentage points (positive = favourable for the alert)."""
    return (1 if direction == "bull" else -1) * (ret - spy_ret)


def evidence(alerts: int, days: int) -> str:
    """Evidence label: distinct trading days set the level; fewer than 10 alerts drops it one step."""
    levels = [name for _, name in EVIDENCE_DAYS]
    i = next(k for k, (lo, _) in enumerate(EVIDENCE_DAYS) if days >= lo)
    if alerts < EVIDENCE_MIN_ALERTS:
        i = min(i + 1, len(levels) - 1)
    return levels[i]


def concentration(days: list) -> dict:
    """How the alerts spread over trading days (one entry per alert, its trading day)."""
    per: dict = {}
    for d in days:
        per[d] = per.get(d, 0) + 1
    counts = sorted(per.values(), reverse=True)
    n = len(days)
    return {"alerts": n, "days": len(per), "median_per_day": median(counts) if counts else 0,
            "max_per_day": counts[0] if counts else 0,
            "top1_pct": 100 * sum(counts[:1]) / n if n else 0.0, "top3_pct": 100 * sum(counts[:3]) / n if n else 0.0}


def clustered_ci(values: list[float], days: list) -> tuple[float, float] | None:
    """95% interval for the mean of `values`, with each trading day as a cluster (alerts on one day move together).

    mean ± t(k-1) × SE, SE = sqrt(k/(k-1) × Σ_day (Σ_alerts_that_day (x - mean))²) / n, k = trading days.
    None (insufficient sample) below 5 days or 10 values."""
    n, k = len(values), len(set(days))
    if k < CI_MIN_DAYS or n < CI_MIN_ALERTS:
        return None
    m = sum(values) / n
    sums: dict = {}
    for v, d in zip(values, days):
        sums[d] = sums.get(d, 0.0) + (v - m)
    se = (k / (k - 1) * sum(x * x for x in sums.values())) ** 0.5 / n
    df = k - 1
    t = next((_T95[x] for x in sorted(_T95, reverse=True) if df >= x), 2.776) if df <= 30 else 1.96
    return m - t * se, m + t * se


def _series(store, symbol: str) -> tuple[list[float], list[dict]]:
    ts, rows = [], []
    for t, data in store.db.execute("SELECT ts, data FROM metrics WHERE symbol=? ORDER BY ts", (symbol,)):
        d = json.loads(data)
        if d.get("price"):
            ts.append(t)
            rows.append(d)
    return ts, rows


def _first_at(series, t: float, within: float) -> float | None:
    from bisect import bisect_left
    ts, rows = series
    i = bisect_left(ts, t)
    return rows[i]["price"] if i < len(ts) and ts[i] < t + within else None


def spy_regimes(store, tz: ZoneInfo = ET, series=None) -> dict:
    """Trading day -> UP / DOWN / FLAT from SPY's day change vs the prior close at the day's last check up to 16:00."""
    ts, rows = series or _series(store, BENCH)
    out: dict = {}
    for t, d in zip(ts, rows):
        local = datetime.fromtimestamp(t, tz)
        if local.time() > time(16, 1) or d.get("day_change_pct") is None:
            continue
        chg = d["day_change_pct"]
        out[local.date()] = "UP" if chg > REGIME_FLAT_PCT else "DOWN" if chg < -REGIME_FLAT_PCT else "FLAT"
    return out


def scored(store, since_ts: float, source: str, tz: ZoneInfo = ET, series=None, regimes=None) -> list[dict]:
    """One dict per logged alert with its per-horizon raw, SPY and excess returns (computed here; rows unchanged)."""
    ensure(store)
    series = series or _series(store, BENCH)
    regimes = regimes if regimes is not None else spy_regimes(store, tz, series)
    out = []
    for ts, sym, rule, d, price, outcome in store.db.execute(
            "SELECT ts, symbol, rule, dir, price, outcome FROM signals WHERE ts>=? AND source=? ORDER BY ts",
            (since_ts, source)):
        o, sign = json.loads(outcome), (1 if d == "bull" else -1)
        day = datetime.fromtimestamp(ts, tz).date()
        s0 = _first_at(series, ts, INTRADAY_GAP_S)
        hz = {}
        for h, t in due(ts, tz).items():
            x = o.get(h) or {}
            if "move" not in x:
                continue
            ret = 100 * (x["price"] / price - 1) if x.get("price") else sign * x["move"]
            v = {"raw": x["move"], "ret": ret, "spy": None, "xs": None, "bench": sym == BENCH}
            if sym != BENCH:
                s1 = _first_at(series, t, _within(h))
                if s0 and s1:
                    v["spy"] = 100 * (s1 / s0 - 1)
                    v["xs"] = excess(d, ret, v["spy"])
            if "opt_ret" in x:
                v["opt"] = x["opt_ret"]
            hz[h] = v
        out.append({"ts": ts, "symbol": sym, "rule": rule, "dir": d, "source": source, "day": day,
                    "regime": regimes.get(day), "h": hz})
    return out


def summarize(alerts: list[dict], h: str) -> dict | None:
    """Stats for the alerts scored at horizon h: right %, raw and excess (vs SPY) median / mean, days, CIs."""
    rows = [a for a in alerts if h in a["h"]]
    if not rows:
        return None
    raw = [a["h"][h]["raw"] for a in rows]
    days = [a["day"] for a in rows]
    xr = [a for a in rows if a["h"][h]["xs"] is not None]
    xs = [a["h"][h]["xs"] for a in xr]
    opt = [a["h"][h]["opt"] for a in rows if "opt" in a["h"][h]]
    hit_ci = clustered_ci([1.0 if v > 0 else 0.0 for v in raw], days)
    return {"n": len(rows), "days": len(set(days)), "right": 100 * sum(v > 0 for v in raw) / len(raw),
            "raw_med": median(raw), "raw_mean": sum(raw) / len(raw),
            "xs_n": len(xs), "xs_med": median(xs) if xs else None, "xs_mean": sum(xs) / len(xs) if xs else None,
            "bench_n": sum(a["h"][h]["bench"] for a in rows),
            "right_ci": (100 * hit_ci[0], 100 * hit_ci[1]) if hit_ci else None,
            "xs_ci": clustered_ci(xs, [a["day"] for a in xr]),
            "opt_n": len(opt), "opt_right": 100 * sum(v > 0 for v in opt) / len(opt) if opt else None,
            "opt_med": median(opt) if opt else None, "opt_mean": sum(opt) / len(opt) if opt else None,
            "evidence": evidence(len(rows), len(set(days)))}


def by_regime(alerts: list[dict], h: str) -> dict:
    return {g: summarize([a for a in alerts if a["regime"] == g], h) for g in REGIMES}


def _p(v: float | None, digits: int = 1) -> str:
    return "–" if v is None else f"{v:+.{digits}f}%"


def _stock(s: dict) -> str:
    out = (f"{s['right']:.0f}% right · raw med {_p(s['raw_med'], 2)}, mean {_p(s['raw_mean'], 2)} · ")
    if s["xs_n"] == 0:
        out += "vs SPY N/A — benchmark is SPY"
    else:
        out += f"vs SPY med {_p(s['xs_med'], 2)}, mean {_p(s['xs_mean'], 2)}"
        if s["bench_n"]:
            out += f" ({s['bench_n']} SPY alert{'s' if s['bench_n'] > 1 else ''} N/A)"
    return out + f" · {s['n']} alerts / {s['days']} day{'s' if s['days'] != 1 else ''}"


def _ci(s: dict) -> str:
    if s["right_ci"] is None:
        return (f"95% CI: insufficient sample ({s['n']} alerts / {s['days']} day{'s' if s['days'] != 1 else ''}; "
                f"needs ≥{CI_MIN_ALERTS} alerts and ≥{CI_MIN_DAYS} days)")
    out = f"95% CI (by day): right {s['right_ci'][0]:.0f}–{s['right_ci'][1]:.0f}%"
    out += (f" · vs SPY mean {_p(s['xs_ci'][0], 2)} to {_p(s['xs_ci'][1], 2)}" if s["xs_ci"]
            else " · vs SPY mean: insufficient sample" if s["xs_n"] else "")
    return out


def _option(s: dict) -> str:
    return (f"{s['opt_right']:.0f}% right · med {_p(s['opt_med'])}, mean {_p(s['opt_mean'])} · "
            f"{s['opt_n']} contracts")


def _section(alerts: list[dict], label: str, horizons: tuple, full: bool) -> list[str]:
    c = concentration([a["day"] for a in alerts])
    s1 = summarize(alerts, "1d")
    ev = s1["evidence"] if s1 else evidence(c["alerts"], c["days"])
    lines = [f"  <b>{label}</b> · {c['alerts']} alerts across {c['days']} trading day{'s' if c['days'] != 1 else ''}"
             f" · Evidence (1d): {ev}",
             f"    per day: median {c['median_per_day']:g}, max {c['max_per_day']} · top day {c['top1_pct']:.0f}%"
             f" of alerts, top 3 days {c['top3_pct']:.0f}%"]
    for h in horizons:
        for name, part in (("bull", [a for a in alerts if a["dir"] == "bull"]),
                           ("bear", [a for a in alerts if a["dir"] == "bear"]), ("all", alerts)):
            s = summarize(part, h)
            if s:
                lines.append(f"    {h} {name}: {_stock(s)}")
        s = summarize(alerts, h)
        if s and (full or h == "1d"):
            lines.append(f"    {h} {_ci(s)}")
        if s and s["opt_n"]:
            lines.append(f"    {h} option: {_option(s)}")
    for h in (("1h", "1d") if full else ("1d",)):
        reg = by_regime(alerts, h)
        cells = [f"{g} {r['n']}: {r['right']:.0f}% right, raw med {_p(r['raw_med'], 2)}, vs SPY med "
                 f"{_p(r['xs_med'], 2) if r['xs_n'] else 'N/A'}" for g, r in reg.items() if r]
        if cells:
            lines.append(f"    {h} by SPY day: " + " │ ".join(cells))
    return lines


def _summary_table(groups: dict) -> list[str]:
    def pct(s, key="right"):
        return f"{s[key]:.0f}" if s else "–"
    rows = [f"{'Rule':<17} {'Src':<3} {'Alrt':>4} {'Days':>4} {'Bull':>4} {'Bear':>4} {'1d%':>4} {'1dXS':>6}  Evidence"]
    for rule in sorted(groups, key=lambda r: -sum(len(v) for v in groups[r].values())):
        for src, alerts in groups[rule].items():
            if not alerts:
                continue
            s = summarize(alerts, "1d")
            bull = summarize([a for a in alerts if a["dir"] == "bull"], "1d")
            bear = summarize([a for a in alerts if a["dir"] == "bear"], "1d")
            xs = "N/A" if s and not s["xs_n"] else (f"{s['xs_med']:+.2f}" if s else "–")
            c = concentration([a["day"] for a in alerts])
            rows.append(f"{SHORT.get(rule, rule)[:17]:<17} {'L' if src == 'live' else 'R':<3} {c['alerts']:>4} "
                        f"{c['days']:>4} {pct(bull):>4} {pct(bear):>4} {pct(s):>4} {xs:>6}  "
                        f"{s['evidence'] if s else evidence(c['alerts'], c['days'])}")
    return ["<pre>" + "\n".join(rows) + "</pre>"]


def _span(alerts: list[dict]) -> str:
    days = sorted({a["day"] for a in alerts})
    return f"{days[0]:%b %d}–{days[-1]:%b %d}" if days else "none"


def report(store, now: datetime, days: int = 7, source: str = "both", full: bool = False, tz: ZoneInfo = ET) -> str:
    """Scorecard text. source: both (live for the last `days` days next to all replayed history), live or replay.
    full: every horizon, intervals and 1h regimes; otherwise 1h / 1d per rule."""
    series = _series(store, BENCH)
    regimes = spy_regimes(store, tz, series)
    srcs = ("live", "replay") if source == "both" else (source,)
    data = {src: scored(store, now.timestamp() - days * 86400 if src == "live" else 0, src, tz, series, regimes)
            for src in srcs}
    heads = {"live": f"live last {days} days ({_span(data.get('live', []))})",
             "replay": f"replayed history ({_span(data.get('replay', []))})"}
    lines = ["📈 <b>Alert scorecard</b> — " + " · ".join(heads[s] for s in srcs)]
    if not any(data.values()):
        lines.append("Nothing logged yet: alerts are scored once they fire (1 hour, 1 day and 5 days later).")
        return "\n".join(lines)
    groups: dict = {}
    for src in srcs:
        for a in data[src]:
            groups.setdefault(a["rule"], {s: [] for s in srcs})[src].append(a)
    lines.append("<i>Report only. Right = the stock moved the alert's way. Raw = direction-adjusted stock move. "
                 "vs SPY = direction-adjusted excess over SPY's move in the same window (bearish: SPY move minus the "
                 "stock's). L = live alerts, R = replayed through today's rules (stock only, no options); shown "
                 "separately, never merged, as they cover overlapping days. Bull / Bear / 1d% = right after 1 day; "
                 "1dXS = median 1-day excess vs SPY (points).</i>")
    lines += _summary_table(groups)
    lines.append(f"<i>Evidence by distinct trading days: &lt;5 VERY LIMITED · 5–9 LIMITED · 10–19 EARLY · 20+ "
                 f"DEVELOPING; under {EVIDENCE_MIN_ALERTS} alerts drops one step. Nothing here is validated. "
                 f"SPY day: UP / DOWN beyond ±{REGIME_FLAT_PCT}% close to close, else FLAT.</i>")
    horizons = HORIZONS if full else ("1d",)
    for rule in sorted(groups, key=lambda r: -sum(len(v) for v in groups[r].values())):
        lines.append(f"\n<b>{NAMES.get(rule, rule)}</b>")
        for src in srcs:
            if groups[rule][src]:
                lines += _section(groups[rule][src], "LIVE" if src == "live" else "REPLAYED", horizons, full)
    if not full:
        lines.append("\n<i>/scorecard full: every horizon (15m–5d) with intervals, and 1h SPY-day splits.</i>")
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
            got = price_at(store, sym, t, _within(h))
            if got:
                sign = 1 if dr == "bull" else -1
                out[h] = {"price": got[1], "move": round(sign * 100 * (got[1] / price - 1), 3)}
        store.db.execute("UPDATE signals SET outcome=?, done=? WHERE id=?",
                         (json.dumps(out), int(len(out) == len(HORIZONS)), sid))
    return n
