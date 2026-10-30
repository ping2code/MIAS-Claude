"""Strategies E7 and E21 (experimental, paper only): Strategy D's entry with a fixed-expiry ~0.50-delta option and
one all-out exit.

Both take the SAME Strategy D signal (qualify.enter with qualify.D_GATES: EMA 9/21 cross, valid retest, trend
regime, D's time windows; nothing added, no delay) and are independent paper strategies with their own state,
trades, loss counters, alerts and candidate logs. One D signal can give an E7 and an E21 trade: that is one signal
and two correlated observations, and the daily report counts them that way.

  E7   the listed expiry closest to signal day + 7 calendar days (within +-3), target +25%, stop -20%, sell all
  E21  the listed expiry closest to signal day + 21 calendar days (within +-3), target +30%, stop -20%, sell all

Contract: of the 8 listed strikes nearest the stock price for that expiry and side (two-sided quotes only), the one
whose |delta| is closest to 0.50 inside 0.45-0.55; ties go to the tighter spread, then the lower strike. Delta is
the snapshot's (vendor) delta, or Black-Scholes from the quote mid when the snapshot has none. Nothing in range:
NO_DELTA_MATCH, never a substitute. The health checks are D's (two-sided, fresh quote, spread <= 10%); the premium
is not a gate.

Exits (emacross._manage with emacross.rules_for: first_target 0.25 / 0.30, partial = every contract, no trail), in
this order each re-price: force close at 15:10 (sold at the bid), a 5-minute close through the invalidation level
(sold at the bid), the stop when the MID is at or below entry -20% (sold at the bid), the target when the MID is at
or above entry +25% / +30% (sold at the lower of the target and the bid). Entry is the ask. No partials, runners,
breakeven or trailing.

10-minute close confirmation (DIAGNOSTIC ONLY, never read by entry or exit code): after the two 5-minute candles
that follow the entry candle (signal + 10 minutes), PASS when the second candle closed at least 0.10% the trade's
way from the signal price, else FAIL; NOT_AVAILABLE when that candle never arrives. Kept per signal in DIAG_KEY.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta

from .emacross import FIVE, LOG_KEY, STRATEGIES, DayTradeRules, Setup, Trade, _at, _et, _mid, _money, _quote

log = logging.getLogger(__name__)

E7, E21 = "ema_9_21_e7", "ema_9_21_e21"
PLANS = {
    E7: {"dte": 7, "name": "E7", "label": "EMA 9/21 E7 — 7DTE 25/20 All-Out"},
    E21: {"dte": 21, "name": "E21", "label": "EMA 9/21 E21 — 21DTE 30/20 All-Out"},
}
WINDOW_DAYS = 3                 # an expiry within +-3 calendar days of the target DTE
NEAREST = 8                     # strikes considered, nearest the stock price
DELTA_LO, DELTA_HI = 0.45, 0.55
STRIKE_PCT = 0.05               # chain request: strikes within 5% of the stock price
RATE = 0.04                     # Black-Scholes fallback
CONFIRM_PCT = 0.10              # 10-minute close confirmation threshold (diagnostic)
DIAG_KEY = "ema_e_diag"         # per D signal: the 10-minute close confirmation
DIAG_MAX = 5000
GATE_REASONS = {"EMA_DIRECTION_MISALIGNED", "REGIME_CHOP", "OUTSIDE_TRADING_WINDOW", "WHIPSAW_COOLDOWN",
                "STALE_SIGNAL", "NO_VALID_RETEST"}


# ---- contract choice (pure) ----------------------------------------------------------------------

def pick_expiry(expiries, day: date, n: int) -> str | None:
    """The listed expiry closest to day + n calendar days (ties: the later one), only within +-3 days and after the
    signal day (the DTE research's rule)."""
    ds = sorted({date.fromisoformat(e) for e in expiries})
    want = day + timedelta(days=n)
    best = min(ds, key=lambda d: (abs((d - want).days), -d.toordinal()), default=None)
    return best.isoformat() if best and abs((best - want).days) <= WINDOW_DAYS and best > day else None


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(s: float, k: float, t: float, vol: float, call: bool) -> tuple[float, float]:
    d1 = (math.log(s / k) + (RATE + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return s * _ncdf(d1) - k * math.exp(-RATE * t) * _ncdf(d2), _ncdf(d1)
    return k * math.exp(-RATE * t) * _ncdf(-d2) - s * _ncdf(-d1), _ncdf(d1) - 1


def bs_delta(spot: float, strike: float, years: float, price: float, call: bool) -> float | None:
    """Delta implied by an option price (bisection on volatility); None when the price is outside the model."""
    lo, hi = 0.005, 6.0
    years = max(years, 1e-5)
    if price <= _bs(spot, strike, years, lo, call)[0] or price >= _bs(spot, strike, years, hi, call)[0]:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if _bs(spot, strike, years, mid, call)[0] < price:
            lo = mid
        else:
            hi = mid
    return _bs(spot, strike, years, (lo + hi) / 2, call)[1]


def _years(expiry: str, now_ms: int, tz) -> float:
    end = datetime.combine(date.fromisoformat(expiry), time(16), tz).timestamp() * 1000
    return (end - now_ms) / (365 * 86_400_000)


def choose(rows: list[dict], side: str, spot: float, expiry: str, now_ms: int, tz) -> tuple[dict | None, dict]:
    """The ~0.50-delta contract among the NEAREST strikes of one expiry and side. Returns (contract or None, fields);
    fields carry the delta, its source and, with no match, the nearest deltas seen."""
    call = side == "bull"
    kind = "call" if call else "put"
    rows = [c for c in rows if (c.get("details") or {}).get("contract_type") == kind
            and c["details"].get("expiration_date") == expiry]
    rows = sorted(rows, key=lambda c: (abs(float(c["details"]["strike_price"]) - spot),
                                       float(c["details"]["strike_price"])))[:NEAREST]
    cands = []
    for c in rows:
        bid, ask, mid = _quote(c)
        if not bid or not ask or not mid:
            continue
        d, src = (c.get("greeks") or {}).get("delta"), "vendor"
        if not d:
            d, src = bs_delta(spot, float(c["details"]["strike_price"]), _years(expiry, now_ms, tz), mid, call), "bs"
        if d is None:
            continue
        cands.append((c, round(d, 3), src, 100 * (ask - bid) / mid))
    ok = [x for x in cands if DELTA_LO <= abs(x[1]) <= DELTA_HI]
    if not ok:
        near = sorted((abs(x[1]) for x in cands), key=lambda d: abs(d - 0.5))[:3]
        return None, {"near_deltas": near, "strikes_checked": len(rows)}
    c, d, src, _ = min(ok, key=lambda x: (round(abs(abs(x[1]) - 0.5), 3), x[3],
                                          float(x[0]["details"]["strike_price"])))
    return c, {"delta": d, "delta_source": src}


async def option(client, sym: str, su: Setup, today: date, now: datetime, tz, strategy: str):
    """E7's / E21's contract: (contract, expiry, fields, reason or None), as qualify._option returns."""
    from . import health
    n = PLANS[strategy]["dte"]
    now_ms = int(now.timestamp() * 1000)
    kind = "call" if su.side == "bull" else "put"
    try:
        rows, _ = await client.option_chain(sym, spot=su.price, today=today, min_dte=max(1, n - WINDOW_DAYS),
                                            max_dte=n + WINDOW_DAYS, strike_pct=STRIKE_PCT, max_pages=5,
                                            contract_type=kind)
    except Exception as exc:
        log.warning("strategy %s %s chain failed: %s", strategy, sym, exc)
        return None, None, {}, "INVALID_DATA"
    rows = [c for c in rows if (c.get("details") or {}).get("contract_type") == kind]
    expiry = pick_expiry([c["details"]["expiration_date"] for c in rows], today, n)
    f = {"requested_dte": n, "expiry": expiry}
    if not expiry:
        return None, None, f, "NO_CONTRACT"
    spot = next(((c.get("underlying_asset") or {}).get("price") for c in rows
                 if (c.get("underlying_asset") or {}).get("price")), None) or su.price
    c, picked = choose(rows, su.side, spot, expiry, now_ms, tz)
    f.update(picked, spot=spot, actual_dte=(date.fromisoformat(expiry) - today).days)
    if c is None:
        return None, expiry, f, "NO_DELTA_MATCH"
    bid, ask, mid = _quote(c)
    f.update(option=c["details"]["ticker"], opt_bid=bid, opt_ask=ask, opt_mid=mid,
             opt_spread=round(ask - bid, 2), opt_spread_pct=round(100 * (ask - bid) / mid, 2))
    return c, expiry, f, ("INVALID_DATA" if health.quote_issue(c, now) else None)


def levels(su: Setup, tr: Trade, rules: DayTradeRules, rec: dict, strategy: str, tz) -> dict:
    """What E's trade record keeps besides D's: the contract choice and the per-contract economics."""
    return {"signal_key": su.key, "underlying_entry": su.price, "requested_dte": PLANS[strategy]["dte"],
            "actual_dte": rec.get("actual_dte"), "expiry": tr.expiry, "delta": rec.get("delta"),
            "delta_source": rec.get("delta_source"), "target_price": round(tr.entry * (1 + rules.first_target), 2),
            "stop_price": tr.stop, "target_pct": round(100 * rules.first_target), "contracts": rules.contracts,
            "capital_per_contract": round(100 * tr.entry, 2), "risk_per_contract": round(100 * (tr.entry - tr.stop), 2),
            "signal_at": _et(su.entry_t, tz).strftime("%H:%M"),
            "confirm_at": _et(su.entry_t + 2 * FIVE, tz).strftime("%H:%M")}


def entry_text(su: Setup, tr: Trade, rules: DayTradeRules, rec: dict, tz, losses: int, strategy: str,
               now_ms: int) -> str:
    p = PLANS[strategy]
    i = levels(su, tr, rules, rec, strategy, tz)
    kind = "C" if su.side == "bull" else "P"
    return "\n".join([
        f"{'🟢' if su.sign > 0 else '🔴'} <b>{p['name']} PAPER — {su.symbol} {'CALL' if su.sign > 0 else 'PUT'}</b> · "
        f"{p['label']}",
        f"Signal {i['signal_at']} (Strategy D entry) · filled {_et(now_ms, tz):%H:%M:%S} · {su.symbol} "
        f"{su.price:.2f}",
        f"Contract <b>{tr.strike:g}{kind} {date.fromisoformat(tr.expiry):%m/%d}</b> ({tr.contract[2:]}) · "
        f"{p['dte']}DTE requested, {i['actual_dte']}DTE actual · delta {abs(i['delta'] or 0):.2f} "
        f"({i['delta_source']})",
        f"Bid {tr.bid or 0:.2f} / ask {tr.ask or 0:.2f} / mid {tr.entry_mid or 0:.2f} · spread "
        f"{rec.get('opt_spread_pct') or 0:.1f}% · fill {tr.entry:.2f} (ask)",
        f"Target +{i['target_pct']}%: {i['target_price']:.2f} · Stop -{100 * rules.stop:.0f}%: {tr.stop:.2f} "
        f"(mid trigger, sold at the bid) · ALL-OUT EXIT",
        f"Chart invalidation: a 5m close {'below' if su.sign > 0 else 'above'} {su.invalidation:.2f} · force close "
        f"{rules.close} ET",
        f"Capital/contract ${i['capital_per_contract']:,.0f} · theoretical -{100 * rules.stop:.0f}% risk/contract "
        f"${i['risk_per_contract']:,.0f} · paper qty {rules.contracts}",
        f"10-min close confirmation: pending ({i['confirm_at']}) · diagnostic only",
        f"<i>PAPER ONLY · losses in a row today: {losses}/{rules.max_losses_in_row} · no orders are placed</i>"])


# ---- 10-minute close confirmation (diagnostic only) -----------------------------------------------

def close_confirm(bars, side: str, entry_t: int, price: float, now_ms: int, tz) -> dict:
    """PASS / FAIL once the second 5-minute candle after the entry candle has closed (signal + 10 minutes); PENDING
    before, NOT_AVAILABLE when that candle is missing 30 minutes later. Never used by any trade decision."""
    at = entry_t + 2 * FIVE
    out = {"close_confirm_10m_timestamp": _et(at, tz).strftime("%H:%M")}
    ck = next((b for b in bars if b.t == entry_t + FIVE), None)
    if ck is None or at > now_ms:
        out["close_confirm_10m"] = "NOT_AVAILABLE" if now_ms >= at + 6 * FIVE else "PENDING"
        return out
    raw = 100 * (ck.c / price - 1)
    fav = raw if side == "bull" else -raw
    out.update(close_confirm_10m="PASS" if round(fav, 6) >= CONFIRM_PCT else "FAIL",
               close_confirm_10m_move_pct=round(raw, 3), close_confirm_10m_fav_pct=round(fav, 3),
               close_confirm_10m_close=ck.c)
    return out


def register(store, su: Setup, tz) -> None:
    """Start tracking a D signal's confirmation (once per signal, whichever of E7 / E21 opens first)."""
    diag = store.get(DIAG_KEY) or {}
    if su.key in diag:
        return
    diag[su.key] = {"key": su.key, "symbol": su.symbol, "side": su.side, "date": _et(su.entry_t, tz).date().isoformat(),
                    "signal_at": _et(su.entry_t, tz).strftime("%H:%M"), "entry_t": su.entry_t, "price": su.price,
                    "close_confirm_10m": "PENDING"}
    if len(diag) > DIAG_MAX:
        for k in sorted(diag, key=lambda k: diag[k]["entry_t"])[:len(diag) - DIAG_MAX]:
            del diag[k]
    store.put(DIAG_KEY, diag)


def track(store, cfg, now: datetime, five: dict) -> None:
    """Resolve pending confirmations from completed 5-minute candles; note, per strategy, whether that strategy's
    trade had already closed (the diagnostic was then observed after the exit)."""
    diag = store.get(DIAG_KEY) or {}
    now_ms = int(now.timestamp() * 1000)
    changed = False
    for d in diag.values():
        if d["close_confirm_10m"] != "PENDING":
            continue
        bars = [b for b in five.get(d["symbol"]) or [] if b.t + FIVE <= now_ms]
        res = close_confirm(bars, d["side"], d["entry_t"], d["price"], now_ms, cfg.tz)
        if res["close_confirm_10m"] == "PENDING":
            continue
        d.update(res)
        d["observed_after_exit"] = {}
        for s in PLANS:
            st = store.get(STRATEGIES[s]["key"]) or {}
            done = next((t for t in st.get("closed") or [] if t.get("signal_key") == d["key"]), None)
            live = any((t.get("info") or {}).get("signal_key") == d["key"] for t in (st.get("open") or {}).values())
            d["observed_after_exit"][s] = (done["out"] < d["close_confirm_10m_timestamp"]) if done else \
                False if live else None
        changed = True
    if changed:
        store.put(DIAG_KEY, diag)


# ---- daily report --------------------------------------------------------------------------------

def _pct(x) -> str:
    return "–" if x is None else f"{x:+.0f}%"


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2


def report(store, cfg, day: date) -> str:
    """E7 / E21 for one day: unique D signals and what each strategy did with them, results with per-contract
    economics, every trade, the paired same-signal view and the 10-minute confirmation split (diagnostic)."""
    from .qualify import PROFILES, _count, _row_stats
    rules = cfg.daytrade
    fee, ds = rules.fee_per_contract, day.isoformat()
    rows = {s: [t for t in store.get(LOG_KEY) or [] if t.get("date") == ds and t.get("strategy") == s] for s in PLANS}
    cands = {s: {c["key"]: c for c in store.get(PROFILES[s]["log"]) or [] if c.get("date") == ds} for s in PLANS}
    diag = {k: v for k, v in (store.get(DIAG_KEY) or {}).items() if v.get("date") == ds}
    signals = sorted({k for s in PLANS for k, c in cands[s].items()
                      if c.get("status") == "entry" and not GATE_REASONS & set(c.get("reasons") or [])})
    lines = [f"🧪 <b>E7 / E21 paper report</b> {day:%a %b %d} · PAPER ONLY",
             "<i>Both trade the same Strategy D signals: one signal can give two correlated observations, not two "
             "independent trades.</i>"]

    def outcome(s, k):
        c = cands[s].get(k)
        if c is None:
            return "none"
        if c["disposition"] == "TRADE":
            return "valid"
        r = c.get("reasons") or ["?"]
        return "no delta match" if "NO_DELTA_MATCH" in r else "no contract" if "NO_CONTRACT" in r else r[0]
    out = {s: _count(outcome(s, k) for k in signals) for s in PLANS}
    both = sum(outcome(E7, k) == "valid" and outcome(E21, k) == "valid" for k in signals)
    only7 = sum(outcome(E7, k) == "valid" and outcome(E21, k) != "valid" for k in signals)
    only21 = sum(outcome(E21, k) == "valid" and outcome(E7, k) != "valid" for k in signals)
    lines.append(f"<b>Unique D signals</b>: {len(signals)} · both valid {both} · only E7 {only7} · only E21 {only21}")
    for s in PLANS:
        lines.append(f"{PLANS[s]['name']}: " + (" · ".join(f"{k} {v}" for k, v in sorted(out[s].items())) or "–"))
    for s, p in PLANS.items():
        rs = rows[s]
        lines += ["", f"<b>{p['label']}</b>: {_row_stats(rs, fee, rules.contracts)}"]
        if not rs:
            continue
        ex = _count(t["exit_reason"] for t in rs)
        stops = [100 * t["pnl"] / ((t.get("contracts") or rules.contracts) * 100 * t["entry"]) for t in rs
                 if t["exit_reason"] == "stop"]
        lines.append("Exits: " + " · ".join(f"{k} {ex.get(k, 0)}" for k in ("target", "stop", "invalidated", "time"))
                     + (f" · avg stop fill {sum(stops) / len(stops):+.1f}%" if stops else ""))
        lines.append(f"Per contract: median premium ${_median([t['entry'] for t in rs]):.2f} · capital "
                     f"${_median([t.get('capital_per_contract') for t in rs]) or 0:,.0f} · -20% risk "
                     f"${_median([t.get('risk_per_contract') for t in rs]) or 0:,.0f} · median delta "
                     f"{abs(_median([abs(t['delta']) for t in rs if t.get('delta')]) or 0):.2f} · avg spread "
                     f"{sum(t.get('entry_spread_pct') or 0 for t in rs) / len(rs):.1f}% · median hold "
                     f"{_median([t.get('minutes') for t in rs]) or 0:.0f} min")
        for t in rs:
            q = t.get("contracts") or rules.contracts
            d = diag.get(t.get("signal_key"), {})
            lines.append(
                f"{'🟢 CALL' if t['side'] == 'bull' else '🔴 PUT'} {t['symbol']} signal {t.get('signal_at')} · "
                f"{t.get('ticker')} {t.get('actual_dte')}DTE Δ{abs(t.get('delta') or 0):.2f} · in {t['entry']:.2f} · "
                f"target {t.get('target_price')} · stop {t.get('stop_price')} → {t['exit_reason']} {t['out']} · "
                f"{_money(t['pnl'] / q)}/contract ({t['pnl_pct']:+}%) · MFE {_pct(t.get('mfe_pct'))} / MAE "
                f"{_pct(t.get('mae_pct'))} · 10m {d.get('close_confirm_10m', '–')}")
    k7 = {t.get("signal_key"): t for t in rows[E7]}
    k21 = {t.get("signal_key"): t for t in rows[E21]}
    paired = [k for k in k7 if k in k21]
    if paired:
        lines += ["", "<b>Same signal, both expiries</b>"]
        for k in paired:
            a, b, d = k7[k], k21[k], diag.get(k, {})
            f = lambda t: (f"{t.get('actual_dte')}DTE Δ{abs(t.get('delta') or 0):.2f} ${t['entry']:.2f} "
                           f"sp {t.get('entry_spread_pct') or 0:.1f}% → {t['exit_reason']} {t.get('minutes', 0):.0f}m "
                           f"MFE {_pct(t.get('mfe_pct'))} MAE {_pct(t.get('mae_pct'))} {t['pnl_pct']:+}%")
            lines.append(f"{a['symbol']} {'CALL' if a['side'] == 'bull' else 'PUT'} {a.get('signal_at')} · 10m "
                         f"{d.get('close_confirm_10m', '–')} {d.get('close_confirm_10m_fav_pct', '')} · E7 {f(a)} · "
                         f"E21 {f(b)}")
    if diag:
        lines += ["", "<b>10-minute close confirmation</b> (diagnostic only, never used by a trade)"]
        for s, p in PLANS.items():
            g: dict[str, list] = {}
            for t in rows[s]:
                g.setdefault(diag.get(t.get("signal_key"), {}).get("close_confirm_10m", "–"), []).append(t)
            lines.append(f"{p['name']}: " + (" · ".join(
                f"{k} {len(v)} ({sum(t['pnl'] > 0 for t in v)}W, "
                f"{_money(sum(t['pnl'] - 2 * fee * (t.get('contracts') or rules.contracts) for t in v))})"
                for k, v in sorted(g.items())) or "no trades"))
    lines.append("<i>One day is not evidence: E7 vs E21 is judged on the accumulated paired record.</i>")
    return "\n".join(lines)
