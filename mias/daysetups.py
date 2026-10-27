"""Intraday setups for the day-trade backtest (and, once one proves itself, live paper alerts), each with the same
shape as emacross.scan so they share its trade management and option-price backtest.

  ema           EMA 9/21 cross + retest (emacross.scan)
  orb           opening-range breakout: first candle after 09:45 closing beyond the 15-minute range, on volume
                above the session's average candle; invalidation the range midpoint. Once per side per day.
  vwap_reclaim  after 3+ closes on one side of VWAP, two closes on the other side; invalidation the extreme of
                those two candles. Mirror for a lost VWAP. Up to 3 per side per day, 6 candles apart.
  vwap_reject   with 3+ closes on one side of VWAP, a candle tags VWAP and closes back on its side in the trend's
                color; invalidation that candle's extreme. Up to 3 per side per day, 6 candles apart.
  pdhl          first close beyond the prior day's high (bull) or low (bear); invalidation the candle's extreme.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from . import emacross
from .emacross import FIVE, DayTradeRules, Setup, _et, fill_entry
from .indicators import Bar


def _session(bars: list[Bar], day: date, tz: ZoneInfo) -> tuple[list[int], list[float]]:
    """Indexes of `day`'s candles and the running VWAP at each of them."""
    idx = [i for i, b in enumerate(bars) if _et(b.t, tz).date() == day]
    vw, pv, vol = [], 0.0, 0.0
    for i in idx:
        b = bars[i]
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
        vw.append(pv / vol if vol else b.c)
    return idx, vw


def _new(symbol, side, bars, i, first, extreme, tz, rules, span) -> Setup:
    su = Setup(symbol, side, bars[i].t)
    fill_entry(su, bars, i, first, extreme, tz, rules, span)
    return su


def orb(symbol: str, bars: list[Bar], day: date, tz: ZoneInfo, rules: DayTradeRules, *, retest: bool = True,
        span: int = FIVE) -> list[Setup]:
    idx, _ = _session(bars, day, tz)
    if len(idx) < 4:
        return []
    first = idx[0]
    end_orb = bars[first].t + 15 * 60_000
    rng = [bars[i] for i in idx if bars[i].t < end_orb]
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    mid = (hi + lo) / 2
    out, done = [], set()
    vols = []
    for i in idx:
        b = bars[i]
        vols.append(b.v)
        if b.t < end_orb:
            continue
        avg = sum(vols[:-1]) / max(1, len(vols) - 1)
        for side, beyond in (("bull", b.c > hi), ("bear", b.c < lo)):
            if side in done or not beyond:
                continue
            done.add(side)
            if b.v >= avg:
                out.append(_new(symbol, side, bars, i, first, mid, tz, rules, span))
    return out


def _vwap_scan(symbol, bars, day, tz, rules, span, reject: bool) -> list[Setup]:
    idx, vw = _session(bars, day, tz)
    if len(idx) < 6:
        return []
    first = idx[0]
    out: list[Setup] = []
    last_at = {"bull": -99, "bear": -99}
    count = {"bull": 0, "bear": 0}
    side_of = [1 if bars[i].c > vw[n] else -1 if bars[i].c < vw[n] else 0 for n, i in enumerate(idx)]
    for n in range(4, len(idx)):
        i = idx[n]
        b = bars[i]
        if reject:
            # trend side = the side of the 3 closes before this candle
            prior = side_of[n - 3:n]
            if abs(sum(prior)) != 3:
                continue
            sg = prior[0]
            touched = (b.h >= vw[n]) if sg < 0 else (b.l <= vw[n])
            held = sg * (b.c - vw[n]) > 0 and sg * (b.c - b.o) > 0
            if touched and held:
                side = "bull" if sg > 0 else "bear"
                extreme = b.l if sg > 0 else b.h
            else:
                continue
        else:
            prior = side_of[n - 4:n - 1]
            if abs(sum(prior)) != 3 or side_of[n - 1] != -prior[0] or side_of[n] != -prior[0]:
                continue
            sg = -prior[0]
            side = "bull" if sg > 0 else "bear"
            two = (bars[idx[n - 1]], b)
            extreme = min(x.l for x in two) if sg > 0 else max(x.h for x in two)
        if n - last_at[side] < 6 or count[side] >= 3:
            continue
        last_at[side] = n
        count[side] += 1
        out.append(_new(symbol, side, bars, i, first, extreme, tz, rules, span))
    return out


def vwap_reclaim(symbol: str, bars: list[Bar], day: date, tz: ZoneInfo, rules: DayTradeRules, *,
                 retest: bool = True, span: int = FIVE) -> list[Setup]:
    return _vwap_scan(symbol, bars, day, tz, rules, span, reject=False)


def vwap_reject(symbol: str, bars: list[Bar], day: date, tz: ZoneInfo, rules: DayTradeRules, *,
                retest: bool = True, span: int = FIVE) -> list[Setup]:
    return _vwap_scan(symbol, bars, day, tz, rules, span, reject=True)


def pdhl(symbol: str, bars: list[Bar], day: date, tz: ZoneInfo, rules: DayTradeRules, *, retest: bool = True,
         span: int = FIVE) -> list[Setup]:
    idx, _ = _session(bars, day, tz)
    if not idx or idx[0] == 0:
        return []
    first = idx[0]
    prev_day = _et(bars[first - 1].t, tz).date()
    prev = [b for b in bars[max(0, first - 400):first] if _et(b.t, tz).date() == prev_day]
    if not prev:
        return []
    pdh, pdl = max(b.h for b in prev), min(b.l for b in prev)
    out, done = [], set()
    for i in idx:
        b = bars[i]
        for side, beyond, extreme in (("bull", b.c > pdh, b.l), ("bear", b.c < pdl, b.h)):
            if side in done or not beyond:
                continue
            if i == first:
                done.add(side)          # gapped and closed beyond at the open: not a break during the day
                continue
            done.add(side)
            out.append(_new(symbol, side, bars, i, first, extreme, tz, rules, span))
    return out


SETUPS = {"ema": emacross.scan, "orb": orb, "vwap_reclaim": vwap_reclaim, "vwap_reject": vwap_reject, "pdhl": pdhl}


async def run(cfg, setups: list[str], symbols: list[str], start: date, end: date, minutes: list[int],
              slip: float, out_dir) -> str:
    """Every setup x symbol x candle size, with slippage, into one comparison report."""
    from .emacross import _summary_rows, backtest, write_csv
    from .massive import MassiveClient
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    all_trades = []
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        for name in setups:
            for m in (minutes if name == "ema" else [5]):
                for sym in symbols:
                    trades, _ = await backtest(client, sym, start, end, cfg.tz, cfg.daytrade, out_dir / "cache",
                                               scan_fn=SETUPS[name], minutes=m, slip=slip,
                                               trend=(cfg.trend_profiles, cfg.trend_strong, cfg.trend_bias))
                    for t in trades:
                        t.update(setup=name, minutes=m, symbol=sym)
                    all_trades += trades
                    rows.append((name, m, sym, trades))
    write_csv(out_dir / "trades.csv", all_trades)

    def line(label, ts):
        s = _summary_rows(ts)
        if not s["trades"]:
            return f"| {label} | 0 | | | | | |"
        pf = s["profit_factor"] if s["profit_factor"] is not None else "–"
        return (f"| {label} | {s['trades']} | {s['win_rate']}% | ${s['total']:,} | {s['avg_pct']:+}% | {pf} | "
                f"${s['max_drawdown']:,} |")
    head = ["| Setup | Trades | Win | Total P&L | Avg trade | Profit factor | Max drawdown |",
            "|---|---|---|---|---|---|---|"]
    lines = [f"# Day-trade setups {start} to {end}", "",
             f"5 contracts per trade, the handout's management (stop -20%, sell 3 at +30%, runners to breakeven, "
             f"trail), real option minute bars, {100 * slip:.1f}% slippage on every fill.", "",
             "## All symbols together", "", *head]
    keys = sorted({(n, m) for n, m, _, _ in rows}, key=lambda x: (setups.index(x[0]), x[1]))
    for n, m in keys:
        ts = [t for nn, mm, _, tt in rows if (nn, mm) == (n, m) for t in tt]
        lines.append(line(f"{n} {m}m", ts))
        for reg in ("trend", "chop"):
            lines.append(line(f"  {reg} days", [t for t in ts if t.get("regime") == reg]))
        for w, label in (("with", "with the swing trend"), ("neutral", "swing neutral"),
                         ("against", "against the swing trend")):
            lines.append(line(f"  {label}", [t for t in ts if t.get("with_swing") == w]))
    lines += ["", "## By symbol", "", *head]
    for n, m, sym, ts in rows:
        lines.append(line(f"{n} {m}m {sym}", ts))
    text = "\n".join(lines)
    (out_dir / "report.md").write_text(text)
    return text
