"""EMA 9/21 Simple — SPY / QQQ / IWM (ema_9_21_simple_index): EXPERIMENTAL, paper only, a frozen benchmark.

The plainest version of the EMA 9/21 idea, to see whether the cross itself has an options edge on the index ETFs:
  - Signal: a completed regular-session 5-minute candle whose EMA 9 / EMA 21 state differs from the previous completed
    candle's (bullish: prev EMA9 <= EMA21 and now EMA9 > EMA21; bearish: prev EMA9 >= EMA21 and now EMA9 < EMA21).
    EMAs are mias.indicators.ema (standard EMA, k = 2/(n+1), seeded with the first close) on the closes of completed
    regular-session candles, earlier days included so they're warmed up (the same input as emacross.scan). Only
    completed candles are ever read, so a cross that reverses inside a candle never exists here.
  - Per symbol per day, the first two signals whose candle closes by last_entry (15:00, [daytrade]) are traded,
    each as its own trade (trade #2 may open while #1 is still open); the third and later are ignored. Signals after
    last_entry are not traded and don't count. SPY, QQQ and IWM count independently; the count resets each day.
  - Contract: ATM (the listed strike nearest the signal candle's close, the lower on a tie) CALL for bullish, PUT for
    bearish, on emacross.pick_expiry's expiry (0DTE, the next expiry from 13:00), as Strategy D.
  - Paper fill at the ask; target +25% and stop -20% of that entry, judged on the option mid, all contracts sold at
    the bid (a target never above its price); everything closed at 15:10. No partials, runners, breakeven, trail,
    chart invalidation, loss limit or any qualification gate.
Its own state, trade log and alerts: Strategies A/B/C/D/E7/E21 never see it and it never sees them. No order path.
"""
import asyncio
import logging
from dataclasses import asdict, replace
from datetime import date, datetime

from .emacross import (FIVE, DayTradeRules, Setup, Trade, _at, _close_record, _et, _money, _quote, pick_expiry)
from .indicators import Bar, ema

log = logging.getLogger(__name__)

NAME = "ema_9_21_simple_index"
LABEL = "EMA 9/21 Simple — SPY / QQQ / IWM"
SYMBOLS = ("SPY", "QQQ", "IWM")
MAX_TRADES = 2                      # per symbol per day
TARGET, STOP = 0.25, 0.20
KEY = "ema_simple_day"
LOG_KEY = "ema_simple_log"
LOG_MAX = 3000
LOCK = asyncio.Lock()               # the cycle (live) and the 30 s watcher (manage) both update the day's state;
                                    # without it both can close the same trade (2026-11-10 QQQ 673P, logged twice)
FRESH_MS = 2 * FIVE                 # a signal found later than this after its candle closed is not traded (missed)


def rules_of(cfg) -> DayTradeRules:
    """[daytrade] times and size, with this strategy's frozen exits: one all-out target, no trail."""
    base: DayTradeRules = cfg.daytrade
    return replace(base, first_target=TARGET, stop=STOP, partial=base.contracts, trail=())


def crosses(symbol: str, bars: list[Bar], day: date, tz, fast: int = 9, slow: int = 21) -> list[dict]:
    """Every EMA fast/slow state change on `day`'s completed candles (earlier days in `bars` warm the EMAs; the
    first candle of the day is compared with the previous day's last)."""
    closes = [b.c for b in bars]
    f, s = ema(closes, fast), ema(closes, slow)
    out = []
    for i in range(1, len(bars)):
        if _et(bars[i].t, tz).date() != day:
            continue
        side = "bull" if f[i - 1] <= s[i - 1] and f[i] > s[i] else \
            "bear" if f[i - 1] >= s[i - 1] and f[i] < s[i] else None
        if side:
            b = bars[i]
            out.append({"symbol": symbol, "side": side, "t": b.t, "close_t": b.t + FIVE, "price": b.c,
                        "ema9": round(f[i], 4), "ema21": round(s[i], 4), "key": f"{symbol}:{side}:{b.t}"})
    return out


def number(signals: list[dict], day: date, tz, rules: DayTradeRules) -> list[dict]:
    """The daily counter for one symbol: each signal gets n (1, 2, 3, ...) in order; n <= MAX_TRADES is traded.
    Signals closing after last_entry get n = None (not traded, not counted)."""
    last = _at(day, rules.last_entry, tz)
    n, out = 0, []
    for sg in sorted(signals, key=lambda x: x["t"]):
        if sg["close_t"] > last:
            out.append(dict(sg, n=None, action="after_last_entry"))
            continue
        n += 1
        out.append(dict(sg, n=n, action="trade" if n <= MAX_TRADES else "ignored_limit"))
    return out


def setup_of(sg: dict) -> Setup:
    """emacross.Trade needs a Setup; the invalidation level is never used by this strategy."""
    return Setup(sg["symbol"], sg["side"], sg["t"], status="entry", entry_t=sg["close_t"], price=sg["price"],
                 invalidation=sg["price"])


def atm(rows: list[dict], side: str, spot: float, day: date, now_ms: int, tz, rules: DayTradeRules) -> dict | None:
    """The ATM call / put on the 0DTE (or, from one_dte_after, next) expiry: strike nearest spot, lower on a tie."""
    kind = "call" if side == "bull" else "put"
    rows = [c for c in rows if (c.get("details") or {}).get("contract_type") == kind]
    expiry = pick_expiry(list({c["details"]["expiration_date"] for c in rows}), day, now_ms, tz, rules)
    rows = [c for c in rows if c["details"]["expiration_date"] == expiry]
    if not rows:
        return None
    return min(rows, key=lambda c: (abs(c["details"]["strike_price"] - spot), c["details"]["strike_price"]))


# ---- live (paper) --------------------------------------------------------------------------------

def _state(store, today: date) -> dict:
    st = store.get(KEY) or {}
    if st.get("date") != today.isoformat():
        st = {"date": today.isoformat(), "seen": [], "counts": {s: 0 for s in SYMBOLS}, "open": {}, "closed": [],
              "ignored": {s: 0 for s in SYMBOLS}, "skipped": [], "summary": False}
    return st


def entry_text(sg: dict, tr: Trade, rules: DayTradeRules, tz) -> str:
    kind = "CALL" if sg["side"] == "bull" else "PUT"
    exp = date.fromisoformat(tr.expiry)
    dte = (exp - _et(sg["t"], tz).date()).days
    spread = tr.info.get("entry_spread")
    return "\n".join([
        f"{'🟢' if sg['side'] == 'bull' else '🔴'} <b>EMA SIMPLE — PAPER</b>",
        f"Symbol: <b>{sg['symbol']}</b> · Direction: <b>{kind}</b>",
        f"Signal time: {_et(tr.opened_t, tz):%H:%M:%S} · crossover candle {_et(sg['t'], tz):%H:%M}–"
        f"{_et(sg['close_t'], tz):%H:%M}",
        f"Underlying price: {sg['price']:.2f} · EMA9 {sg['ema9']:.2f} · EMA21 {sg['ema21']:.2f}",
        f"Trade number for this symbol today: {sg['n']} of {MAX_TRADES}",
        f"Option: {tr.contract} · Strike {tr.strike:g} · Expiration {exp:%Y-%m-%d} · DTE {dte}",
        f"Bid {tr.bid:.2f} · Ask {tr.ask:.2f} · Mid {tr.entry_mid:.2f} · Spread {spread:.2f}"
        f" ({tr.info.get('entry_spread_pct', 0):.1f}%)" if tr.bid and tr.ask and tr.entry_mid and spread is not None
        else "Bid/ask: n/a",
        f"Paper entry: {tr.entry:.2f} (ask) × {tr.qty}",
        f"Target +{100 * rules.first_target:.0f}%: {round(tr.entry * (1 + rules.first_target), 2):.2f} · "
        f"Stop -{100 * rules.stop:.0f}%: {tr.stop:.2f} (on the mid, sold at the bid, all contracts)",
        f"Force close: {rules.close}",
        "<i>PAPER ONLY</i>"])


def event_text(tr: Trade, sg_n: int, kind: str, detail: str, value: float) -> str:
    icon = {"target": "🎯", "stop": "🛑", "time": "⏰"}.get(kind, "•")
    head = {"target": "TARGET +25%", "stop": "STOPPED -20%", "time": "FORCE CLOSE 15:10"}.get(kind, kind.upper())
    k = "C" if tr.side == "bull" else "P"
    tail = f" · total {_money(tr.pnl)} ({tr.pnl_pct:+}%) at bid/ask, {_money(tr.pnl_mid)} at mid" if tr.closed else ""
    return (f"{icon} <b>EMA SIMPLE — PAPER · {tr.symbol} {tr.strike:g}{k} trade {sg_n} of {MAX_TRADES}: {head}</b>\n"
            f"{detail} · option mid {value:.2f}{tail}")


def summary_text(st: dict, today: date) -> str | None:
    closed = st.get("closed") or []
    if not closed and not st.get("seen"):
        return None
    total = sum(t["pnl"] for t in closed)
    lines = [f"📒 <b>{LABEL}</b> (paper) {today:%a %b %d}: {len(closed)} trade{'s' if len(closed) != 1 else ''} · "
             f"{_money(total)} at bid/ask"]
    for sym in SYMBOLS:
        mine = [t for t in closed if t["symbol"] == sym]
        lines.append(f"<b>{sym}</b>: {st['counts'].get(sym, 0)} signal(s) counted, {len(mine)} traded, "
                     f"{st['ignored'].get(sym, 0)} ignored (limit {MAX_TRADES}/day) · "
                     f"{_money(sum(t['pnl'] for t in mine))}")
        for t in mine:
            lines.append(f"  #{t['n']} {'🟢' if t['side'] == 'bull' else '🔴'} {t['in']} {t['contract']} "
                         f"{t['entry']:.2f} → {t['exit_reason']} {t['out']} · {_money(t['pnl'])} ({t['pnl_pct']:+}%)")
    for s in st.get("skipped") or []:
        lines.append(f"  not traded: {s}")
    return "\n".join(lines)


def _manage(snaps: dict, store, cfg, now: datetime, st: dict) -> int:
    rules = rules_of(cfg)
    tz = cfg.tz
    now_ms = int(now.timestamp() * 1000)
    close_ms = _at(now.astimezone(tz).date(), rules.close, tz)
    sent = 0
    for pos, d in list(st["open"].items()):
        tr = Trade(**d)
        n = tr.info["n"]
        bid, ask, value = _quote((snaps or {}).get(tr.contract) or {})
        if value is None:
            continue
        tr.bid, tr.ask = bid, ask
        events = tr.exit(now_ms, value, "time") if now_ms >= close_ms else \
            tr.on_price(now_ms, value, value, value, value, rules)
        if tr.closed and bid and ask and value:          # logged only, as emacross._manage: fills are unchanged
            tr.exit_spread_pct = round(100 * (ask - bid) / value, 1)
        for kind, detail in events:
            store.enqueue("ema_simple", event_text(tr, n, kind, detail, value), tr.symbol)
            sent += 1
        if tr.closed:
            del st["open"][pos]
            rec = _close_record(tr, tz, now_ms)
            st["closed"].append(rec)
            store.put(LOG_KEY, (store.get(LOG_KEY) or [])[-(LOG_MAX - 1):] + [dict(rec, date=st["date"],
                                                                                   strategy=NAME)])
        else:
            st["open"][pos] = asdict(tr)
    return sent


async def _snapshots(client, st: dict) -> dict:
    tickers = [d["contract"] for d in st["open"].values()]
    if not tickers:
        return {}
    try:
        return await client.snapshots(tickers)
    except Exception as exc:
        log.warning("ema simple quotes failed: %s", exc)
        return {}


async def manage(client, store, cfg, now: datetime) -> int:
    """The 30 s watcher: re-price open paper trades (target, stop, 15:10)."""
    async with LOCK:
        st = _state(store, now.astimezone(cfg.tz).date())
        if not st["open"]:
            return 0
        sent = _manage(await _snapshots(client, st), store, cfg, now, st)
        store.put(KEY, st)
        return sent


async def _enter(client, store, cfg, now: datetime, five: dict[str, list[Bar]], st: dict) -> int:
    from . import health
    rules = rules_of(cfg)
    tz = cfg.tz
    today = now.astimezone(tz).date()
    now_ms = int(now.timestamp() * 1000)
    sent = 0
    for sym in SYMBOLS:
        bars = [b for b in five.get(sym) or [] if b.t + FIVE <= now_ms]
        for sg in number(crosses(sym, bars, today, tz, rules.fast, rules.slow), today, tz, rules):
            if sg["key"] in st["seen"]:
                continue
            st["seen"].append(sg["key"])
            if sg["n"] is None:
                continue
            st["counts"][sym] = max(st["counts"].get(sym, 0), sg["n"])
            if sg["action"] == "ignored_limit":
                st["ignored"][sym] = st["ignored"].get(sym, 0) + 1
                continue
            when = f"{sym} #{sg['n']} {_et(sg['t'], tz):%H:%M}"
            if now_ms - sg["close_t"] > FRESH_MS:
                st["skipped"].append(f"{when}: missed (found {round((now_ms - sg['close_t']) / 60000)} min late)")
                continue
            try:
                rows, _ = await client.option_chain(sym, spot=sg["price"], today=today, max_dte=8, strike_pct=0.02,
                                                    max_pages=5)
            except Exception as exc:
                st["skipped"].append(f"{when}: option chain failed ({exc})")
                continue
            c = atm(rows, sg["side"], sg["price"], today, now_ms, tz, rules)
            if c is None:
                st["skipped"].append(f"{when}: no contract")
                continue
            bid, ask, mid = _quote(c)
            bad = ("no two-sided option quote" if not bid or not ask or ask < bid else None) or \
                health.quote_issue(c, now)
            if bad:
                st["skipped"].append(f"{when}: {c['details']['ticker']} {bad}")
                continue
            tr = Trade.open(setup_of(sg), c["details"]["ticker"], float(c["details"]["strike_price"]),
                            c["details"]["expiration_date"], now_ms, ask, rules)
            tr.entry_mid, tr.bid, tr.ask = mid, bid, ask
            tr.info = {"n": sg["n"], "signal_candle": _et(sg["t"], tz).strftime("%H:%M"),
                       "candle_close": _et(sg["close_t"], tz).strftime("%H:%M:%S"),
                       "fill_at": _et(now_ms, tz).strftime("%H:%M:%S"),
                       "fill_delay_s": round((now_ms - sg["close_t"]) / 1000), "underlying": sg["price"],
                       "ema9": sg["ema9"], "ema21": sg["ema21"], "expiry": tr.expiry,
                       "dte": (date.fromisoformat(tr.expiry) - today).days,
                       "entry_bid": bid, "entry_ask": ask, "entry_mid": mid, "entry_spread": round(ask - bid, 2),
                       "entry_spread_pct": round(100 * (ask - bid) / mid, 1) if mid else None}
            st["open"][f"{sym}#{sg['n']}"] = asdict(tr)
            store.enqueue("ema_simple", entry_text(sg, tr, rules, tz), sym)
            sent += 1
    return sent


async def live(client, store, cfg, now: datetime, five: dict[str, list[Bar]]) -> int:
    """Once per cycle: manage open trades, open new ones, and the closing summary."""
    today = now.astimezone(cfg.tz).date()
    async with LOCK:
        st = _state(store, today)
        sent = _manage(await _snapshots(client, st), store, cfg, now, st)
        sent += await _enter(client, store, cfg, now, five, st)
        if int(now.timestamp() * 1000) >= _at(today, cfg.daytrade.close, cfg.tz) and not st["open"] \
                and not st["summary"]:
            text = summary_text(st, today)
            if text:
                store.enqueue("ema_summary", text)
                sent += 1
            st["summary"] = True
        store.put(KEY, st)
        return sent
