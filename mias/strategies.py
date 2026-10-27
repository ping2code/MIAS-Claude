"""Strategy report: every paper strategy's own record, so a winner can't hide a loser.

Strategies: the EMA 9/21 day trades (mias/emacross.py, retest and no-retest) and the 3:45 PM close fade
(mias/closeplay.py). Per strategy, for the period and since the start: trades, win rate, average win and loss, expectancy per trade
after commissions (fills are already at the bid/ask), total, profit factor and the worst drawdown. Signals the
health gate held (mias/health.py) are counted for the period. Sent with the Friday closing summary; /strategies
any time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import closeplay, emacross, health

MIN_TRADES = 30        # below this a strategy's numbers are too few to judge


def stats(trades: list[dict], fee: float, contracts: int = 5) -> dict:
    """fee: commission per contract per side; a trade's contracts are its "qty" (else `contracts`)."""
    pnl = [t["pnl"] - 2 * fee * t.get("qty", contracts) for t in trades]
    n = len(pnl)
    if not n:
        return {"n": 0}
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": n, "win": 100 * len(wins) / n, "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0, "expectancy": sum(pnl) / n,
            "total": sum(pnl), "pf": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "max_dd": dd}


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _line(label: str, s: dict) -> str:
    if not s["n"]:
        return f"{label}: no trades"
    pf = f"{s['pf']:.2f}" if s["pf"] is not None else "–"
    return (f"{label}: {s['n']} trade{'s' if s['n'] != 1 else ''} · win {s['win']:.0f}% · avg win "
            f"{_money(s['avg_win'])} / loss {_money(s['avg_loss'])} · <b>expectancy {_money(s['expectancy'])}/trade</b> · total "
            f"{_money(s['total'])} · PF {pf} · max drawdown {_money(s['max_dd'])}")


def _block(title: str, rows: list[dict], since: date, days: int, fee: float, contracts: int) -> list[str]:
    week = [t for t in rows if date.fromisoformat(t["date"]) >= since]
    lines = ["", title, _line(f"{days} days", stats(week, fee, contracts))]
    everything = stats(rows, fee, contracts)
    if rows and len(rows) != len(week):
        lines.append(_line(f"Since {rows[0]['date']}", everything))
    if everything["n"] < MIN_TRADES:
        lines.append(f"<i>{everything['n']} of {MIN_TRADES} trades needed before judging it</i>")
    else:
        lines.append("✅ positive expectancy after costs so far" if everything["expectancy"] > 0
                     else "⚠️ negative expectancy after costs: losing money per trade")
    sides = {s: stats([t for t in week if t["side"] == s], fee, contracts) for s in ("bull", "bear")}
    if any(v["n"] for v in sides.values()):
        lines.append(" · ".join(f"{'calls' if s == 'bull' else 'puts'} {v['n']} ({_money(v['total'])})"
                                for s, v in sides.items() if v["n"]))
    return lines


def report(store, cfg, now: datetime, days: int = 7) -> str:
    rules = cfg.daytrade
    today = now.astimezone(cfg.tz).date()
    since = today - timedelta(days=days - 1)
    fee = rules.fee_per_contract
    log = store.get(emacross.LOG_KEY) or []
    lines = [f"📊 <b>Strategy report</b> (paper) {since:%b %d}–{today:%b %d}",
             f"<i>Fills at bid/ask; ${fee:.2f}/contract commission each way taken off.</i>"]
    names = list(dict.fromkeys(list(rules.strategies) + [t.get("strategy", "retest") for t in log]))
    for name in names:
        meta = emacross.STRATEGIES.get(name, {})
        title = f"<b>EMA {rules.fast}/{rules.slow} {meta.get('title', name).lower()}</b>" \
            + ("" if name in rules.strategies else " (no longer running)")
        lines += _block(title, [t for t in log if t.get("strategy", "retest") == name], since, days, fee,
                        rules.contracts)
    close = store.get(closeplay.LOG_KEY) or []
    if cfg.closeplay_enabled or close:
        lines += _block("<b>3:45 PM close fade</b>" + ("" if cfg.closeplay_enabled else " (no longer running)"),
                        close, since, days, fee, cfg.closeplay_contracts)
    held: dict[str, dict[str, int]] = {}
    for i in range(days):
        for sym, per in health.day_counts(store, (since + timedelta(days=i)).isoformat()).items():
            for why, n in per.items():
                held.setdefault(sym, {})[why] = held.setdefault(sym, {}).get(why, 0) + n
    lines += ["", health.summary_line(held) or "🩺 Health gate: no signals held"]
    return "\n".join(lines)
