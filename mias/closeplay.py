"""3:45 PM close play: at 15:45 ET pick a 0DTE CALL, a PUT or NO TRADE for the last 15 minutes; backtest first.

At 15:45 the read is three signs from completed 5-minute candles (the last one 15:40-15:45):
  day       price against the day's open
  vwap      price against the session VWAP
  momentum  price against 15:00 (the last 45 minutes)
Rules compared (each says CALL, PUT or nothing):
  trend     all three agree, trade with them
  strong    trend, and the day has moved at least STRONG_DAY %
  fade      all three agree, trade against them (the closing reversal)
  momentum  the 45-minute move alone
  vwap      the VWAP side alone
The outcome is the 15:45 -> 16:00 move. Option results use SPY 0DTE ATM options' real minute bars (SPX settles
the same move ~10x the size; the plan has no SPX index bars): bought at the first minute's open from 15:45,
sold at the 15:59 close, SLIP lost on each fill, commissions taken off.

Live (live() each cycle, manage() from the 30 s watcher): the fade rule only, paper-traded. Every weekday at the
first cycle from 15:45 the group gets CALL, PUT or NO TRADE with the reason and the SPX equivalent strike; a trade
is bought at the ask and sold at the bid at 15:59, then logged (LOG_KEY) for the strategy report.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path

from .emacross import _Cache, _et, _expiries, _five_minute, option_ticker
from .indicators import session_vwap
from .trend import regular_session, to_bars

log = logging.getLogger(__name__)

SIGNAL, EXIT = time(15, 45), time(16, 0)
STRONG_DAY = 0.5           # % day move for the 'strong' rule
SLIP = 0.01                # dollars lost per share on each fill (SPY 0DTE spreads are about a cent)
FEE = 0.65                 # per contract per side
RULES = ("trend", "strong", "fade", "momentum", "vwap")


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def read(day_bars: list, tz, outcome: bool = True) -> dict | None:
    """The 15:45 read from one day's regular-session 5-minute bars (None on half days or missing bars).
    outcome: also the 15:45 -> 16:00 move (backtests; needs the 15:55 candle)."""
    at = {_et(b.t, tz).time(): b for b in day_bars}
    pre = [b for b in day_bars if _et(b.t, tz).time() < SIGNAL]
    last, b1500, close = at.get(time(15, 40)), at.get(time(14, 55)), at.get(time(15, 55))
    if not pre or not last or not b1500 or (outcome and not close):
        return None
    vw = session_vwap(pre)
    price = last.c
    day = 100 * (price / pre[0].o - 1)
    x = {"price": price, "open": pre[0].o, "vwap": vw, "day_pct": day,
         "day": _sign(price - pre[0].o), "vwap_side": _sign(price - vw) if vw else 0,
         "momentum": _sign(price - b1500.c), "mom_pct": 100 * (price / b1500.c - 1)}
    if outcome:
        x.update(close=close.c, move_pct=100 * (close.c / price - 1))
    return x


def decide(x: dict, rule: str) -> int:
    """+1 CALL, -1 PUT, 0 no trade."""
    agree = x["day"] if x["day"] == x["vwap_side"] == x["momentum"] else 0
    if rule == "trend":
        return agree
    if rule == "strong":
        return agree if abs(x["day_pct"]) >= STRONG_DAY else 0
    if rule == "fade":
        return -agree
    if rule == "momentum":
        return x["momentum"]
    if rule == "vwap":
        return x["vwap_side"]
    raise ValueError(rule)


async def _option(client, cache: _Cache, symbol: str, day: date, side: int, strike: float, tz) -> dict | None:
    ticker = option_ticker(symbol, day.isoformat(), "bull" if side > 0 else "bear", strike)
    raw = await cache.get(f"{ticker}_{day}_close", lambda: client.aggregates(ticker, 1, "minute", day, day))
    mins = [b for b in to_bars(raw) if SIGNAL <= _et(b.t, tz).time() < EXIT]
    if not mins:
        return None
    entry = mins[0].o + SLIP
    exit_ = max(0.0, mins[-1].c - SLIP)
    return {"ticker": ticker, "entry": round(entry, 2), "exit": round(exit_, 2),
            "pnl": round((exit_ - entry) * 100 - 2 * FEE, 2), "pnl_pct": round(100 * (exit_ / entry - 1), 1),
            "peak_pct": round(100 * (max(b.h for b in mins) / entry - 1), 1)}


async def backtest(client, symbol: str, start: date, end: date, tz, cache_dir: Path) -> list[dict]:
    cache = _Cache(cache_dir)
    bars = await _five_minute(client, cache, symbol, start, end, tz)
    by_day: dict[date, list] = {}
    for b in bars:
        by_day.setdefault(_et(b.t, tz).date(), []).append(b)
    rows = []
    for day in sorted(by_day):
        x = read(by_day[day], tz)
        if not x:
            continue
        calls = {r: decide(x, r) for r in RULES}
        row = {"date": day.isoformat(), **{k: round(v, 3) if isinstance(v, float) else v for k, v in x.items()},
               **{f"rule_{r}": v for r, v in calls.items()}}
        if any(calls.values()):
            try:
                chains = await _expiries(client, cache, symbol, day, x["price"])
            except Exception as exc:
                log.warning("%s %s contracts failed: %s", symbol, day, exc)
                chains = {}
            strikes = chains.get(day.isoformat())
            if strikes:
                strike = min(strikes, key=lambda k: (abs(k - x["price"]), k))
                row["strike"] = strike
                for side, name in ((1, "call"), (-1, "put")):
                    if side in calls.values():
                        o = await _option(client, cache, symbol, day, side, strike, tz)
                        if o:
                            row.update({f"{name}_{k}": v for k, v in o.items()})
        rows.append(row)
    return rows


def summary(rows: list[dict], rule: str) -> dict:
    taken = [(r, r[f"rule_{rule}"]) for r in rows if r[f"rule_{rule}"]]
    n = len(taken)
    out = {"rule": rule, "days": len(rows), "signals": n}
    if not n:
        return out
    moves = [side * r["move_pct"] for r, side in taken]
    out.update(hit=100 * sum(m > 0 for m in moves) / n, avg_move=sum(moves) / n)
    opts = [r[f"{'call' if side > 0 else 'put'}_pnl"] for r, side in taken
            if f"{'call' if side > 0 else 'put'}_pnl" in r]
    if opts:
        wins = [p for p in opts if p > 0]
        losses = [p for p in opts if p <= 0]
        eq = peak = dd = 0.0
        for p in opts:
            eq += p
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        out.update(trades=len(opts), win=100 * len(wins) / len(opts), expectancy=sum(opts) / len(opts),
                   total=sum(opts), avg_win=sum(wins) / len(wins) if wins else 0.0,
                   avg_loss=sum(losses) / len(losses) if losses else 0.0,
                   pf=sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None, max_dd=dd)
    return out


def report(symbol: str, start: date, end: date, rows: list[dict]) -> str:
    def table(rs: list[dict], title: str) -> list[str]:
        out = [f"### {title} ({len(rs)} days)", "",
               "| Rule | Signals | Direction right | Avg 15-min move | Option trades | Win | Avg win / loss | "
               "Expectancy (1 contract) | Total | PF | Max drawdown |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for rule in RULES:
            s = summary(rs, rule)
            if not s["signals"]:
                out.append(f"| {rule} | 0 | | | | | | | | | |")
                continue
            pf = f"{s['pf']:.2f}" if s.get("pf") is not None else "–"
            o = (f"{s['trades']} | {s['win']:.0f}% | ${s['avg_win']:.0f} / ${s['avg_loss']:.0f} | "
                 f"**${s['expectancy']:+.1f}** | ${s['total']:+,.0f} | {pf} | ${s['max_dd']:,.0f}"
                 if s.get("trades") else "0 | | | | | |")
            out.append(f"| {rule} | {s['signals']} ({100 * s['signals'] / s['days']:.0f}% of days) | "
                       f"{s['hit']:.0f}% | {s['avg_move']:+.3f}% | {o} |")
        return out

    mid = start + (end - start) / 2
    first = [r for r in rows if date.fromisoformat(r["date"]) < mid]
    second = [r for r in rows if date.fromisoformat(r["date"]) >= mid]
    lines = [f"# 3:45 PM close play: {symbol} 0DTE, {start} to {end}", "",
             "Signal at 15:45 ET from completed 5-minute candles; exit at the 15:59 close. Option fills: first "
             f"minute's open from 15:45 + ${SLIP:.2f}, exit at the 15:59 close - ${SLIP:.2f}, ${FEE:.2f}/contract "
             "commission each way. 'Direction right' is the share of signals where SPY closed further in the "
             "signal's direction than at 15:45.", ""]
    lines += table(rows, "All days") + [""]
    lines += table(first, f"First half (to {mid})") + [""] + table(second, f"Second half (from {mid}), unseen "
                                                                           "when judging the first") + [""]
    return "\n".join(lines)


async def run_backtest(cfg, symbol: str, start: date, end: date, out_dir: Path) -> str:
    from .massive import MassiveClient
    out_dir.mkdir(parents=True, exist_ok=True)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        rows = await backtest(client, symbol, start, end, cfg.tz, out_dir / "cache")
    text = report(symbol, start, end, rows)
    (out_dir / f"{symbol}_report.md").write_text(text)
    if rows:
        with (out_dir / f"{symbol}_days.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
            w.writeheader()
            w.writerows(rows)
    return text


# ---- live paper trade ----------------------------------------------------------------------------

KEY = "close_play"
LOG_KEY = "close_log"
RULE = "fade"
LAST_SIGNAL, SELL = time(15, 52), time(15, 59)
BACKTEST_NOTE = "backtest 2y: right 59%, +$6.4/contract after costs, PF 1.34; thin edge"


def _state(store, day: date) -> dict:
    st = store.get(KEY) or {}
    return st if st.get("date") == day.isoformat() else {"date": day.isoformat(), "status": "waiting"}


def _word(sign: int, up: str, down: str) -> str:
    return up if sign > 0 else down if sign < 0 else "flat"


def signal_text(x: dict, side: int, local: datetime, symbol: str, spx: float | None) -> list[str]:
    agree = x["day"] if x["day"] == x["vwap_side"] == x["momentum"] else 0
    lines = [f"{symbol} {x['price']:.2f} · day {x['day_pct']:+.2f}% · {_word(x['vwap_side'], 'above', 'below')} "
             f"VWAP {x['vwap']:.2f} · last 45 min {x['mom_pct']:+.2f}%"]
    if agree:
        lines.append(f"All three {'up' if agree > 0 else 'down'}: fade it, the close tends to pull back")
    else:
        lines.append("Day, VWAP and the last 45 minutes don't all agree: no edge in the backtest")
    if side and spx:
        k = 5 * round(spx / 5)
        lines.append(f"SPX equivalent: 0DTE {k}{'C' if side > 0 else 'P'} (SPX ~{spx:,.0f}, ~10× the dollars, "
                     f"wider spreads)")
    return lines


async def live(client, store, cfg, now: datetime, today5: list[dict] | None, issues: list[str] | None = None,
               spx: float | None = None) -> int:
    """Once per cycle: the 15:45 call (and the paper entry), then the 15:59 exit if the watcher missed it."""
    if not cfg.closeplay_enabled:
        return 0
    tz = cfg.tz
    local = now.astimezone(tz)
    if local.weekday() >= 5:
        return 0
    st = _state(store, local.date())
    sent = 0
    if st["status"] == "waiting" and SIGNAL <= local.time() < LAST_SIGNAL:
        sent += await _signal(client, store, cfg, now, today5 or [], issues or [], spx, st)
    elif st["status"] == "waiting" and local.time() >= LAST_SIGNAL:
        st["status"] = "missed"
        log.warning("close play: no 15:45 read by %s", LAST_SIGNAL)
    store.put(KEY, st)
    return sent + await manage(client, store, cfg, now)


async def _signal(client, store, cfg, now: datetime, today5: list[dict], issues: list[str], spx: float | None,
                  st: dict) -> int:
    from . import health
    from .emacross import _mid, _quote
    tz, sym = cfg.tz, cfg.closeplay_symbol
    local = now.astimezone(tz)
    now_ms = int(now.timestamp() * 1000)
    bars = [b for b in regular_session(to_bars(today5), tz) if b.t + 300_000 <= now_ms]
    x = read(bars, tz, outcome=False)
    if x is None:
        return 0                                   # the 15:40 candle isn't in yet: next cycle
    side = decide(x, RULE)
    head = f"🕞 <b>3:45 close play (paper)</b> {local:%a %H:%M} ET"
    st.update(read={k: round(v, 4) if isinstance(v, float) else v for k, v in x.items()}, side=side)
    if not side:
        st["status"] = "no_trade"
        store.enqueue("closeplay", "\n".join([f"{head}: <b>NO TRADE</b>", *signal_text(x, 0, local, sym, spx),
                                              f"<i>{BACKTEST_NOTE}</i>"]))
        return 1
    if issues:
        st["status"] = "held"
        health.record(store, now, tz, sym, "close play", issues)
        store.enqueue("closeplay", f"{head}: ⚠️ held by the health gate ({escape(issues[0])})")
        return 1
    kind = "call" if side > 0 else "put"
    try:
        chain, _ = await client.option_chain(sym, spot=x["price"], today=local.date(), max_dte=0, strike_pct=0.01,
                                             max_pages=3)
    except Exception as exc:
        log.warning("close play chain failed: %s", exc)
        chain = []
    rows = [c for c in chain if (c.get("details") or {}).get("contract_type") == kind
            and c["details"].get("expiration_date") == local.date().isoformat() and _mid(c)]
    if not rows:
        st["status"] = "held"
        store.enqueue("closeplay", f"{head}: {kind.upper()} signal, but no 0DTE {sym} quote to paper-trade")
        return 1
    spot = (rows[0].get("underlying_asset") or {}).get("price") or x["price"]
    c = min(rows, key=lambda c: (abs(c["details"]["strike_price"] - spot), c["details"]["strike_price"]))
    bid, ask, mid = _quote(c)
    bad = health.spread_issue(bid, ask) or health.quote_issue(c, now)
    if bad:
        st["status"] = "held"
        health.record(store, now, tz, sym, "close play", [bad])
        store.enqueue("closeplay", f"{head}: {kind.upper()} signal held: {bad}")
        return 1
    strike = float(c["details"]["strike_price"])
    qty = cfg.closeplay_contracts
    st.update(status="open", ticker=c["details"]["ticker"], strike=strike, kind=kind, qty=qty, entry=ask,
              entry_mid=mid, entry_spread_pct=round(100 * (ask - bid) / mid, 1), opened=now.timestamp(),
              under=spot)
    k = "C" if side > 0 else "P"
    store.enqueue("closeplay", "\n".join([
        f"{head}: {'🟢' if side > 0 else '🔴'} <b>{kind.upper()}</b> · {sym} {strike:g}{k} 0DTE at {ask:.2f} ask "
        f"(mid {mid:.2f}) × {qty}",
        *signal_text(x, side, local, sym, spx),
        f"Exit: sell at 15:59 · max loss the premium (${ask * 100 * qty:,.0f})",
        f"<i>{BACKTEST_NOTE}</i>"]))
    return 1


async def manage(client, store, cfg, now: datetime) -> int:
    """Sell the open close-play trade at the bid from 15:59 (called by the cycle and the 30 s watcher)."""
    local = now.astimezone(cfg.tz)
    st = store.get(KEY) or {}
    if st.get("status") != "open" or st.get("date") != local.date().isoformat() or local.time() < SELL:
        return 0
    from .emacross import _quote
    try:
        snap = (await client.snapshots([st["ticker"]])).get(st["ticker"]) or {}
    except Exception as exc:
        log.warning("close play quote failed: %s", exc)
        return 0
    bid, ask, mid = _quote(snap)
    if mid is None and bid is None:
        return 0
    exit_ = bid if bid is not None else mid
    qty = st["qty"]
    pnl = round((exit_ - st["entry"]) * 100 * qty, 2)
    under = (snap.get("underlying_asset") or {}).get("price")
    st.update(status="closed", exit=exit_, exit_mid=mid, pnl=pnl, closed=now.timestamp())
    store.put(KEY, st)
    k = "C" if st["kind"] == "call" else "P"
    rec = {"date": st["date"], "strategy": "close_fade", "symbol": cfg.closeplay_symbol,
           "side": "bull" if st["kind"] == "call" else "bear", "contract": f"{st['strike']:g}{k}",
           "ticker": st["ticker"], "qty": qty, "entry": st["entry"], "entry_mid": st["entry_mid"], "exit": exit_,
           "pnl": pnl, "pnl_pct": round(100 * (exit_ / st["entry"] - 1), 1) if st["entry"] else 0.0,
           "pnl_mid": round(((mid or exit_) - (st["entry_mid"] or st["entry"])) * 100 * qty, 2),
           "under_in": st.get("under"), "under_out": under, "read": st.get("read")}
    store.put(LOG_KEY, (store.get(LOG_KEY) or [])[-999:] + [rec])
    sign = "+" if pnl >= 0 else "-"
    store.enqueue("closeplay", f"{'🎯' if pnl > 0 else '🛑'} <b>3:45 close play: SOLD</b> {cfg.closeplay_symbol} "
                               f"{st['strike']:g}{k} at {exit_:.2f} bid (in {st['entry']:.2f}) · "
                               f"{sign}${abs(pnl):,.0f} ({rec['pnl_pct']:+}%)"
                               + (f" · {cfg.closeplay_symbol} {st['under']:.2f} → {under:.2f}"
                                  if under and st.get("under") else ""))
    return 1
