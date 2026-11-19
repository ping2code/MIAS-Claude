"""V3 EARLY WATCH detector: a clone of the current V2 detector (mias/observer/v2.py V2Observer, frozen) with V3's own
state instances and config values (early_watch_v3.toml, copied from V2). Same families, definitions, thresholds,
persistence, lifecycle and ids, so on identical bars the candidate / Watch timestamps and directions equal V2's
(tests/test_early_watch_v3.py and the daily V2-vs-V3 comparison check this).

  structure  V3-A: developing Higher Low / Lower High, kept for need_structure more close(s)
  vwap       V3-B: close reclaims / loses VWAP after vwap_prior_closes closes on the other side, holds need_vwap closes
  or         V3-C: close breaks the opening range after a close inside it, then holds need_or closes

Additions are context only (never read by the detection): the ATR, SMA200 value and completed EMA values stored with
each Watch, and ``vwap_progress`` (read by the presentation). Pure: process() returns plain dict events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo

from ..indicators import Bar
from .config import FAMILIES, ONE, DetectRules
from .state import Features, SymbolState, at

VERSION = "early-watch-v3 (detection = early-watch-v2-research)"
SIGN = {"bull": 1, "bear": -1}


@dataclass
class Pending:
    cand: dict
    need: int
    anchor_t: int | None = None
    level: float | None = None


@dataclass
class SymV3:
    hist: list = field(default_factory=list)
    ema_diff: float | None = None
    ema9_prev: float | None = None
    slope: float | None = None


class V3Detector:
    def __init__(self, symbols: list[str], day: date, tz: ZoneInfo, rules: DetectRules = DetectRules(),
                 context=None, market: str = "SPY"):
        self.symbols, self.day, self.tz, self.r = list(symbols), day, tz, rules
        self.need = rules.need
        self.states = {s: SymbolState(s, tz, rules) for s in self.symbols}
        self.sym = {s: SymV3() for s in self.symbols}
        self.pending: dict[tuple, Pending] = {}
        self.open: dict[tuple, dict] = {}
        self.used_legs: set = set()
        self.confirms: list[dict] = []
        self.seen_confirms: set = set()
        self.last_close: dict[str, float] = {}
        self.context = context
        self.market = market
        self.market_pct: list[tuple[int, float | None]] = []
        self.t_open = at(day, rules.session_open, tz)
        self.t_main0 = at(day, rules.main_start, tz)
        self.t_main1 = at(day, rules.main_end, tz)
        self.t_close = at(day, rules.session_close, tz)

    def warm(self, sym: str, sessions, daily_closes) -> None:
        st = self.states[sym]
        st.warm(sessions, daily_closes)
        st.start_day(self.day, None)
        sv = self.sym[sym]
        if st.ema9_c is not None and st.ema21_c is not None:
            sv.ema_diff = st.ema9_c - st.ema21_c
            sv.ema9_prev = st.ema9_c

    def add_confirm(self, ev: dict) -> bool:
        key = (ev["sym"], ev["side"], round(float(ev["ts"]), 3), ev["source"])
        if key in self.seen_confirms or ev["sym"] not in self.states:
            return False
        self.seen_confirms.add(key)
        self.confirms.append(dict(ev, ts_ms=int(float(ev["ts"]) * 1000)))
        self.confirms.sort(key=lambda x: (x["ts_ms"], x["sym"], x["source"]))
        return True

    # ---- one bar ----------------------------------------------------------------------------------

    def process(self, sym: str, bar: Bar, gap: bool = False, received_at: float | None = None) -> list[dict]:
        out: list[dict] = []
        end = bar.t + ONE
        out += self._apply_confirms(sym, end)
        f = self.states[sym].on_minute(bar, gap)
        sv = self.sym[sym]
        if f.closed5 and f.ema9_c is not None and f.ema21_c is not None:
            d = f.ema9_c - f.ema21_c
            if sv.ema_diff is not None:
                if d >= 0 > sv.ema_diff:
                    out.append({"kind": "v3_ema_cross", "day": str(self.day), "sym": sym, "side": "bull", "at": f.e})
                elif d <= 0 < sv.ema_diff:
                    out.append({"kind": "v3_ema_cross", "day": str(self.day), "sym": sym, "side": "bear", "at": f.e})
            sv.slope = None if sv.ema9_prev is None else f.ema9_c - sv.ema9_prev
            sv.ema_diff, sv.ema9_prev = d, f.ema9_c
        if sym == self.market:
            self.market_pct.append((f.e, f.day_pct))
        prev = sv.hist[-1] if sv.hist else None
        out += self._watches(sym, f, prev)
        out += self._pendings(sym, f)
        out += self._candidates(sym, f, sv.hist, received_at)
        sv.hist = (sv.hist + [f])[-max(4, self.r.vwap_prior_closes + 1):]
        self.last_close[sym] = f.c
        return out

    def finalize(self) -> list[dict]:
        out = []
        for sym in self.symbols:
            out += self._apply_confirms(sym, self.t_close + 3_600_000)
        for key in list(self.pending):
            p = self.pending.pop(key)
            out.append(self._cand_end(p.cand, "not_confirmed", self.t_close))
        for key in list(self.open):
            out.append(self._end(key, "EXPIRED", "session_end", self.t_close))
        return out

    # ---- presentation hooks (read-only) ---------------------------------------------------------------

    def vwap_progress(self, sym: str, side: str) -> tuple[int, int] | None:
        """A pending VWAP reclaim / loss on sym / side: (hold closes done, hold closes needed)."""
        p = self.pending.get(("vwap", sym, side))
        if p is None:
            return None
        return self.need["vwap"] - p.need, self.need["vwap"]

    def open_families(self, sym: str, side: str) -> list[str]:
        return [f for f in FAMILIES if (f, sym, side) in self.open]

    # ---- confirmed events ---------------------------------------------------------------------------

    def _apply_confirms(self, sym: str, before_ms: int) -> list[dict]:
        out, keep = [], []
        wm = self.r.watch_minutes * ONE
        for ev in self.confirms:
            if ev["sym"] != sym or ev["ts_ms"] >= before_ms:
                keep.append(ev)
                continue
            for key, w in list(self.open.items()):
                if w["sym"] == sym and w["side"] == ev["side"] and w["watch_at"] <= ev["ts_ms"] <= w["watch_at"] + wm:
                    w["upgrade_source"], w["upgrade_at"] = ev["source"], ev["ts_ms"]
                    w["upgrade_price"] = self.last_close.get(sym)
                    out.append(self._end(key, "UPGRADED", ev["source"], ev["ts_ms"]))
        self.confirms = keep
        return out

    # ---- open watches -------------------------------------------------------------------------------

    def _watches(self, sym: str, f: Features, prev: Features | None) -> list[dict]:
        out = []
        for key, w in list(self.open.items()):
            if w["sym"] != sym:
                continue
            if f.e >= w["watch_at"] + self.r.watch_minutes * ONE:
                out.append(self._end(key, "EXPIRED", f"{self.r.watch_minutes}_min_no_upgrade", f.e))
                continue
            sg, fam = SIGN[w["side"]], w["family"]
            reason = None
            if fam in ("structure", "or") and sg * (f.c - w["level"]) < 0:
                reason = "close_beyond_level"
            elif fam == "vwap" and f.vwap is not None:
                w["_through"] = w.get("_through", 0) + 1 if sg * (f.c - f.vwap) <= 0 else 0
                if w["_through"] >= self.r.vwap_invalid_closes:
                    reason = "two_closes_through_vwap" if self.r.vwap_invalid_closes == 2 else \
                        f"{self.r.vwap_invalid_closes}_closes_through_vwap"
            if reason:
                out.append(self._end(key, "INVALIDATED", reason, f.e))
        return out

    def _end(self, key, state: str, reason: str, at_ms: int) -> dict:
        w = self.open.pop(key)
        w["state"], w["end_at"], w["end_reason"] = state, at_ms, reason
        return {"kind": "v3_end", "watch_id": w["watch_id"], "state": state, "end_at": at_ms, "end_reason": reason,
                "upgrade_source": w.get("upgrade_source"), "upgrade_at": w.get("upgrade_at"),
                "upgrade_price": w.get("upgrade_price"), "family": w["family"], "sym": w["sym"], "side": w["side"]}

    # ---- candidates -> watches ----------------------------------------------------------------------

    def _pendings(self, sym: str, f: Features) -> list[dict]:
        out = []
        for key, p in list(self.pending.items()):
            fam, s, side = key
            if s != sym:
                continue
            sg = SIGN[side]
            if f.stale:
                del self.pending[key]
                out.append(self._cand_end(p.cand, "stale", f.e))
                continue
            if fam == "structure":
                c = f.hl if side == "bull" else f.lh
                ok = c is not None and c.anchor.bar_t == p.anchor_t and c.level == p.level
            elif fam == "vwap":
                ok = f.vwap is not None and sg * (f.c - f.vwap) > 0
            else:
                ok = sg * (f.c - p.level) > 0
            if not ok:
                del self.pending[key]
                out.append(self._cand_end(p.cand, "not_confirmed", f.e))
                continue
            p.need -= 1
            if p.need == 0:
                del self.pending[key]
                out.append(self._cand_end(p.cand, "watched", f.e))
                out.append(self._watch(key, p, f))
        return out

    def _candidates(self, sym: str, f: Features, hist: list, received_at) -> list[dict]:
        found: list[tuple[str, str, str, float | None, int | None]] = []
        for name, c in f.dev_events:
            if name in ("DEV_HL", "DEV_LH"):
                found.append(("structure", c.side, f"P{c.anchor.bar_t}", c.level, c.anchor.bar_t))
        n = self.r.vwap_prior_closes
        if f.vwap is not None and len(hist) >= n and all(h.vwap is not None for h in hist[-n:]):
            prev = hist[-n:]
            if f.c > f.vwap and all(h.c <= h.vwap for h in prev):
                found.append(("vwap", "bull", f"VW{f.t}", None, None))
            elif f.c < f.vwap and all(h.c >= h.vwap for h in prev):
                found.append(("vwap", "bear", f"VW{f.t}", None, None))
        if f.or_done and f.t >= self.t_main0 and hist and f.or_high is not None and f.or_low is not None:
            p = hist[-1]
            if f.c > f.or_high and p.c <= f.or_high:
                found.append(("or", "bull", f"OR{f.t}", f.or_high, None))
            elif f.c < f.or_low and p.c >= f.or_low:
                found.append(("or", "bear", f"OR{f.t}", f.or_low, None))
        out = []
        for fam, side, leg, level, anchor in found:
            key = (fam, sym, side)
            cand = {"cand_id": f"{self.day}|{fam}|{sym}|{side}|{leg}|{f.t}", "day": str(self.day), "family": fam,
                    "sym": sym, "side": side, "leg_id": leg, "candidate_at": f.e, "candidate_price": f.c,
                    "ema": self._ema_ctx(f, side), "received_at": received_at}
            cand["leg_start"] = self._leg_start(sym, side)
            status = None
            if f.stale:
                status = "stale"
            elif (fam, sym, side, leg) in self.used_legs:
                status = "same_leg"
            elif key in self.open:
                status = "open_watch_exists"
            elif key in self.pending:
                status = "pending_exists"
            cand["status"] = status or "pending"
            out.append({"kind": "v3_candidate", **cand})
            if status is None:
                self.pending[key] = Pending(cand, self.need[fam], anchor, level)
        return out

    def _cand_end(self, cand: dict, status: str, at_ms: int) -> dict:
        return {"kind": "v3_candidate_end", "cand_id": cand["cand_id"], "status": status, "end_at": at_ms}

    def _watch(self, key, p: Pending, f: Features) -> dict:
        fam, sym, side = key
        cand = p.cand
        if f.t >= self.t_main0 and f.e <= self.t_main1:
            lane = "main"
        elif self.t_open <= f.t < self.t_main0:
            lane = "opening"
        else:
            lane = "late"
        level = f.vwap if fam == "vwap" else p.level
        ema_w = self._ema_ctx(f, side)
        spy = next((pct for e, pct in reversed(self.market_pct) if e <= f.e), None)
        ctx = {"rel_vol": _r(f.rel_vol, 2),
               "sma200_side": None if f.sma200 is None else ("above" if f.c > f.sma200 else "below"),
               "spy_pct": _r(spy, 3), "spy_dir": None if spy is None else ("UP" if spy > 0 else "DOWN" if spy < 0
                                                                            else "FLAT"),
               "rsi14_5m": _r(f.rsi14, 1), "macd_hist_5m": _r(f.macd_hist, 4), "day_pct": _r(f.day_pct, 3),
               "vwap": _r(f.vwap, 4), "or_high": f.or_high, "or_low": f.or_low,
               "minutes_since_open": f.minutes_since_open,
               # V3 context additions (stored with the Watch; never read by the detection)
               "atr14_5m": _r(f.atr, 4), "sma200": _r(f.sma200, 4), "vwap_exact": f.vwap}
        if self.context:
            try:
                ctx.update(self.context(sym, f.e) or {})
            except Exception as exc:
                ctx["context_error"] = str(exc)[:120]
        w = {"watch_id": f"{self.day}|{fam}|{sym}|{side}|{cand['leg_id']}", "cand_id": cand["cand_id"],
             "day": str(self.day), "family": fam,
             "lane": lane, "sym": sym, "side": side, "leg_id": cand["leg_id"],
             "candidate_at": cand["candidate_at"], "candidate_price": cand["candidate_price"],
             "watch_at": f.e, "watch_price": f.c, "level": level, "pre_ema": not ema_w["crossed_completed"],
             "ema_candidate": cand["ema"], "ema_watch": ema_w, "leg_start": cand["leg_start"], "context": ctx,
             "state": "WATCH", "rules_version": VERSION}
        self.open[key] = w
        self.used_legs.add((fam, sym, side, cand["leg_id"]))
        return {"kind": "v3_watch", **w}

    # ---- context ------------------------------------------------------------------------------------

    def _ema_ctx(self, f: Features, side: str) -> dict:
        sg = SIGN[side]
        sv = self.sym[f.sym]
        crossed_c = None if f.ema9_c is None or f.ema21_c is None else sg * (f.ema9_c - f.ema21_c) >= 0
        crossed_p = None if f.ema9 is None or f.ema21 is None else sg * (f.ema9 - f.ema21) >= 0
        return {"ema9": _r(f.ema9, 4), "ema21": _r(f.ema21, 4), "spread": _r(f.spread, 4),
                "spread_change": None if f.spread is None or f.spread_ref is None else _r(f.spread - f.spread_ref, 4),
                "ema9_slope_5m": _r(sv.slope, 4), "crossed_completed": bool(crossed_c),
                "crossed_provisional": bool(crossed_p),
                "ema9_c": _r(f.ema9_c, 4), "ema21_c": _r(f.ema21_c, 4)}

    def _leg_start(self, sym: str, side: str) -> dict | None:
        p = self.states[sym].structure.last("L" if side == "bull" else "H")
        return None if p is None else {"price": p.price, "bar_t": p.bar_t, "confirmed_at": p.confirmed_at}


def _r(x, n):
    return None if x is None else round(x, n)
