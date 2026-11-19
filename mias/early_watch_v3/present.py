"""V3 presentation: three-state checks, margins, invalidation distance, options line, quality line, flags and the
Telegram text. PRESENTATION ONLY: nothing here is read by the detector, and nothing here creates, suppresses, delays,
invalidates or changes a Watch. ``build_view`` returns every displayed value; ``render`` turns that stored view into
the message, so stored == displayed. No strike / expiration / contract / CALL-PUT pick / target / stop / profit text.

Check states, relative to the Watch direction: ✓ supports · ✗ against · ○ neutral / unavailable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import FAMILIES, Presentation

FOOTER = "WATCH ONLY — NOT A TRADE SIGNAL"
TAG = {"structure": "V3-A Structure", "vwap": "V3-B VWAP", "or": "V3-C OR"}
MARK = {"support": "✓", "against": "✗", "neutral": "○"}
INDEX = ("SPY", "QQQ", "IWM")
CHECKS = ("vwap", "ema", "sma200", "spy", "volume", "options")
WORD = {"CONFIRMING": "CONFIRMING", "CONFLICTING": "CONFLICTING", "NEUTRAL": "NEUTRAL", "MIXED": "MIXED",
        "INSUFFICIENT_DATA": "INSUFFICIENT DATA", None: "INSUFFICIENT DATA"}
VIEW_DIR = {"BULLISH_CONFIRMATION": "bullish", "BEARISH_CONFIRMATION": "bearish"}


def _num(x: float, fmt: str) -> str:
    """Signed numbers with a real minus sign (e.g. SPY −1.24%)."""
    return format(x, fmt).replace("-", "−")


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def relation(view: str | None, side: str) -> str:
    """= mias.observer.optctx.relation (relative verdict of a view for a side)."""
    if view in (None, "INSUFFICIENT_DATA"):
        return "INSUFFICIENT_DATA"
    if view in VIEW_DIR:
        return "CONFIRMING" if (VIEW_DIR[view] == "bullish") == (side == "bull") else "CONFLICTING"
    return "NEUTRAL"


def _dist(price: float, ref: float | None, atr: float | None):
    if ref is None or not ref:
        return None, None
    d = price - ref
    return round(100 * d / ref, 3), (round(d / atr, 3) if atr else None)


def _margin(pct, atr_d) -> str:
    s = _num(pct, "+.2f") + "%"
    return s + (f" · {_num(atr_d, '+.2f')} ATR" if atr_d is not None else "")


def build_view(w: dict, open_fams: list[str], vwap_progress: tuple[int, int] | None, pres: Presentation,
               tz: ZoneInfo, t_close_ms: int, options_fresh_s: float = 330,
               open_levels: dict | None = None) -> dict:
    """Everything the V3 message shows, as plain data (stored with the Watch)."""
    bull = w["side"] == "bull"
    sg = 1 if bull else -1
    ctx = w.get("context") or {}
    ema = w.get("ema_watch") or {}
    price = float(w["watch_price"])
    atr = ctx.get("atr14_5m")
    fams = [f for f in FAMILIES if f in set(open_fams) | {w["family"]}]
    v: dict = {"family": w["family"], "families": fams, "sym": w["sym"], "side": w["side"], "price": price,
               "watch_at": w["watch_at"], "atr": atr, "is_index": w["sym"] in INDEX}
    checks: dict[str, tuple[str, str]] = {}

    # VWAP (wording follows the detector state: confirmed V3-B watch, pending reclaim / loss, or plain position)
    vw = ctx.get("vwap_exact", ctx.get("vwap"))
    pct, ad = _dist(price, vw, atr)
    v.update(vwap=vw, vwap_dist_pct=pct, vwap_dist_atr=ad,
             vwap_marginal=ad is not None and abs(ad) < pres.marginal_atr)
    marg = " (marginal)" if v["vwap_marginal"] else ""
    if vw is None:
        checks["vwap"] = ("neutral", "VWAP n/a")
    else:
        word = "Above" if price > vw else "Below" if price < vw else "At"
        where = f"{word} VWAP ${vw:,.2f} {_margin(pct, ad)}{marg}"
        if "vwap" in fams and sg * (price - vw) > 0:
            checks["vwap"] = ("support", where)
        elif vwap_progress is not None:
            k, n = vwap_progress
            checks["vwap"] = ("neutral", f"VWAP {'reclaim' if bull else 'loss'} developing — {k}/{n} closes "
                                         f"{'above' if bull else 'below'}")
        elif sg * (price - vw) > 0:
            checks["vwap"] = ("support", where)
        elif sg * (price - vw) < 0:
            checks["vwap"] = ("against", where)
        else:
            checks["vwap"] = ("neutral", where)

    # EMA9 / EMA21 on completed 5-minute bars (provisional cross = developing)
    e9, e21 = ema.get("ema9_c"), ema.get("ema21_c")
    if e9 is None or e21 is None:
        checks["ema"] = ("neutral", "EMA9/21 (5m) n/a")
    elif sg * (e9 - e21) >= 0:
        checks["ema"] = ("support", f"EMA9 {'above' if bull else 'below'} EMA21 (5m)")
    elif ema.get("crossed_provisional"):
        checks["ema"] = ("neutral", f"EMA9/21 (5m) cross {'up' if bull else 'down'} developing")
    else:
        checks["ema"] = ("against", f"EMA9 {'below' if bull else 'above'} EMA21 (5m)")

    # SMA200 (daily closes)
    sma = ctx.get("sma200")
    if sma is None:
        checks["sma200"] = ("neutral", "SMA200 (daily) n/a")
    else:
        p2, _ = _dist(price, sma, None)
        word = "Above" if price > sma else "Below" if price < sma else "At"
        st = "support" if sg * (price - sma) > 0 else "against" if sg * (price - sma) < 0 else "neutral"
        checks["sma200"] = (st, f"{word} SMA200 ${sma:,.2f} ({_num(p2, '+.1f')}%)")

    # SPY direction today
    spy = ctx.get("spy_pct")
    if w["sym"] == "SPY":
        checks["spy"] = ("neutral", "SPY is the Watch symbol")
    elif spy is None:
        checks["spy"] = ("neutral", "SPY n/a")
    else:
        st = "support" if sg * spy > 0 else "against" if sg * spy < 0 else "neutral"
        checks["spy"] = (st, f"SPY {_num(spy, '+.2f')}%")

    # Relative volume (existing rel_vol; no new data)
    rv = ctx.get("rel_vol")
    v["rel_vol"] = rv
    v["low_conviction"] = rv is not None and rv < pres.low_conviction_rvol
    if rv is None:
        checks["volume"] = ("neutral", "Volume n/a")
    elif rv >= pres.volume_support_rvol:
        checks["volume"] = ("support", f"Volume {rv:.1f}× normal")
    elif rv < pres.low_conviction_rvol:
        checks["volume"] = ("against", f"Volume {rv:.1f}× normal (low)")
    else:
        checks["volume"] = ("neutral", f"Volume {rv:.1f}× normal")

    # Options (existing MIAS options / PCR context; never waited for)
    x = ctx.get("optx") or None
    if not x or x.get("status") == "unavailable":
        stock = market = ovr = "INSUFFICIENT_DATA"
        o_status, age, snap = "unavailable", None, None
    else:
        age, snap = x.get("age_s"), x.get("snapshot_ts")
        o_status = "stale" if (age is not None and age > options_fresh_s) or x.get("status") == "stale" else \
            x.get("status") or "ok"
        stock = (x.get("relation") or {}).get(w["side"]) or relation(x.get("view"), w["side"])
        market = relation(x.get("market_view"), w["side"]) if x.get("market_view") else None
        ovr = (x.get("overall") or {}).get(w["side"]) or stock
    mismatch = None
    mv = (x or {}).get("market_view")
    if mv in VIEW_DIR and spy is not None and w["sym"] != "SPY":
        if (VIEW_DIR[mv] == "bullish" and spy < 0) or (VIEW_DIR[mv] == "bearish" and spy > 0):
            mismatch = f"Market mismatch: options {VIEW_DIR[mv]} while SPY {_num(spy, '+.2f')}%"
    v.update(options_status=o_status, options_age_s=age, options_snapshot_ts=snap, options_stock=stock,
             options_market=market, options_overall=ovr, market_mismatch=mismatch)
    ost = "support" if ovr == "CONFIRMING" else "against" if ovr == "CONFLICTING" else "neutral"
    checks["options"] = (ost, f"Options: {WORD.get(ovr, ovr)}")

    v["checks"] = {k: {"state": checks[k][0], "text": checks[k][1]} for k in CHECKS}
    v["n_support"] = sum(c[0] == "support" for c in checks.values())
    v["n_against"] = sum(c[0] == "against" for c in checks.values())
    v["n_neutral"] = sum(c[0] == "neutral" for c in checks.values())

    # Trigger (only the Watch direction, per the actual detector state) and level margins
    lvl = w.get("level")
    trig = []
    for f in fams:
        if f == "structure":
            lv = lvl if w["family"] == "structure" else (open_levels or {}).get("structure")
            trig.append(f"{'developing HL' if bull else 'developing LH'}"
                        + (f" at ${lv:,.2f}" if lv is not None else "") + f" ({TAG[f]})")
        elif f == "vwap":
            trig.append(f"{'VWAP reclaim' if bull else 'VWAP loss'} — held "
                        f"{'above' if bull else 'below'} ({TAG[f]})")
        else:
            orl = ctx.get("or_high") if bull else ctx.get("or_low")
            s = f"{'OR breakout above' if bull else 'OR breakdown below'}"
            if orl is not None:
                p3, a3 = _dist(price, orl, atr)
                s += f" ${orl:,.2f} {_margin(p3, a3)}" + (" (marginal)" if a3 is not None and
                                                           abs(a3) < pres.marginal_atr else "")
            trig.append(s + f" ({TAG[f]})")
    v["triggers"] = trig

    # Invalidation (V2 logic and lifecycle unchanged; this only shows the distance)
    inv_ref = vw if w["family"] == "vwap" else lvl
    v["inval_kind"] = "two_closes_through_vwap" if w["family"] == "vwap" else "close_beyond_level"
    v["inval_level"] = inv_ref
    if inv_ref:
        room = sg * (price - inv_ref)
        v["inval_dist_pct"] = round(100 * room / price, 3)
        v["inval_dist_atr"] = round(room / atr, 3) if atr else None
    else:
        v["inval_dist_pct"] = v["inval_dist_atr"] = None
    v["tight"] = v["inval_dist_atr"] is not None and v["inval_dist_atr"] < pres.tight_invalidation_atr

    t = _et(w["watch_at"], tz)
    v["hhmm"] = f"{t:%H:%M}"
    v["minutes_to_close"] = max(0, int((t_close_ms - w["watch_at"]) // 60_000))
    v["late_session"] = t.time() >= pres.late_session_et
    return v


def render(v: dict) -> str:
    bull = v["side"] == "bull"
    tags = " + ".join(TAG[f] for f in v["families"])
    head = (f"🧭 V3 EARLY WATCH · {tags} · {v['sym']} · {'BULLISH' if bull else 'BEARISH'} · {v['hhmm']} ET @ "
            f"${v['price']:,.2f}" + (" · low conviction" if v["low_conviction"] else ""))
    lines = [head]
    lines += [f"Trigger: {t}" for t in v["triggers"]]
    lines.append("Checks:")
    lines += [f"{MARK[v['checks'][k]['state']]} {v['checks'][k]['text']}" for k in CHECKS]
    lines.append(f"Quality: Supports {v['n_support']} · Against {v['n_against']} · Neutral {v['n_neutral']}")
    mk = "n/a (index)" if v["is_index"] and v["options_market"] is None else WORD.get(v["options_market"], "n/a")
    lines.append(f"Stock options: {WORD.get(v['options_stock'])} | Market options: {mk} | "
                 f"Overall: {WORD.get(v['options_overall'])}")
    if v.get("market_mismatch"):
        lines.append(v["market_mismatch"])
    if v["options_status"] == "unavailable":
        lines.append("Options: unavailable")
    elif v["options_status"] == "stale":
        lines.append("Options: STALE" + (f" (age {v['options_age_s']:.0f}s)" if v["options_age_s"] is not None
                                          else ""))
    elif v["options_age_s"] is not None:
        lines.append(f"Options age: {v['options_age_s']:.0f}s")
    if v["atr"]:
        lines.append(f"ATR (14 × 5m): ${v['atr']:,.2f}")
    tight = " · tight — noise risk" if v["tight"] else ""
    if v["inval_level"] is not None:
        dist = f"{_num(v['inval_dist_pct'], '+.2f')}%" + (f" · {v['inval_dist_atr']:.2f} ATR"
                                                         if v["inval_dist_atr"] is not None else "")
        if v["family"] == "vwap":
            lines.append(f"Invalidation: 2 closes back {'below' if bull else 'above'} VWAP "
                         f"(now ${v['inval_level']:,.2f} · {dist}){tight}")
        else:
            lines.append(f"Invalidation: ${v['inval_level']:,.2f} · {dist}{tight}")
    lines.append(f"Minutes to close: {v['minutes_to_close']}" + (" · late session" if v["late_session"] else ""))
    lines.append(FOOTER)
    return "\n".join(lines)
