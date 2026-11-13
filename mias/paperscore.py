"""Paper Strategy Scorecard: every paper strategy's forward record, measured fairly (REPORT ONLY).

Separate from the Alert Scorecard (mias/scorecard.py). Reads the paper trade logs (emacross LOG_KEY for A-E21,
simple LOG_KEY, closeplay LOG_KEY); writes nothing and changes no strategy. Net P/L = logged P/L (bought at the ask,
sold at the bid) minus commissions of fee_per_contract per contract each way. Forward paper trades and HISTORICAL
backtests are shown side by side, never merged. Evidence comes from distinct trading days first (trade count can
only lower it); nothing is ever labelled validated. Market context (SPY day UP / DOWN / FLAT at ±0.25%, the Alert
Scorecard convention, and SPY's day change at entry) is diagnostic only. Several strategies can act on one setup:
trades with the same symbol and direction entered within 10 minutes on the same day count as one setup
(setup_id = date_SYMBOL_SIDE_HHMMSS of its first entry). The setup-level view sums every expression's premium and
P/L per setup and shows several stated ways to count a setup once; it applies no risk cap.
python -m mias paper-scorecard [--full] · /paperscore [full]
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from statistics import median

from . import closeplay, emacross, simple

CODES = {"retest": "A", "cross": "B", "qualified": "C", "ema_9_21_d": "D", "ema_9_21_e7": "E7", "ema_9_21_e21": "E21"}
ORDER = ("A", "B", "C", "D", "E7", "E21", "Simple", "Close fade")
NAMES = {"A": "EMA Retest (A)", "B": "EMA Cross (B)", "C": "Qualified (C)", "D": "Strategy D", "E7": "E7 (7DTE)",
         "E21": "E21 (21DTE)", "Simple": "Simple EMA — SPY/QQQ/IWM", "Close fade": "3:45 PM close fade"}
# Retired from active paper trading (removed from [daytrade] strategies); code and logged trades are kept.
RETIRED = {"A": "2026-11-09"}
# Paused from NEW forward paper entries (2026-11-13, user decision; "cross" removed from [daytrade] strategies,
# [ema_simple] enabled = false). Benchmark / research only: code, logged trades, replays and backtests are kept and
# still reported. Shown as paused only while the configuration has them off; re-enabling makes them active again.
PAUSED = {"B": "2026-11-13 00:59 ET", "Simple": "2026-11-13 00:59 ET"}
EXITS = {"target": "target", "stop": "stop", "invalidated": "invalidation", "time": "time", "trailed": "trailing",
         "breakeven": "breakeven", "force": "force-close", "force_close": "force-close"}
EXIT_ORDER = ("target", "stop", "invalidation", "time", "trailing", "breakeven", "force-close", "other")
EXIT_NOTE = "Exit buckets are descriptive. Loss concentration under STOP does not establish that the stop rule caused the losses."
LEVELS = ("VERY LIMITED", "LIMITED", "EARLY", "DEVELOPING", "MORE MATURE")
MIN_TRADES = 30            # fewer trades lowers the day-based evidence label one step
OVERLAP_S = 600            # same symbol + direction within 10 minutes = one underlying setup
CORRELATED = ("C", "D", "E7", "E21")
# Backtests already run (research notes in the project history). Different engines and sizing from the forward
# paper record, so never added to it.
HISTORICAL = {
    "A": "Oct 2022–Oct 2026 replay as configured live, minute bars, 5 contracts, after fees: 8,932 trades, "
         "−$365,800; negative every year",
    "B": "Oct 2022–Oct 2026 replay, minute bars, 5 contracts, after fees: 17,718 trades, −$809,529; negative every year",
    "C": "Oct 2022–Oct 2026 replay, 5 contracts, after fees: 188 trades, +$1,482 (Oct 2022–24 −$720)",
    "D": "Oct 2022–Oct 2026 replay, 5 contracts, after fees: 311 trades, +$3,106 ($10/trade, 95% CI −$44 to +$66; "
         "Oct 2022–24 −$5,824)",
    "E7": "NBBO replay per contract after fees: Oct 2024–26 160 trades +$592 PF 1.06; Oct 2022–24 −$2,223; "
          "4-year total negative",
    "E21": "NBBO replay per contract after fees: Oct 2024–26 155 trades +$440 PF 1.04; Oct 2022–24 −$2,242; "
           "4-year total negative",
    "Simple": "Oct 2022–Oct 2026 NBBO replay, 5 contracts, after fees: 4,921 trades, −$170,826, PF 0.65, "
              "win 40.6%, −$34.71/trade; every symbol-year negative",
    "Close fade": "2-year SPY backtest (before trading SPX): " + closeplay.BACKTEST_NOTE,
}


# ---- trades ------------------------------------------------------------------------------------------

def _ts(day: str, hhmm: str | None, tz) -> float | None:
    if not hhmm:
        return None
    parts = [int(p) for p in hhmm.split(":")]
    h, m, s = (parts + [0, 0])[:3]
    return datetime(*map(int, day.split("-")), h, m, s, tzinfo=tz).timestamp()


def trades(store, cfg) -> list[dict]:
    """Every logged paper trade, normalised: strategy code, day, entry time, side, contracts, gross / fee / net."""
    tz, fee, lots = cfg.tz, cfg.daytrade.fee_per_contract, cfg.daytrade.contracts
    out = []

    def add(code, t, qty, exit_reason, entry_time, exit_time=None):
        ts = _ts(t["date"], entry_time, tz)
        hold = t.get("minutes")
        exit_ts = ts + 60 * float(hold) if ts is not None and hold is not None else _ts(t["date"], exit_time, tz)
        out.append({"code": code, "date": date.fromisoformat(t["date"]), "symbol": t.get("symbol"),
                    "side": t.get("side"), "qty": qty, "gross": float(t["pnl"]), "fee": 2 * fee * qty,
                    "net": float(t["pnl"]) - 2 * fee * qty, "exit": EXITS.get(exit_reason or "", "other"),
                    "hold": hold, "ts": ts, "entry_time": entry_time, "exit_ts": exit_ts,
                    "premium": float(t.get("entry") or 0) * 100 * qty, "ticker": t.get("ticker"),
                    "n": t.get("n"), "ema_regime": t.get("regime"), "context": t.get("context")})
    for t in store.get(emacross.LOG_KEY) or []:
        add(CODES.get(t.get("strategy", "retest"), t.get("strategy")), t, t.get("contracts") or lots,
            t.get("exit_reason"), t.get("fill_at") or t.get("in"))
    for t in store.get(simple.LOG_KEY) or []:
        add("Simple", t, lots, t.get("exit_reason"), t.get("fill_at") or t.get("in"))
    for t in store.get(closeplay.LOG_KEY) or []:
        add("Close fade", t, t.get("qty") or 1, "time", "15:45", "15:59")   # scheduled sale at 15:59 = a time exit
    out.sort(key=lambda r: (r["date"], r["ts"] or 0))
    return out


def with_market(store, rows: list[dict], tz) -> list[dict]:
    """Adds the SPY day regime (UP / DOWN / FLAT) and SPY's day change at the last check at or before entry."""
    from .scorecard import _series, spy_regimes
    series = _series(store, "SPY")
    regimes = spy_regimes(store, tz, series)
    ts, data = series
    for r in rows:
        r["regime"] = regimes.get(r["date"])
        r["spy_at_entry"] = None
        if r["ts"]:
            i = bisect_right(ts, r["ts"]) - 1
            if i >= 0 and datetime.fromtimestamp(ts[i], tz).date() == r["date"]:
                r["spy_at_entry"] = data[i].get("day_change_pct")
    return rows


# ---- measures ----------------------------------------------------------------------------------------

def evidence(n: int, days: int) -> str:
    i = 0 if days < 5 else 1 if days < 10 else 2 if days < 20 else 3 if days < 40 else 4
    return LEVELS[max(0, i - 1) if n < MIN_TRADES else i]


def insufficient(n: int, days: int) -> bool:
    return evidence(n, days) in LEVELS[:2]


def max_drawdown(nets: list[float]) -> float:
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def stats(rows: list[dict]) -> dict:
    nets = [r["net"] for r in rows]
    n = len(nets)
    if not n:
        return {"n": 0, "days": 0}
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    holds = [r["hold"] for r in rows if r.get("hold") is not None]
    side = {s: [r["net"] for r in rows if r["side"] == s] for s in ("bull", "bear")}
    days = len({r["date"] for r in rows})
    return {"n": n, "days": days, "wins": len(wins), "losses": len(losses), "win": 100 * len(wins) / n,
            "gross": sum(r["gross"] for r in rows), "fees": sum(r["fee"] for r in rows), "net": sum(nets),
            "per_trade": sum(nets) / n, "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "pf": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "max_dd": max_drawdown(nets), "best": max(nets), "worst": min(nets),
            "hold_med": median(holds) if holds else None,
            "calls": (len(side["bull"]), sum(side["bull"])), "puts": (len(side["bear"]), sum(side["bear"])),
            "evidence": evidence(n, days)}


def by_period(rows: list[dict], week: bool = False) -> dict:
    out: dict = {}
    for r in rows:
        k = r["date"] - timedelta(days=r["date"].weekday()) if week else r["date"]
        out[k] = out.get(k, 0.0) + r["net"]
    return dict(sorted(out.items()))


def day_summary(rows: list[dict]) -> dict:
    d = by_period(rows)
    if not d:
        return {"positive": 0, "negative": 0, "best": None, "worst": None}
    best, worst = max(d.items(), key=lambda kv: kv[1]), min(d.items(), key=lambda kv: kv[1])
    return {"positive": sum(v > 0 for v in d.values()), "negative": sum(v < 0 for v in d.values()),
            "best": best, "worst": worst}


def by_key(rows: list[dict], key) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return {k: stats(v) for k, v in groups.items()}


def concentration(rows: list[dict]) -> dict | None:
    """Per-symbol nets: best / worst symbol, and the share of all positive symbol P/L from the best and top 3."""
    sym = {k: v["net"] for k, v in by_key(rows, lambda r: r["symbol"]).items()}
    if not sym:
        return None
    ranked = sorted(sym.items(), key=lambda kv: -kv[1])
    positive = sum(v for v in sym.values() if v > 0)
    return {"best": ranked[0], "worst": ranked[-1],
            "best_share": 100 * ranked[0][1] / positive if positive and ranked[0][1] > 0 else None,
            "top3_share": 100 * sum(v for _, v in ranked[:3] if v > 0) / positive if positive else None}


def without(rows: list[dict], symbol: str) -> dict:
    return stats([r for r in rows if r["symbol"] != symbol])


def outliers(rows: list[dict]) -> dict:
    nets = sorted(r["net"] for r in rows)
    total = sum(nets)
    return {"net": total, "no_best": total - nets[-1], "no_top3": total - sum(nets[-3:]), "no_worst": total - nets[0]}


def setups(rows: list[dict]) -> list[list[dict]]:
    """Trades grouped into underlying setups: same day, symbol and direction, entered within OVERLAP_S."""
    groups: list[list[dict]] = []
    for r in sorted(rows, key=lambda r: (r["date"], r["symbol"], r["side"], r["ts"] or 0)):
        g = groups[-1] if groups else None
        if g and (g[-1]["date"], g[-1]["symbol"], g[-1]["side"]) == (r["date"], r["symbol"], r["side"]) \
                and r["ts"] is not None and g[-1]["ts"] is not None and r["ts"] - g[-1]["ts"] <= OVERLAP_S:
            g.append(r)
        else:
            groups.append([r])
    return groups


def setup_id(group: list[dict], tz) -> str:
    """Deterministic id of one setup from its own facts only (never a strategy name): trading date, symbol, direction
    and the earliest member's entry time, e.g. 2026-11-03_MSFT_BULL_101001. Members are chained by setups(): each
    within OVERLAP_S of the previous one, so the anchor is the setup's first entry."""
    first = group[0]
    when = datetime.fromtimestamp(first["ts"], tz).strftime("%H%M%S") if first.get("ts") is not None else "NA"
    return f"{first['date'].isoformat()}_{first['symbol']}_{(first['side'] or '').upper()}_{when}"


def with_setups(rows: list[dict], tz) -> list[dict]:
    """Adds setup_id and setup_size (strategy observations sharing it) to every row, grouping ALL rows given."""
    for g in setups(rows):
        sid = setup_id(g, tz)
        for r in g:
            r["setup_id"], r["setup_size"] = sid, len(g)
    return rows


def family(code: str) -> str:
    return "D-family" if code in CORRELATED else code


def max_concurrent(rows: list[dict]) -> tuple[float, float | None]:
    """Largest sum of premium held at one moment ([entry, exit) intervals) and when it happened (epoch s)."""
    ev = []
    for r in rows:
        if r.get("ts") is not None and r.get("exit_ts") is not None:
            ev += [(r["ts"], 1, r["premium"]), (max(r["exit_ts"], r["ts"]), 0, -r["premium"])]
    best, at, cur = 0.0, None, 0.0
    for t, _, x in sorted(ev):                           # exits sort before entries at the same second
        cur += x
        if cur > best + 1e-9:
            best, at = cur, t
    return best, at


def setup_stats(rows: list[dict]) -> dict:
    """Setup-level measures (REPORT ONLY). Setup P/L 'as traded' = the sum of every expression of it (what MIAS
    actually made or lost from one idea); exposure = the sum of the premium paid by every expression."""
    groups = setups(rows)
    if not groups:
        return {"obs": 0, "setups": 0}
    nets = sorted(((sum(r["net"] for r in g), g) for g in groups), key=lambda x: x[0])
    exposure = [sum(r["premium"] for r in g) for g in groups]
    total = sum(n for n, _ in nets)
    ranked = sorted((n for n, _ in nets), reverse=True)
    positive = sum(n for n in ranked if n > 0)
    sizes: dict[int, int] = {}
    for g in groups:
        sizes[len(g)] = sizes.get(len(g), 0) + 1
    big = max(range(len(groups)), key=lambda i: exposure[i])
    return {"obs": len(rows), "setups": len(groups), "multi": sum(len(g) > 1 for g in groups),
            "sizes": dict(sorted(sizes.items())), "premium": sum(r["premium"] for r in rows),
            "setup_avg_exposure": sum(exposure) / len(groups), "largest": (exposure[big], groups[big]),
            "concurrent": max_concurrent(rows), "net": total, "best": nets[-1], "worst": nets[0],
            "top1_share": 100 * ranked[0] / positive if positive and ranked[0] > 0 else None,
            "top3_share": 100 * sum(n for n in ranked[:3] if n > 0) / positive if positive else None,
            "no_top1": total - ranked[0], "no_top3": total - sum(ranked[:3])}


def count_once(rows: list[dict]) -> dict:
    """Ways to count each underlying setup once. None is canonical; each is a stated methodology:
    mean = the equal-weight average of the setup's expressions (one 'unit' of the idea);
    first = the expression that entered first (ties broken by strategy order C, D, E7, E21: a convention only);
    ret = setup net / setup premium, the return on the money the setup tied up, averaged over setups."""
    groups = setups(rows)
    if not groups:
        return {}
    first = [min(g, key=lambda r: (r["ts"] or 0, ORDER.index(r["code"]) if r["code"] in ORDER else 99)) for g in groups]
    rets = [sum(r["net"] for r in g) / sum(r["premium"] for r in g) for g in groups if sum(r["premium"] for r in g)]
    return {"setups": len(groups), "all": sum(r["net"] for r in rows),
            "mean": sum(sum(r["net"] for r in g) / len(g) for g in groups),
            "first": sum(r["net"] for r in first), "ret": 100 * sum(rets) / len(rets) if rets else None}


# ---- text --------------------------------------------------------------------------------------------

def _m(x: float | None) -> str:
    return "–" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _usd(x: float | None) -> str:
    return "–" if x is None else f"${x:,.0f}"


def _pf(s: dict) -> str:
    return "–" if s.get("pf") is None else f"{s['pf']:.2f}"


def _line(s: dict) -> str:
    if not s["n"]:
        return "no trades"
    return (f"{s['n']} trades / {s['days']} days · win {s['win']:.0f}% · net {_m(s['net'])} "
            f"({_m(s['per_trade'])}/trade) · PF {_pf(s)}")


def _configured(code: str, cfg) -> bool:
    if code == "Simple":
        return bool(cfg.ema_simple_enabled)
    if code == "Close fade":
        return bool(cfg.closeplay_enabled)
    name = next((k for k, v in CODES.items() if v == code), None)
    return name in cfg.daytrade.strategies


def _status(code: str, cfg) -> str:
    if code in RETIRED:
        return f"retired {RETIRED[code]}"
    on = _configured(code, cfg)
    if code in PAUSED and not on:
        return "paused — benchmark"
    if not on:
        return "off"
    return "active research" if code in CORRELATED else "active"


def _live(code: str, cfg) -> bool:
    return _status(code, cfg).startswith("active")


def _table(groups: dict, cfg) -> list[str]:
    rows = [f"{'Strategy':<11}{'Status':<19}{'Trd':>4}{'Days':>5}{'Win%':>5}{'Net':>9}{'/trade':>7}{'PF':>6}"
            f"{'MaxDD':>9}  Evidence"]
    for code in ORDER:
        s = stats(groups.get(code, []))
        if not s["n"]:
            rows.append(f"{code:<11}{_status(code, cfg):<19}{0:>4}")
            continue
        rows.append(f"{code:<11}{_status(code, cfg):<19}{s['n']:>4}{s['days']:>5}{s['win']:>5.0f}"
                    f"{_m(s['net']):>9}{_m(s['per_trade']):>7}{_pf(s):>6}{_m(s['max_dd']):>9}  {s['evidence']}")
    return ["<pre>" + "\n".join(rows) + "</pre>"]


def _portfolio(title: str, rows: list[dict]) -> list[str]:
    s = stats(rows)
    if not s["n"]:
        return [f"<b>{title}</b>: no trades"]
    daily = " · ".join(f"{d:%m-%d} {_m(v)}" for d, v in by_period(rows).items())
    weekly = " · ".join(f"wk {d:%m-%d} {_m(v)}" for d, v in by_period(rows, week=True).items())
    return [f"<b>{title}</b>: {s['n']} trades / {s['days']} days · gross {_m(s['gross'])} · commissions "
            f"{_m(-s['fees'])} · <b>net {_m(s['net'])}</b> · max drawdown {_m(s['max_dd'])}",
            f"  daily: {daily}", f"  weekly: {weekly}"]


def _sym_table(rows: list[dict]) -> list[str]:
    out = []
    for sym, s in sorted(by_key(rows, lambda r: r["symbol"]).items(), key=lambda kv: -kv[1]["net"]):
        out.append(f"    {sym}: {s['n']} trades · win {s['win']:.0f}% · net {_m(s['net'])} · PF {_pf(s)} · "
                   f"avg {_m(s['per_trade'])}")
    c = concentration(rows)
    if c:
        share = f"{c['best_share']:.0f}%" if c["best_share"] is not None else "–"
        top3 = f"{c['top3_share']:.0f}%" if c["top3_share"] is not None else "–"
        out.append(f"    best {c['best'][0]} {_m(c['best'][1])} ({share} of all positive symbol P/L; top 3 "
                   f"{top3}) · worst {c['worst'][0]} {_m(c['worst'][1])}")
    return out


def _split(label: str, groups: dict) -> str:
    return f"  {label}: " + " │ ".join(f"{k} {_line(s)}" for k, s in groups.items() if s["n"])


def _block(code: str, rows: list[dict], cfg, full: bool) -> list[str]:
    s = stats(rows)
    lines = [f"\n<b>{NAMES[code]}</b> ({_status(code, cfg)})"]
    if code in PAUSED and not _configured(code, cfg):
        lines.append(f"  <i>no new forward paper entries after {PAUSED[code]} (paused; benchmark / research only; "
                     "history below is unchanged)</i>")
    if not s["n"]:
        return lines + ["  FORWARD PAPER: no trades yet", f"  HISTORICAL: {HISTORICAL.get(code, 'none')}"]
    flag = " · <b>INSUFFICIENT FOR PERFORMANCE JUDGMENT</b>" if insufficient(s["n"], s["days"]) else ""
    lines += [f"  FORWARD PAPER: {s['n']} trades / {s['days']} trading days · Evidence: {s['evidence']}{flag}",
              f"    wins {s['wins']} · losses {s['losses']} · win {s['win']:.0f}% · gross {_m(s['gross'])} · "
              f"commissions {_m(-s['fees'])} · net {_m(s['net'])} ({_m(s['per_trade'])}/trade = expectancy)",
              f"    avg winner {_m(s['avg_win'])} · avg loser {_m(s['avg_loss'])} · PF {_pf(s)} · max drawdown "
              f"{_m(s['max_dd'])} · largest winner {_m(s['best'])} · largest loser {_m(s['worst'])} · median hold "
              + (f"{s['hold_med']:.0f} min" if s["hold_med"] is not None else "–"),
              f"    calls {s['calls'][0]} ({_m(s['calls'][1])}) · puts {s['puts'][0]} ({_m(s['puts'][1])})",
              f"  HISTORICAL: {HISTORICAL.get(code, 'none')}"]
    d = day_summary(rows)
    lines.append(f"  days: {d['positive']} positive · {d['negative']} negative · best {d['best'][0]:%m-%d} "
                 f"{_m(d['best'][1])} · worst {d['worst'][0]:%m-%d} {_m(d['worst'][1])}")
    if not full:
        return lines
    lines.append("  daily: " + " · ".join(f"{k:%m-%d} {_m(v)}" for k, v in by_period(rows).items()))
    lines.append("  weekly: " + " · ".join(f"wk {k:%m-%d} {_m(v)}" for k, v in by_period(rows, True).items()))
    lines.append("  by symbol:")
    lines += _sym_table(rows)
    if s["n"] >= 10:
        o = outliers(rows)
        lines.append(f"  outlier check: net {_m(o['net'])} · without largest winner {_m(o['no_best'])} · without "
                     f"top 3 winners {_m(o['no_top3'])} · without largest loser {_m(o['no_worst'])}")
    ex = by_key(rows, lambda r: r["exit"])
    lines.append("  exits: " + " · ".join(f"{k} {ex[k]['n']} ({_m(ex[k]['net'])}, avg {_m(ex[k]['per_trade'])})"
                                         for k in EXIT_ORDER if k in ex))
    lines.append(f"  <i>{EXIT_NOTE}</i>")
    reg = by_key(rows, lambda r: r.get("regime") or "unknown")
    lines.append(_split("SPY day (diagnostic)", {k: reg[k] for k in ("UP", "DOWN", "FLAT", "unknown") if k in reg}))
    return lines


def _b_extra(rows: list[dict]) -> list[str]:
    lines = ["\n<b>B diagnostics</b> (observation only: no filter, no B2)"]
    if not rows:
        return lines + ["  no trades"]
    m = [r for r in rows if r["symbol"] == "MSFT"]
    lines.append(f"  MSFT: {_line(stats(m))} · B without MSFT: {_line(without(rows, 'MSFT'))}")
    lines.append(_split("CALL vs PUT", {"CALL": stats([r for r in rows if r["side"] == "bull"]),
                                        "PUT": stats([r for r in rows if r["side"] == "bear"])}))
    half = by_key(rows, lambda r: "first half (before 12:45)" if (r["entry_time"] or "99") < "12:45"
                  else "second half")
    lines.append(_split("session", half))
    lines.append(_split("EMA regime at entry (trend / chop, emacross.regime)",
                        by_key(rows, lambda r: r.get("ema_regime") or "unknown")))
    lines.append(_split("week", {f"wk {k:%m-%d}": v for k, v in
                                 sorted(by_key(rows, lambda r: r["date"] - timedelta(days=r["date"].weekday())).items())}))
    return lines


def _simple_extra(rows: list[dict]) -> list[str]:
    lines = ["\n<b>Simple diagnostics</b> (unchanged; too few trades to optimise anything)"]
    if not rows:
        return lines + ["  no trades"]
    lines.append(_split("symbol", {k: v for k, v in sorted(by_key(rows, lambda r: r["symbol"]).items())}))
    lines.append(_split("trade of day", {f"#{k}": v for k, v in sorted(by_key(rows, lambda r: r.get("n") or 0).items())}))
    lines.append(_split("side", {"CALL": stats([r for r in rows if r["side"] == "bull"]),
                                 "PUT": stats([r for r in rows if r["side"] == "bear"])}))
    lines.append(_split("time", by_key(rows, lambda r: "morning (before 12:00)" if (r["entry_time"] or "99") < "12:00"
                                       else "afternoon")))
    return lines


def _close_extra(rows: list[dict]) -> list[str]:
    lines = ["\n<b>Close-fade market context</b> (report only; never used for eligibility or execution)"]
    for r in rows:
        c = r.get("context") or {}
        spy = c.get("spy_day_pct", r.get("spy_at_entry"))
        src = "logged" if c else "from recorded checks"
        big = ", ".join(f"{k}%+" for k in (1, 2, 3) if spy is not None and abs(spy) >= k) or "under 1%"
        lines.append(f"  {r['date']:%m-%d} {r['side']} net {_m(r['net'])} · SPY day {spy:+.2f}% ({src}) · "
                     f"{r.get('regime') or c.get('direction') or '–'} · move {big}"
                     + (f" · SPX day {c['spx_day_pct']:+.2f}%" if c.get("spx_day_pct") is not None else "")
                     + (f" · range {c['day_range_pct']:.2f}%" if c.get("day_range_pct") is not None else "")
                     if spy is not None else f"  {r['date']:%m-%d} {r['side']} net {_m(r['net'])} · no SPY data")
    return lines


def _setup_lines(title: str, rows: list[dict], tz) -> list[str]:
    st = setup_stats(rows)
    if not st["obs"]:
        return [f"  {title}: no trades"]
    conc, at = st["concurrent"]
    big_exp, big = st["largest"]
    when = datetime.fromtimestamp(at, tz).strftime("%m-%d %H:%M") if at else "–"
    pct = lambda x: "–" if x is None else f"{x:.0f}%"
    return [f"  <b>{title}</b>: {st['obs']} strategy observations · {st['setups']} unique setups · "
            f"{st['multi']} taken by 2+ strategies · observations per setup "
            + ", ".join(f"{k}×{v}" for k, v in st["sizes"].items()),
            f"    premium paid, all observations {_usd(st['premium'])} · per setup avg {_usd(st['setup_avg_exposure'])} · "
            f"largest setup {_usd(big_exp)} ({setup_id(big, tz)}: {'+'.join(r['code'] for r in big)}) · "
            f"max held at once {_usd(conc)} ({when})",
            f"    setup P/L as traded (sum of expressions): best {_m(st['best'][0])} ({setup_id(st['best'][1], tz)}) · "
            f"worst {_m(st['worst'][0])} ({setup_id(st['worst'][1], tz)}) · top 1 setup {pct(st['top1_share'])} and "
            f"top 3 {pct(st['top3_share'])} of positive setup P/L · net {_m(st['net'])}, without top 1 "
            f"{_m(st['no_top1'])}, without top 3 {_m(st['no_top3'])}"]


def _setups(rows: list[dict], cfg) -> list[str]:
    tz = cfg.tz
    active = [r for r in rows if _live(r["code"], cfg)]
    corr = [r for r in rows if r["code"] in CORRELATED]
    lines = ["\n<b>Setup-level view</b> (REPORT ONLY; same day, symbol and direction, each entry within 10 minutes "
             "of the previous = one setup; exposure = premium paid, the most a long option can lose; no risk cap is "
             "applied)"]
    lines += _setup_lines("All strategies incl. retired A and paused B / Simple", rows, tz)
    lines += _setup_lines("Current active set", active, tz)
    lines += _setup_lines("D-family (C / D / E7 / E21)", corr, tz)
    c = count_once(corr)
    if c:
        lines.append(f"    D-family P/L: every observation {_m(c['all'])} · each setup once, equal-weight mean of its "
                     f"expressions {_m(c['mean'])} · each setup once, first expression to enter {_m(c['first'])} · "
                     f"avg setup return on premium " + ("–" if c["ret"] is None else f"{c['ret']:+.1f}%"))
        lines.append("    <i>No count-once figure is canonical: the mean treats each idea as one unit, the first-entry "
                     "figure is one arbitrary expression, the return ignores size.</i>")
    fam = {}
    for g in setups(active):
        fam.setdefault("+".join(sorted({family(r["code"]) for r in g})), []).append(g)
    lines.append("    active setups by family mix: " + " · ".join(
        f"{k} {len(v)} ({_m(sum(r['net'] for g in v for r in g))})" for k, v in sorted(fam.items(), key=lambda kv: -len(kv[1]))))
    multi = [g for g in setups(corr) if len({r["code"] for r in g}) > 1]
    for g in multi:
        lines.append(f"    {setup_id(g, tz)}: " + ", ".join(f"{r['code']} {_m(r['net'])}" for r in g)
                     + f" · together {_m(sum(r['net'] for r in g))} on {_usd(sum(r['premium'] for r in g))} premium")
    return lines


def report(store, cfg, full: bool = False) -> str:
    rows = with_market(store, trades(store, cfg), cfg.tz)
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["code"], []).append(r)
    first = min((r["date"] for r in rows), default=None)
    last = max((r["date"] for r in rows), default=None)
    lines = ["📒 <b>Paper Strategy Scorecard</b>" + (f" — forward paper {first:%b %d}–{last:%b %d}" if rows else ""),
             f"<i>Report only. Net = fills at ask/bid minus ${cfg.daytrade.fee_per_contract:.2f}/contract each way. "
             "Evidence by distinct trading days (&lt;5 VERY LIMITED · 5–9 LIMITED · 10–19 EARLY · 20–39 DEVELOPING · "
             "40+ MORE MATURE; under 30 trades one step lower). Forward and historical are never merged.</i>"]
    if not rows:
        return "\n".join(lines + ["No paper trades logged yet."])
    lines += _table(groups, cfg)
    lines += _portfolio("All strategies incl. retired A and paused B / Simple history", rows)
    lines += _portfolio(f"Current active set ({' / '.join(c for c in ORDER if _live(c, cfg))}; history of those "
                        "strategies only)", [r for r in rows if _live(r["code"], cfg)])
    for code in ORDER:
        lines += _block(code, groups.get(code, []), cfg, full)
    if full:
        lines += _b_extra(groups.get("B", []))
        lines += _simple_extra(groups.get("Simple", []))
        lines += _close_extra(groups.get("Close fade", []))
        lines += _setups(rows, cfg)
    else:
        lines.append("\n<i>/paperscore full: daily / weekly, symbols, outliers, exits, SPY-day splits, B and Simple "
                     "diagnostics, close-fade context, setup-level view.</i>")
    return "\n".join(lines)
