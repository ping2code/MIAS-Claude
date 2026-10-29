"""Strategy C (experimental, paper only): EMA 9/21 cross + retest + trend/VWAP qualification.

Runs beside A (cross + retest) and B (cross, no retest) on the same 5-minute bars and option chains, with its own
state (kv ema_qual_day), trade records (ema_log rows tagged "qualified") and candidate log. The retest detection,
contract choice, bid/ask fills and trade management are A's (emacross.scan, Trade, _manage); the difference is that a
confirmed retest is only a candidate. Every cross gets a disposition, logged with its diagnostics:
  WATCH   crossed, retest still pending
  REJECT  with reasons (REGIME_CHOP, VWAP_MISALIGNED, ...); A/B are unaffected
  TRADE   passed every hard gate and was paper-traded
Hard gates (each switchable in [ema_qualified]): regime trend (emacross.regime), price on the trade's side of the
session VWAP, EMA 9 and EMA 21 both sloping the trade's way over slope_bars candles, price on the trade's side of the
15-minute opening-range midpoint (fails closed without a complete opening range), entry windows, option spread,
whipsaw lock (whipsaw_flips opposite crosses within whipsaw_window_minutes lock the symbol for cooldown_minutes).
Logged only: EMA separation (against price and ATR), SPY/QQQ alignment, volume against the prior 20 candles, and an
unweighted quality score (one point per component: reporting only, never a gate). Everything is read from completed
candles up to the entry candle, so nothing looks ahead.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time

from .emacross import (FIVE, LOG_KEY, MAX_SPREAD_PCT, STRATEGIES, DayTradeRules, Setup, Trade, _at, _chain, _et, _mid,
                       _money, _quote, _skip, _spread_pct, entry_info, entry_text, pick_expiry, regime, scan)
from .indicators import Bar, atr, ema, session_vwap

log = logging.getLogger(__name__)

NAME = "qualified"
CANDIDATES = "ema_qual_candidates"     # the day's candidates by setup key (WATCH ones included)
CAND_LOG = "ema_qual_log"              # every final candidate, all days
CAND_MAX = 5000
MARKET = ("SPY", "QQQ")
REASONS = ("NO_VALID_RETEST", "STALE_SIGNAL", "INVALID_DATA", "EMA_DIRECTION_MISALIGNED", "REGIME_CHOP",
           "VWAP_MISALIGNED", "EMA_SLOPE_MISALIGNED", "EMA_SEPARATION_WEAK", "OPENING_RANGE_UNAVAILABLE",
           "OPENING_RANGE_MISALIGNED", "OUTSIDE_TRADING_WINDOW", "WHIPSAW_COOLDOWN", "MARKET_CONFLICT", "IN_TRADE",
           "DAILY_LOSS_LIMIT", "DATA_HEALTH", "NO_CONTRACT", "OPTION_SPREAD_TOO_WIDE")


@dataclass(frozen=True)
class QualRules:
    trend_only: bool = True               # regime must be "trend"
    require_vwap: bool = True             # calls above VWAP, puts below
    require_slope: bool = True            # EMA 9 and EMA 21 both sloping the trade's way (sign only, no threshold)
    require_or: bool = True               # price on the trade's side of the opening-range midpoint
    require_market: bool = False          # SPY and QQQ both with the trade (logged either way)
    min_separation_pct: float = 0.0       # |EMA9 - EMA21| / price, in %; 0 = logged, not a gate
    sessions: tuple[tuple[str, str], ...] = (("09:35", "11:00"), ("13:30", "14:30"))   # entry-candle close, ET
    no_entries_after: str = "14:45"       # absolute cut-off for the fill itself
    max_spread_pct: float = 8.0           # option bid/ask spread as a % of the mid
    whipsaw_window_minutes: int = 30
    whipsaw_flips: int = 2                # opposite-direction flips within the window that lock the symbol
    cooldown_minutes: int = 60
    slope_bars: int = 3                   # EMA slope = change over this many candles
    vwap_hold_bars: int = 3               # prior candles checked for closing on the same side of VWAP


def rules_of(cfg) -> QualRules:
    return getattr(cfg, "ema_qualified", None) or QualRules()


# Strategy D (EXPERIMENTAL): the backtested "A + trend + time windows" rule, fixed in code so it can't drift from what
# was validated (Oct 2024-Oct 2026, both years positive): the trend gate and the entry windows only; VWAP, slope,
# opening range and whipsaw are logged as diagnostics, never gates. Exits: DayTradeRules with first_target 0.50
# (emacross.STRATEGIES["ema_9_21_d"]["rules"]); option checks are A's (health spread <= 10%, fresh quote).
D = "ema_9_21_d"
D_GATES = QualRules(require_vwap=False, require_slope=False, require_or=False, whipsaw_flips=999)

# Per experimental strategy: candidate keys, gates, and which option-spread check applies.
PROFILES = {
    NAME: {"candidates": CANDIDATES, "log": CAND_LOG, "gates": rules_of, "spread": "max_spread_pct"},
    D: {"candidates": "ema_d_candidates", "log": "ema_d_log", "gates": lambda cfg: D_GATES, "spread": "health"},
}


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


# ---- diagnostics (pure) --------------------------------------------------------------------------

def crosses(bars: list[Bar], fast: list[float], slow: list[float], first: int) -> list[tuple[int, str]]:
    """(candle close ms, side) of every EMA cross from the session's first candle (scan()'s definition)."""
    out = []
    for i in range(max(first, 1), len(bars)):
        up, was = fast[i] > slow[i], fast[i - 1] > slow[i - 1]
        if up and not was:
            out.append((bars[i].t + FIVE, "bull"))
        elif was and fast[i] < slow[i]:
            out.append((bars[i].t + FIVE, "bear"))
    return out


def whipsaw(xs: list[tuple[int, str]], at_ms: int, q: QualRules) -> dict:
    """Flips in the window ending at at_ms, and the lock: whipsaw_flips flips (whipsaw_flips + 1 crosses, which
    always alternate) within whipsaw_window_minutes lock the symbol from the last of them for cooldown_minutes."""
    win = q.whipsaw_window_minutes * 60_000
    seen = [t for t, _ in xs if t <= at_ms]
    flips = max(0, sum(t >= at_ms - win for t in seen) - 1)
    until, n = None, q.whipsaw_flips
    for k in range(n, len(seen)):
        if seen[k] - seen[k - n] <= win and seen[k] <= at_ms < seen[k] + q.cooldown_minutes * 60_000:
            until = max(until or 0, seen[k] + q.cooldown_minutes * 60_000)
    return {"whipsaw_count": flips, "chop_lock": until is not None, "cooldown_until": until}


def opening_range(bars: list[Bar], first: int, i: int, tz) -> tuple[float, float, float] | None:
    """(high, low, midpoint) of the 15-minute opening range (09:30-09:45), once all three candles are complete at
    candle i. None when the session didn't start at 09:30 or the range isn't complete: Strategy C fails closed."""
    if _et(bars[first].t, tz).time() != time(9, 30) or i < first + 2:
        return None
    orb = [b for b in bars[first:i + 1] if b.t < bars[first].t + 15 * 60_000]
    if len(orb) != 3:
        return None
    hi, lo = max(b.h for b in orb), min(b.l for b in orb)
    return hi, lo, (hi + lo) / 2


def market(five: dict[str, list[Bar]], upto_ms: int, sign: int, tz, day: date) -> dict:
    """SPY/QQQ direction on the day and side of VWAP at upto_ms; aligned when both move and sit the trade's way."""
    out: dict = {}
    agree: list[bool | None] = []
    for s in MARKET:
        bs = [b for b in five.get(s) or [] if b.t + FIVE <= upto_ms and _et(b.t, tz).date() == day]
        k = s.lower()
        if not bs:
            out[f"{k}_direction"] = out[f"{k}_vwap"] = None
            agree.append(None)
            continue
        d = 1 if bs[-1].c > bs[0].o else -1 if bs[-1].c < bs[0].o else 0
        vw = session_vwap(bs)
        side = 0 if vw is None else 1 if bs[-1].c > vw else -1 if bs[-1].c < vw else 0
        out[f"{k}_direction"] = {1: "up", -1: "down", 0: "flat"}[d]
        out[f"{k}_vwap"] = {1: "above", -1: "below", 0: "at"}[side]
        agree.append(d == sign and side == sign)
    out["market_aligned"] = None if None in agree else all(agree)
    return out


def in_window(candle_ms: int, fill_ms: int, day: date, tz, q: QualRules) -> bool:
    ok = any(_at(day, a, tz) <= candle_ms <= _at(day, b, tz) for a, b in q.sessions)
    return ok and fill_ms <= _at(day, q.no_entries_after, tz)


def evaluate(su: Setup, bars: list[Bar], first: int, i: int, tz, rules: DayTradeRules, q: QualRules,
             five: dict[str, list[Bar]], fill_ms: int) -> dict:
    """Every Strategy C diagnostic at candle i (the entry candle, or the cross candle for crosses that never
    retested), from bars[:i + 1] only. Returns the fields, the components and the hard-gate reasons."""
    n = q.slope_bars
    if i < max(rules.slow, n + 1) or first > i:
        return {"reasons": ["INVALID_DATA"], "components": {}}
    closes = [b.c for b in bars[:i + 1]]
    fast, slow = ema(closes, rules.fast), ema(closes, rules.slow)
    b, sg, price = bars[i], su.sign, bars[i].c
    day = _et(b.t, tz).date()
    s9, s21 = (fast[i] - fast[i - n]) / n, (slow[i] - slow[i - n]) / n
    gap, gap_prev = fast[i] - slow[i], fast[i - 1] - slow[i - 1]
    a = atr(bars[max(0, i - 15):i + 1])
    vw = session_vwap(bars[first:i + 1])
    prior = []
    for j in range(max(first, i - q.vwap_hold_bars), i):
        v = session_vwap(bars[first:j + 1])
        prior.append(v is not None and sg * (bars[j].c - v) > 0)
    orr = opening_range(bars, first, i, tz)
    reg = regime(bars, i, first)
    vols = [x.v for x in bars[max(0, i - 20):i]]
    avg_v = sum(vols) / len(vols) if vols else 0
    candle_ms = b.t + FIVE
    d = {
        "eval_at": _et(candle_ms, tz).strftime("%H:%M"), "price": round(price, 4),
        "ema9": round(fast[i], 4), "ema21": round(slow[i], 4),
        "ema9_slope_pct": round(100 * s9 / price, 4), "ema21_slope_pct": round(100 * s21 / price, 4),
        "ema_sep_pct": round(100 * abs(gap) / price, 4),
        "ema_sep_atr": round(abs(gap) / a, 3) if a else None,
        "ema_sep_widening": abs(gap) > abs(gap_prev),
        "vwap": round(vw, 4) if vw is not None else None,
        "vwap_dist": round(price - vw, 4) if vw is not None else None,
        "vwap_dist_pct": round(100 * (price - vw) / vw, 3) if vw else None,
        "vwap_prior_same_side": bool(prior) and all(prior),
        "regime": reg,
        "or_high": round(orr[0], 4) if orr else None, "or_low": round(orr[1], 4) if orr else None,
        "or_mid": round(orr[2], 4) if orr else None,
        "or_position": None if not orr else "above_high" if price > orr[0] else "below_low" if price < orr[1]
        else "above_mid" if price > orr[2] else "below_mid" if price < orr[2] else "at_mid",
        "volume": b.v, "rel_volume": round(b.v / avg_v, 2) if avg_v else None,
        **market(five, candle_ms, sg, tz, day),
    }
    w = whipsaw(crosses(bars[:i + 1], fast, slow, first), candle_ms, q)
    d.update(w)
    if w["cooldown_until"]:
        d["cooldown_until"] = _et(w["cooldown_until"], tz).strftime("%H:%M")
    vwap_ok = vw is not None and sg * (price - vw) > 0
    slope_ok = sg * s9 > 0 and sg * s21 > 0
    or_ok = orr is not None and sg * (price - orr[2]) > 0
    window = in_window(candle_ms, fill_ms, day, tz, q)
    d["components"] = {
        "ema_direction": sg * gap > 0, "ema_slope": slope_ok, "ema_separation_widening": d["ema_sep_widening"],
        "vwap": vwap_ok, "opening_range": or_ok, "regime_trend": reg == "trend",
        "market": d["market_aligned"] is True, "volume": bool(d["rel_volume"] and d["rel_volume"] >= 1.0),
        "time_window": window, "retest": su.retest_t is not None and su.status in ("entry", "outside_hours"),
    }
    r = []
    if sg * gap <= 0:
        r.append("EMA_DIRECTION_MISALIGNED")
    if q.trend_only and reg != "trend":
        r.append("REGIME_CHOP")
    if q.require_vwap and not vwap_ok:
        r.append("VWAP_MISALIGNED")
    if q.require_slope and not slope_ok:
        r.append("EMA_SLOPE_MISALIGNED")
    if q.min_separation_pct and d["ema_sep_pct"] < q.min_separation_pct:
        r.append("EMA_SEPARATION_WEAK")
    if q.require_or and orr is None:
        r.append("OPENING_RANGE_UNAVAILABLE")
    elif q.require_or and not or_ok:
        r.append("OPENING_RANGE_MISALIGNED")
    if not window:
        r.append("OUTSIDE_TRADING_WINDOW")
    if w["chop_lock"]:
        r.append("WHIPSAW_COOLDOWN")
    if q.require_market and d["market_aligned"] is False:
        r.append("MARKET_CONFLICT")
    d["reasons"] = r
    return d


def score(components: dict) -> tuple[int, int, list[str]]:
    """Unweighted: one point per component passed (reporting only)."""
    failed = [k for k, v in components.items() if not v]
    return len(components) - len(failed), len(components), failed


# ---- live ----------------------------------------------------------------------------------------

def _book(store, today: date, key: str = CANDIDATES) -> dict:
    b = store.get(key) or {}
    return b if b.get("date") == today.isoformat() else {"date": today.isoformat(), "items": {}}


def _base(su: Setup, tz) -> dict:
    return {"key": su.key, "symbol": su.symbol, "side": su.side,
            "cross_close": _et(su.cross_t + FIVE, tz).strftime("%H:%M"),
            "entry_close": _et(su.entry_t, tz).strftime("%H:%M") if su.entry_t else None,
            "retest_at": _et(su.retest_t, tz).strftime("%H:%M") if su.retest_t else None, "status": su.status}


async def _option(client, sym: str, su: Setup, today: date, now: datetime, tz, rules: DayTradeRules):
    """A's contract choice (ATM, tight quotes first, 0DTE then 1DTE from one_dte_after). Returns (contract, expiry,
    fields, reason or None)."""
    from . import health
    now_ms = int(now.timestamp() * 1000)
    try:
        chain = await _chain(client, sym, su.price, today)
    except Exception as exc:
        log.warning("strategy C %s chain failed: %s", sym, exc)
        return None, None, {}, "INVALID_DATA"
    kind = "call" if su.side == "bull" else "put"
    rows = [c for c in chain if (c.get("details") or {}).get("contract_type") == kind and _mid(c)]
    expiry = pick_expiry(list({c["details"]["expiration_date"] for c in rows}), today, now_ms, tz, rules)
    rows = [c for c in rows if c["details"]["expiration_date"] == expiry]
    if not rows:
        return None, None, {}, "NO_CONTRACT"
    spot = (rows[0].get("underlying_asset") or {}).get("price") or su.price
    liquid = [c for c in rows if _spread_pct(c) <= MAX_SPREAD_PCT] or rows
    c = min(liquid, key=lambda c: abs(c["details"]["strike_price"] - spot))
    bid, ask, mid = _quote(c)
    f = {"option": c["details"]["ticker"], "opt_bid": bid, "opt_ask": ask, "opt_mid": mid,
         "opt_spread": round(ask - bid, 2) if bid and ask else None,
         "opt_spread_pct": round(100 * (ask - bid) / mid, 2) if bid and ask and mid else None, "spot": spot}
    bad = None if bid and ask else "no two-sided option quote"
    bad = bad or health.quote_issue(c, now)
    return c, expiry, f, ("INVALID_DATA" if bad else None)


def _d_line(d: dict, tr: Trade, rules: DayTradeRules) -> str:
    ok = lambda v: "✅" if v else "·"
    c = d.get("components") or {}
    return (f"D rule: trend ✅ · window ✅ ({d.get('eval_at')}) · target +{100 * rules.first_target:.0f}% "
            f"{tr.entry * (1 + rules.first_target):.2f} · stop -{100 * rules.stop:.0f}% {tr.stop:.2f}\n"
            f"<i>Diagnostics only: VWAP {d.get('vwap_dist_pct'):+.2f}% {ok(c.get('vwap'))} · EMA slopes "
            f"{ok(c.get('ema_slope'))} · OR {d.get('or_position')} {ok(c.get('opening_range'))} · SPY/QQQ "
            f"{ {True: '✅', False: '⚠️', None: '?'}[d.get('market_aligned')]} · spread {d.get('opt_spread_pct')}%</i>")


def _qual_line(d: dict) -> str:
    ok = lambda v: "✅" if v else "⚠️"
    c = d.get("components") or {}
    mk = {True: "✅", False: "⚠️", None: "?"}[d.get("market_aligned")]
    return (f"Qualified: regime {d.get('regime')} {ok(c.get('regime_trend'))} · VWAP {d.get('vwap_dist_pct'):+.2f}% "
            f"{ok(c.get('vwap'))} · EMA slopes {ok(c.get('ema_slope'))} · OR {d.get('or_position')} "
            f"{ok(c.get('opening_range'))} · SPY/QQQ {mk} · spread {d.get('opt_spread_pct')}% · "
            f"score {d.get('quality_score')}/{d.get('quality_max')}")


async def enter(client, store, cfg, now: datetime, five: dict[str, list[Bar]], results: dict,
                issues: dict[str, list[str]], st: dict, strategy: str = NAME) -> int:
    """Strategy C's / D's entry step (A/B use emacross._enter): every cross becomes a candidate with a disposition."""
    from . import health
    from .emacross import rules_for
    prof = PROFILES[strategy]
    rules: DayTradeRules = rules_for(cfg, strategy)
    q, tz = prof["gates"](cfg), cfg.tz
    today = now.astimezone(tz).date()
    now_ms = int(now.timestamp() * 1000)
    book = _book(store, today, prof["candidates"])
    final: list[dict] = []
    sent = 0
    for sym in rules.symbols:
        bars = [b for b in five.get(sym) or [] if b.t + FIVE <= now_ms]
        first = next((n for n, b in enumerate(bars) if _et(b.t, tz).date() == today), None)
        for su in scan(sym, bars, today, tz, rules, retest=True):
            if su.key in st["seen"]:
                continue
            if su.status == "pending":
                if su.key not in book["items"]:
                    book["items"][su.key] = dict(_base(su, tz), disposition="WATCH", reasons=[])
                continue
            st["seen"].append(su.key)
            rec = _base(su, tz)
            at = su.entry_t if su.entry_t else su.cross_t + FIVE
            i = next((n for n, b in enumerate(bars) if b.t + FIVE == at), None)
            if i is None or first is None:
                d = {"reasons": ["INVALID_DATA"], "components": {}}
            else:
                d = evaluate(su, bars, first, i, tz, rules, q, five, max(at, now_ms) if su.status == "entry" else at)
            reasons = list(d.pop("reasons"))
            rec.update(d)
            if su.status in ("failed", "no_retest"):
                reasons.insert(0, "NO_VALID_RETEST")
            elif su.status == "outside_hours" and "OUTSIDE_TRADING_WINDOW" not in reasons:
                reasons.insert(0, "OUTSIDE_TRADING_WINDOW")
            c = expiry = None
            if su.status == "entry":
                if now_ms - su.entry_t > 2 * FIVE:
                    reasons.insert(0, "STALE_SIGNAL")
                if sym in st["open"]:
                    reasons.append("IN_TRADE")
                if st["losses"].get(sym, 0) >= rules.max_losses_in_row:
                    reasons.append("DAILY_LOSS_LIMIT")
                if issues.get(sym):
                    reasons.append("DATA_HEALTH")
                    health.record(store, now, tz, sym, f"{strategy} entry", issues[sym])
                elif "STALE_SIGNAL" not in reasons:
                    c, expiry, opt, why = await _option(client, sym, su, today, now, tz, rules)
                    rec.update(opt)
                    if why:
                        reasons.append(why)
                    elif prof["spread"] == "health":                     # A's check: spread <= 10% of the mid
                        bad = health.spread_issue(opt.get("opt_bid"), opt.get("opt_ask"))
                        if bad:
                            reasons.append("DATA_HEALTH")
                            health.record(store, now, tz, sym, f"{strategy} entry", [f"{opt.get('option')}: {bad}"])
                    elif rec.get("opt_spread_pct") is None or rec["opt_spread_pct"] > q.max_spread_pct:
                        reasons.append("OPTION_SPREAD_TOO_WIDE")
            if rec.get("components"):
                sp = rec.get("opt_spread_pct")
                rec["components"]["option_spread"] = sp is not None and sp <= q.max_spread_pct
                rec["quality_score"], rec["quality_max"], rec["failed_components"] = score(rec["components"])
            trade = su.status == "entry" and not reasons and c is not None
            rec["disposition"] = "TRADE" if trade else "REJECT"
            rec["reasons"] = reasons
            if trade:
                sent += _open(store, cfg, now, su, c, expiry, rec, results, st, strategy)
            else:
                _skip(st, reasons[0] if reasons else "rejected")
            book["items"][su.key] = rec
            final.append(dict(rec, date=today.isoformat()))
            log.info("strategy %s %s %s %s: %s %s", strategy, sym, su.side, rec["cross_close"],
                     rec["disposition"], ",".join(reasons))
    store.put(prof["candidates"], book)
    if final:
        store.put(prof["log"], (store.get(prof["log"]) or [])[-(CAND_MAX - len(final)):] + final)
    return sent


def _open(store, cfg, now: datetime, su: Setup, c: dict, expiry: str, rec: dict, results: dict, st: dict,
          strategy: str = NAME) -> int:
    """Open the paper trade exactly as A does (ask fill, same size and management; D: its own first target)."""
    from .emacross import rules_for
    rules: DayTradeRules = rules_for(cfg, strategy)
    now_ms = int(now.timestamp() * 1000)
    sym = su.symbol
    bid, ask, mid = _quote(c)
    spot = rec.get("spot") or su.price
    tr = Trade.open(su, c["details"]["ticker"], float(c["details"]["strike_price"]), expiry, now_ms, ask or mid, rules)
    tr.entry_mid, tr.bid, tr.ask = mid, bid, ask
    tr.mark(now_ms, spot)
    r = results.get(sym)
    swing = r.stock.score if r is not None and r.stock else None
    intra = r.stock.composites.get("intraday", (None, ""))[0] if r is not None and r.stock else None
    delta = abs(((c.get("greeks") or {}).get("delta")) or 0) or None
    tr.info = entry_info(su, tr, rules, now_ms, spot, delta, swing, intra, cfg)
    tr.info.update({f"q_{k}": v for k, v in rec.items() if k not in ("key", "symbol", "side", "status", "price")})
    if strategy == D:                                   # D's report shows each trade's levels
        tr.info.update(underlying_entry=su.price, target_price=round(tr.entry * (1 + rules.first_target), 2),
                       stop_price=tr.stop, first_target_pct=round(100 * rules.first_target))
    st["open"][sym] = asdict(tr)
    line = _d_line(rec, tr, rules) if strategy == D else _qual_line(rec)
    store.enqueue("ema_entry", entry_text(su, tr, rules, cfg.tz, st["losses"].get(sym, 0), strategy) + "\n" + line,
                  sym)
    try:
        from . import scorecard
        scorecard.record(store, now.timestamp(), sym, STRATEGIES[strategy]["rule"], su.side, su.price, tr.contract,
                         tr.entry)
    except Exception:
        log.exception("strategy %s scorecard record failed", strategy)
    return 1


# ---- report --------------------------------------------------------------------------------------

LABELS = {"retest": "A · Cross + retest", "cross": "B · Cross, no retest", NAME: "C · Cross + retest + qualified",
          D: "D · Trend + time + 50/20"}
D_EXITS = {"stop": "STOP_-20", "trailed": "TRAILING_EXIT", "breakeven": "BREAKEVEN (runners after +50%)",
           "target": "TARGET (all sold)", "invalidated": "INVALIDATION", "time": "FORCE_CLOSE"}


def _row_stats(rows: list[dict], fee: float, contracts: int, signals: int | None = None) -> str:
    from .strategies import stats
    sig = f" · {signals} signal{'s' if signals != 1 else ''}" if signals is not None else ""
    if not rows:
        return "no trades" + sig
    g, s = stats(rows, 0, contracts), stats(rows, fee, contracts)
    fees = sum(2 * fee * (t.get("qty") or contracts) for t in rows)
    w = sum(t["pnl"] > 0 for t in rows)
    pf = f"{s['pf']:.2f}" if s["pf"] is not None else "–"
    win = _money(s["avg_win"]) if w else "–"
    loss = _money(s["avg_loss"]) if s["n"] - w else "–"
    return (f"{s['n']} trade{'s' if s['n'] != 1 else ''} · {w}W/{s['n'] - w}L ({s['win']:.0f}%) · gross "
            f"{_money(g['total'])} · fees ${fees:,.0f} · <b>net {_money(s['total'])}</b> · avg win {win} / loss "
            f"{loss} · PF {pf} · max DD {_money(s['max_dd'])} · avg/trade {_money(s['expectancy'])} · calls "
            f"{_money(_net(rows, 'bull', fee, contracts))} / puts {_money(_net(rows, 'bear', fee, contracts))}{sig}")


def _net(rows: list[dict], side: str, fee: float, contracts: int) -> float:
    return sum(t["pnl"] - 2 * fee * (t.get("qty") or contracts) for t in rows if t["side"] == side)


def _bucket(rows: list[dict], key, fee: float, contracts: int) -> list[str]:
    groups: dict[str, list[dict]] = {}
    for t in rows:
        groups.setdefault(str(key(t)), []).append(t)
    out = []
    for k, v in sorted(groups.items()):
        net = sum(t["pnl"] - 2 * fee * (t.get("qty") or contracts) for t in v)
        out.append(f"{k} {len(v)} ({sum(t['pnl'] > 0 for t in v)}W, {_money(net)})")
    return out


def _sep_bucket(x) -> str:
    return "n/a" if x is None else "<0.02%" if x < 0.02 else "0.02-0.05%" if x < 0.05 else "≥0.05%"


def _spread_bucket(x) -> str:
    return "n/a" if x is None else "≤5%" if x <= 5 else "5-8%" if x <= 8 else ">8%"


def _hour_bucket(hm: str | None) -> str:
    if not hm:
        return "n/a"
    return "09:35-11:00" if hm <= "11:00" else "11:00-13:30" if hm < "13:30" else "13:30-14:30" if hm <= "14:30" \
        else "after 14:30"


def _avg(xs: list, signed: bool = True) -> str:
    xs = [x for x in xs if x is not None]
    return (f"{sum(xs) / len(xs):+.2f}" if signed else f"{sum(xs) / len(xs):.1f}") if xs else "–"


def report(store, cfg, day: date) -> str:
    """The A/B/C comparison for one day: results, Strategy C's funnel and rejections, what C's filter would have
    avoided among A's and B's trades, C's breakdowns, MFE/MAE and early progress, whipsaw and spread impact."""
    rules: DayTradeRules = cfg.daytrade
    fee, n5 = rules.fee_per_contract, rules.contracts
    ds = day.isoformat()
    log_ = [t for t in store.get(LOG_KEY) or [] if t.get("date") == ds]
    by = {k: [t for t in log_ if t.get("strategy", "retest") == k] for k in LABELS}
    show = [k for k in LABELS if k != D or D in rules.strategies or by[D]]
    names = "/".join(LABELS[k][0] for k in show)
    lines = [f"🧪 <b>EMA {rules.fast}/{rules.slow} {names} comparison</b> (paper) {day:%a %b %d}",
             f"<i>Same bars, contracts, bid/ask fills and management; ${fee:.2f}/contract each way. "
             f"A and B are the controls; C and D are experiments.</i>"]
    for k in show:
        st = store.get(STRATEGIES[k]["key"]) or {}
        signals = len(st.get("seen") or []) if st.get("date") == ds else None
        lines.append(f"<b>{LABELS[k]}</b>: {_row_stats(by[k], fee, n5, signals)}")

    cands = [c for c in store.get(CAND_LOG) or [] if c.get("date") == ds]
    book = store.get(CANDIDATES) or {}
    watching = [c for c in (book.get("items") or {}).values() if c.get("disposition") == "WATCH"] \
        if book.get("date") == ds else []
    rej = [c for c in cands if c["disposition"] == "REJECT"]
    lines += ["", f"<b>Strategy C funnel</b>: {len(cands) + len(watching)} EMA crosses · "
                  f"{sum(c['disposition'] == 'TRADE' for c in cands)} traded · {len(rej)} rejected"
                  + (f" · {len(watching)} still watching" if watching else "")]
    if rej:
        primary: dict[str, int] = {}
        every: dict[str, int] = {}
        for c in rej:
            primary[c["reasons"][0]] = primary.get(c["reasons"][0], 0) + 1
            for r in c["reasons"]:
                every[r] = every.get(r, 0) + 1
        lines.append("First reason: " + " · ".join(f"{k} {v}" for k, v in sorted(primary.items(), key=lambda x: -x[1])))
        lines.append("Any reason: " + " · ".join(f"{k} {v}" for k, v in sorted(every.items(), key=lambda x: -x[1])))
        retested = [c for c in rej if c["reasons"][0] != "NO_VALID_RETEST"]
        if retested:
            lines.append(f"Confirmed retests C rejected: {len(retested)} (A's setups). Gates failed: " + " · ".join(
                f"{k} {v}" for k, v in sorted(_count(r for c in retested for r in c["reasons"]).items(),
                                              key=lambda x: -x[1])))

    # What C would have avoided among A's and B's actual trades (A by entry candle, B by cross candle).
    idx_entry = {(c["symbol"], c["side"], c.get("entry_close")): c for c in cands}
    idx_cross = {(c["symbol"], c["side"], c.get("cross_close")): c for c in cands}
    for k, idx in (("retest", idx_entry), ("cross", idx_cross)):
        rows = by[k]
        if not rows:
            continue
        groups: dict[str, list[dict]] = {}
        for t in rows:
            c = idx.get((t["symbol"], t["side"], (t.get("candle_close") or "")[:5]))
            g = "C took it too" if c and c["disposition"] == "TRADE" else \
                f"C rejected: {c['reasons'][0]}" if c else "no C candidate"
            groups.setdefault(g, []).append(t)
        lines.append(f"{LABELS[k].split(' · ')[0]}'s trades by C's decision: " + " · ".join(
            f"{g} {len(v)} ({sum(t['pnl'] > 0 for t in v)}W, "
            f"{_money(sum(t['pnl'] - 2 * fee * (t.get('qty') or n5) for t in v))})" for g, v in sorted(groups.items())))

    c_rows = by[NAME]
    if c_rows:
        lines += ["", "<b>Strategy C trades by</b>"]
        dims = [("regime", lambda t: t.get("q_regime")),
                ("VWAP", lambda t: "with" if (t.get("q_components") or {}).get("vwap") else "against"),
                ("time", lambda t: _hour_bucket(t.get("q_entry_close"))),
                ("EMA slope", lambda t: "aligned" if (t.get("q_components") or {}).get("ema_slope") else "not"),
                ("EMA separation", lambda t: _sep_bucket(t.get("q_ema_sep_pct"))),
                ("opening range", lambda t: t.get("q_or_position")),
                ("spread", lambda t: _spread_bucket(t.get("q_opt_spread_pct"))),
                ("SPY/QQQ", lambda t: {True: "aligned", False: "conflict", None: "unknown"}[t.get("q_market_aligned")]),
                ("symbol", lambda t: t["symbol"]), ("side", lambda t: "call" if t["side"] == "bull" else "put"),
                ("hour", lambda t: (t.get("in") or "??")[:2]),
                ("whipsaw", lambda t: f"{t.get('q_whipsaw_count', 0)} flips"),
                ("quality", lambda t: f"{t.get('q_quality_score')}/{t.get('q_quality_max')}")]
        for name, key in dims:
            lines.append(f"{name}: " + " · ".join(_bucket(c_rows, key, fee, n5)))

    lines += ["", "<b>MFE / MAE and early progress</b> (averages; stock %, option %)"]
    for k, label in LABELS.items():
        rows = by[k]
        if not rows:
            continue
        slow_ = [t for t in rows if t.get("first_10m_fav_pct") is not None and t["first_10m_fav_pct"] < 0.1]
        lines.append(
            f"{label.split(' · ')[0]}: stock MFE {_avg([t.get('under_mfe_pct') for t in rows])} / MAE "
            f"{_avg([t.get('under_mae_pct') for t in rows])} · option MFE {_avg([t.get('mfe_pct') for t in rows])} / "
            f"MAE {_avg([t.get('mae_pct') for t in rows])} · best stock move in first 5/10/15 min "
            f"{_avg([t.get('first_5m_fav_pct') for t in rows])}/{_avg([t.get('first_10m_fav_pct') for t in rows])}/"
            f"{_avg([t.get('first_15m_fav_pct') for t in rows])} · minutes to MFE "
            f"{_avg([t.get('time_to_mfe_min') for t in rows], False)}"
            + (f" · under 0.1% in 10 min: {len(slow_)} ({sum(t['pnl'] > 0 for t in slow_)}W, "
               f"{_money(sum(t['pnl'] for t in slow_))} gross)" if slow_ else ""))

    locks = [c for c in cands if c.get("chop_lock")]
    if cands:
        flips = max((c.get("whipsaw_count") or 0) for c in cands)
        lines += ["", f"<b>Whipsaw</b>: {len(locks)} candidates in a chop lock"
                      + (f" ({', '.join(sorted({c['symbol'] for c in locks}))})" if locks else "")
                      + f" · most flips in {rules_of(cfg).whipsaw_window_minutes} min: {flips}"]
    lines.append("<b>Spread cost</b> (mid P&L − bid/ask P&L): " + " · ".join(
        f"{LABELS[k].split(' · ')[0]} ${sum((t.get('pnl_mid') or t['pnl']) - t['pnl'] for t in by[k]):,.0f} "
        f"(avg entry spread {_avg([t.get('entry_spread_pct') for t in by[k]], False)}%)" for k in LABELS if by[k]))
    if D in show:
        lines += _d_section(store, cfg, day, by[D])
    lines.append("<i>One day of evidence: decide KEEP / MODIFY / DISCARD / PROMOTE only after review.</i>")
    return "\n".join(lines)


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def _d_section(store, cfg, day: date, rows: list[dict]) -> list[str]:
    """Strategy D: funnel and rejections, exit-reason counts, and every trade with its levels and diagnostics."""
    from .emacross import rules_for
    rules = rules_for(cfg, D)
    fee, n5, ds = rules.fee_per_contract, rules.contracts, day.isoformat()
    cands = [c for c in store.get(PROFILES[D]["log"]) or [] if c.get("date") == ds]
    rej = [c for c in cands if c["disposition"] == "REJECT"]
    lines = ["", f"<b>Strategy D</b> (EMA 9/21 D — Trend + Time + 50/20): {len(cands)} EMA crosses · "
                 f"{sum(c['disposition'] == 'TRADE' for c in cands)} traded · {len(rej)} rejected"]
    if rej:
        lines.append("D rejections (first reason): " + " · ".join(
            f"{k} {v}" for k, v in sorted(_count(c["reasons"][0] for c in rej).items(), key=lambda x: -x[1])))
    if not rows:
        lines.append("<i>No D trade today: a valid outcome (about one a week in the backtest).</i>")
        return lines
    ex = _count(D_EXITS.get(t["exit_reason"], "OTHER") for t in rows)
    hit = sum((t.get("mfe_pct") or 0) >= (t.get("first_target_pct") or 100 * rules.first_target) for t in rows)
    lines.append(f"D exits: first target +{100 * rules.first_target:.0f}% reached {hit} · " + " · ".join(
        f"{k} {ex.get(k, 0)}" for k in ("STOP_-20", "TRAILING_EXIT", "BREAKEVEN (runners after +50%)",
                                        "TARGET (all sold)", "INVALIDATION", "FORCE_CLOSE", "OTHER")))
    for t in rows:
        qty = t.get("qty") or n5
        fees = 2 * fee * qty
        exit_avg = t["entry"] + t["pnl"] / (100 * qty)
        under = t.get("underlying_entry")
        lines.append(
            f"{'🟢 CALL' if t['side'] == 'bull' else '🔴 PUT'} {t['symbol']} in {t['in']} (stock {under:.2f}) · "
            f"{t.get('ticker') or t['contract']} at {t['entry']:.2f} · target {t.get('target_price') or 0:.2f} · "
            f"stop {t.get('stop_price') or 0:.2f} → out {t['out']} avg exit {exit_avg:.2f} · {t['exit_reason']} · "
            f"gross {_money(t['pnl'])} · fees ${fees:.2f} · net {_money(t['pnl'] - fees)}"
            if under is not None else f"{t['symbol']} {t['in']} {t['contract']} {t['exit_reason']} {_money(t['pnl'])}")
        c = t.get("q_components") or {}
        lines.append(
            f"   regime {t.get('q_regime')} · window {_hour_bucket(t.get('q_entry_close'))} · EMA9 {t.get('q_ema9')} / "
            f"EMA21 {t.get('q_ema21')} · retest {t.get('q_retest_at')} · option MFE {t.get('mfe_pct', 0):+.0f}% / "
            f"MAE {t.get('mae_pct', 0):+.0f}% · <i>diagnostics: VWAP {'with' if c.get('vwap') else 'against'}, "
            f"OR {t.get('q_or_position')}, slopes {'aligned' if c.get('ema_slope') else 'not'}, SPY/QQQ "
            f"{ {True: 'aligned', False: 'conflict', None: 'unknown'}[t.get('q_market_aligned')]}</i>")
    return lines
