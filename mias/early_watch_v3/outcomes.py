"""Forward outcomes of a V3 Watch from the completed 1-minute bars after it (research only, never fed back).

signed move at h minutes = side x (close of the bar ending at watch_at + h - Watch price) / Watch price, in %;
directional success = signed move > 0. MFE / MAE: best / worst excursion of the 1-minute highs / lows in
(watch_at, watch_at + 60 min], capped at the session close; in % and in ATR (the ATR stored with the Watch).
A horizon past the session close (or without its bar) stays empty.
"""
from __future__ import annotations

from .store import HORIZONS

ONE = 60_000


def outcomes(w: dict, bars: list[tuple], t_close_ms: int) -> dict:
    sg = 1 if w["side"] == "bull" else -1
    p = float(w["watch_price"])
    t0 = int(w["watch_at"])
    by_end = {int(s) + ONE: (h, l, c) for s, o, h, l, c, v in bars}
    out: dict = {}
    for hz in HORIZONS:
        e = t0 + hz * ONE
        r = by_end.get(e) if e <= t_close_ms else None
        out[f"out_{hz}"] = None if r is None else round(100 * sg * (r[2] - p) / p, 4)
    end = min(t0 + 60 * ONE, t_close_ms)
    win = [(h, l) for e, (h, l, c) in by_end.items() if t0 < e <= end]
    if win:
        fav = max(sg * ((h if sg > 0 else l) - p) for h, l in win)
        adv = min(sg * ((l if sg > 0 else h) - p) for h, l in win)
        out["mfe_pct"], out["mae_pct"] = round(100 * fav / p, 4), round(100 * adv / p, 4)
        atr = w.get("atr") or (w.get("context") or {}).get("atr14_5m")
        out["mfe_atr"] = round(fav / atr, 3) if atr else None
        out["mae_atr"] = round(adv / atr, 3) if atr else None
    else:
        out.update(mfe_pct=None, mae_pct=None, mfe_atr=None, mae_atr=None)
    return out
