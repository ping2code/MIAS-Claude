"""Telegram text for a future Early Watch pilot. PREPARED ONLY: nothing in mias.observer sends messages, and the
watches table's ``sent`` flag stays 0. No strike, expiry, contract, option price, target, stop or trade advice."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def watch_text(w: dict, tz: ZoneInfo) -> str:
    bull = w["side"] == "bull"
    t = datetime.fromtimestamp(w["created_at"] / 1000, timezone.utc).astimezone(tz)
    ctx = w.get("context") or {}
    rv = ctx.get("rel_vol")
    struct = {"HL": "Higher Low developing", "LH": "Lower High developing",
              "ORB": f"Opening-range {'high' if bull else 'low'} break held 2 closes"}[w["structure_type"]]
    spy = ctx.get("spy_pct")
    lines = [
        f"👀 EARLY WATCH · {w['sym']} · {'BULLISH' if bull else 'BEARISH'} · {t:%H:%M} ET @ ${w['price']:,.2f}",
        f"Possible {'bullish' if bull else 'bearish'} continuation",
        f"✓ {'Above' if bull else 'Below'} VWAP · 2 closes",
        f"✓ EMA9 {'>' if bull else '<'} EMA21 · spread widening",
        f"✓ {struct}",
        f"○ Volume {rv:.1f}× normal" if rv is not None else "○ Volume n/a",
        f"○ SMA200 {ctx['sma200_side']}" if ctx.get("sma200_side") else "○ SMA200 n/a",
        f"○ SPY {spy:+.2f}%" if spy is not None else "○ SPY n/a",
        "○ Options not confirmed",
        f"Invalid {'below' if bull else 'above'}: ${w['level']:,.2f}",
        "WATCH ONLY — NOT A TRADE SIGNAL",
    ]
    return "\n".join(lines)
