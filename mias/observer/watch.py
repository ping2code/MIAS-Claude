"""Early Watch v1: a pure, deterministic state machine over completed 1-minute bars.

    NORMAL --criteria on 2 consecutive completed 1m closes--> WATCH
    WATCH  --existing confirmed event, same symbol + side, within 60 min--> UPGRADED
    WATCH  --invalidation--> INVALIDATED --> COOLDOWN (30 min)
    WATCH  --60 min without upgrade--> EXPIRED --> COOLDOWN (30 min)
    COOLDOWN -> NORMAL, and a new watch also needs a swing leg not used yet (one watch per symbol + side + leg)

Two lanes share the code. "main" (bars starting 09:45 .. ending 15:00) applies cooldown, chop lock and caps.
"opening" (bars starting 09:30 .. 09:45) is research only: opening_shadow_only = True, no caps, never actionable,
and it does not touch the main lane's legs, cooldowns or caps.

Order of work for one bar (fixed, so live, replay and restart agree):
  1. confirmed events for this symbol with ts < bar end are applied (upgrade price = last close before this bar)
  2. the symbol state takes the bar -> Features
  3. each lane's open watch: expiry, then invalidation
  4. each lane: new watch (bull first, then bear)
Bars are processed in (bar end, symbol order) order. ``process`` returns plain dict events; nothing here does I/O.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from zoneinfo import ZoneInfo

from ..indicators import Bar
from .rules import ONE, RULES, VERSION, Rules
from .state import Features, SymbolState, at

SIDES = ("bull", "bear")
SIGN = {"bull": 1, "bear": -1}


@dataclass
class Watch:
    watch_id: str
    day: str
    lane: str
    sym: str
    side: str
    leg_id: str
    created_at: int
    price: float
    vwap: float
    ema9: float
    ema21: float
    spread: float
    spread_ref: float
    atr: float | None
    structure_type: str            # "HL" / "LH" (developing) or "ORB"
    level: float                   # invalidation level: developing swing low / high, or the opening-range edge
    or_high: float | None
    or_low: float | None
    or_break: bool
    minutes_since_open: float
    opening_shadow_only: bool
    context: dict
    received_at: float | None = None
    state: str = "WATCH"
    end_at: int | None = None
    end_reason: str | None = None
    upgrade_source: str | None = None
    upgrade_at: int | None = None
    upgrade_price: float | None = None
    rules_version: str = VERSION


@dataclass
class LaneSym:
    active: Watch | None = None
    cooldown_until: int = 0
    chop_until: int = 0
    invalid_times: list[int] = field(default_factory=list)
    used_legs: set = field(default_factory=set)
    count: int = 0


def structure_now(f: Features, side: str) -> set[str]:
    out = set()
    if (f.hl if side == "bull" else f.lh) is not None:
        out.add("DEV")
    if f.or_done and f.or_high is not None and f.or_low is not None:
        if (side == "bull" and f.c > f.or_high) or (side == "bear" and f.c < f.or_low):
            out.add("ORB")
    return out


def one_close(f: Features, side: str) -> bool:
    """Criteria 1-3 at a single close (the 2-close persistence is checked by the caller)."""
    if None in (f.vwap, f.ema9, f.ema21, f.spread, f.spread_ref):
        return False
    if side == "bull":
        return f.c > f.vwap and f.ema9 >= f.ema21 and f.spread > f.spread_ref
    return f.c < f.vwap and f.ema9 <= f.ema21 and f.spread < f.spread_ref


def criteria(f: Features, p: Features | None, side: str) -> tuple[bool, str | None]:
    """Criteria 1-4 on two consecutive completed closes. Returns (ok, "DEV" | "ORB")."""
    if p is None or p.e >= f.e:            # p: the previous processed close of this symbol today
        return False, None
    if not (one_close(f, side) and one_close(p, side)):
        return False, None
    a, b = structure_now(f, side), structure_now(p, side)
    if "DEV" in a and "DEV" in b:
        ca, cb = (f.hl, p.hl) if side == "bull" else (f.lh, p.lh)
        if ca.anchor == cb.anchor:
            return True, "DEV"
    if "ORB" in a and "ORB" in b:
        return True, "ORB"
    return False, None


class Observer:
    """The whole universe for one trading day. Feed it bars in (end, symbol order) and confirmed events."""

    def __init__(self, symbols: list[str], day: date, tz: ZoneInfo, rules: Rules = RULES,
                 context=None, market: str = "SPY"):
        self.symbols = list(symbols)
        self.day, self.tz, self.r = day, tz, rules
        self.states = {s: SymbolState(s, tz, rules) for s in self.symbols}
        self.prev: dict[str, Features | None] = {s: None for s in self.symbols}
        self.lanes = {lane: {s: LaneSym() for s in self.symbols} for lane in ("main", "opening")}
        self.created_main: list[int] = []
        self.pending: list[dict] = []          # confirmed events not yet applied
        self.seen_confirms: set = set()
        self.suppressed: set = set()
        self.context = context                 # callable(sym, ts_ms) -> dict of extra context (options bias)
        self.market = market
        self.market_pct: list[tuple[int, float | None]] = []
        self.last_close: dict[str, float] = {}
        self.open_px: dict[str, float] = {}
        d = day
        self.t_open = at(d, rules.session_open, tz)
        self.t_main0 = at(d, rules.main_start, tz)
        self.t_main1 = at(d, rules.main_end, tz)
        self.t_close = at(d, rules.session_close, tz)

    # ---- setup ------------------------------------------------------------------------------------

    def warm(self, sym: str, sessions: list[tuple[date, list[Bar]]], daily_closes: list[float] | None) -> None:
        st = self.states[sym]
        st.warm(sessions, daily_closes)
        st.start_day(self.day, None)

    def add_confirm(self, ev: dict) -> bool:
        """ev: {sym, side, ts (epoch s), source}. Duplicates are ignored. Returns True if new."""
        key = (ev["sym"], ev["side"], round(float(ev["ts"]), 3), ev["source"])
        if key in self.seen_confirms or ev["sym"] not in self.states:
            return False
        self.seen_confirms.add(key)
        self.pending.append(dict(ev, ts_ms=int(float(ev["ts"]) * 1000)))
        self.pending.sort(key=lambda x: (x["ts_ms"], x["sym"], x["source"]))
        return True

    # ---- one bar ----------------------------------------------------------------------------------

    def process(self, sym: str, bar: Bar, gap: bool = False, received_at: float | None = None) -> list[dict]:
        out: list[dict] = []
        end = bar.t + ONE
        out += self._apply_confirms(sym, end)
        f = self.states[sym].on_minute(bar, gap)
        p = self.prev[sym]
        if sym not in self.open_px:
            self.open_px[sym] = bar.o
        for pv in f.pivots:
            out.append({"kind": "pivot", "day": str(self.day), "sym": sym, "pivot": pv.kind, "price": pv.price,
                        "bar_t": pv.bar_t, "confirmed_at": pv.confirmed_at, "label": pv.label})
        for name, c in f.dev_events:
            out.append({"kind": "dev", "day": str(self.day), "sym": sym, "event": name, "level": c.level,
                        "anchor_t": c.anchor.bar_t, "at": f.e})
        if sym == self.market:
            self.market_pct.append((f.e, f.day_pct))
        for lane in ("main", "opening"):
            out += self._lifecycle(lane, f, p)
        for lane in ("main", "opening"):
            out += self._create(lane, f, p, received_at)
        self.prev[sym] = f
        self.last_close[sym] = f.c
        return out

    def finalize(self) -> list[dict]:
        """Session over: apply the remaining confirmed events, expire what is still open."""
        out = []
        for sym in self.symbols:
            out += self._apply_confirms(sym, self.t_close + 3_600_000)
        for lane, syms in self.lanes.items():
            for sym, ls in syms.items():
                if ls.active is not None:
                    out.append(self._end(lane, ls, "EXPIRED", "session_end", self.t_close))
        return out

    # ---- lifecycle --------------------------------------------------------------------------------

    def _apply_confirms(self, sym: str, before_ms: int) -> list[dict]:
        out, keep = [], []
        for ev in self.pending:
            if ev["sym"] != sym or ev["ts_ms"] >= before_ms:
                keep.append(ev)
                continue
            for lane, syms in self.lanes.items():
                w = syms[sym].active
                if w and w.side == ev["side"] and w.created_at <= ev["ts_ms"] <= \
                        w.created_at + self.r.watch_minutes * ONE:
                    w.upgrade_source, w.upgrade_at = ev["source"], ev["ts_ms"]
                    w.upgrade_price = self.last_close.get(sym)
                    out.append(self._end(lane, syms[sym], "UPGRADED", ev["source"], ev["ts_ms"]))
        self.pending = keep
        return out

    def _lifecycle(self, lane: str, f: Features, p: Features | None) -> list[dict]:
        ls = self.lanes[lane][f.sym]
        w = ls.active
        if w is None:
            return []
        if f.e >= w.created_at + self.r.watch_minutes * ONE:
            return [self._end(lane, ls, "EXPIRED", "60_min_no_upgrade", f.e)]
        side, sg = w.side, SIGN[w.side]
        reason = None
        if sg * (f.c - w.level) < 0:
            reason = "close_beyond_level"
        elif p is not None and f.vwap is not None and p.vwap is not None and \
                sg * (f.c - f.vwap) < 0 and sg * (p.c - p.vwap) < 0:
            reason = "two_closes_beyond_vwap"
        elif f.closed5 and f.ema9_c is not None and f.ema21_c is not None and sg * (f.ema9_c - f.ema21_c) < 0:
            reason = "5m_ema_flip"
        elif criteria(f, p, "bear" if side == "bull" else "bull")[0]:
            reason = "opposite_criteria"
        if reason:
            return [self._end(lane, ls, "INVALIDATED", reason, f.e)]
        return []

    def _end(self, lane: str, ls: LaneSym, state: str, reason: str, at_ms: int) -> dict:
        w = ls.active
        w.state, w.end_at, w.end_reason = state, at_ms, reason
        ls.active = None
        if lane == "main" and state in ("INVALIDATED", "EXPIRED"):
            ls.cooldown_until = max(ls.cooldown_until, at_ms + self.r.cooldown_minutes * ONE)
        if lane == "main" and state == "INVALIDATED":
            ls.invalid_times = [t for t in ls.invalid_times if t > at_ms - self.r.chop_window_minutes * ONE]
            ls.invalid_times.append(at_ms)
            if len(ls.invalid_times) >= self.r.chop_invalidations:
                ls.chop_until = max(ls.chop_until, at_ms + self.r.chop_lock_minutes * ONE)
        return {"kind": "end", "watch_id": w.watch_id, "state": state, "end_at": at_ms, "end_reason": reason,
                "upgrade_source": w.upgrade_source, "upgrade_at": w.upgrade_at, "upgrade_price": w.upgrade_price,
                "lane": lane, "sym": w.sym, "cooldown_until": ls.cooldown_until, "chop_until": ls.chop_until}

    # ---- creation ---------------------------------------------------------------------------------

    def _create(self, lane: str, f: Features, p: Features | None, received_at: float | None) -> list[dict]:
        ls = self.lanes[lane][f.sym]
        if lane == "main":
            in_window = f.t >= self.t_main0 and f.e <= self.t_main1
            after = f.t >= self.t_main0 and f.e > self.t_main1
        else:
            in_window = self.t_open <= f.t < self.t_main0
            after = False
        if not (in_window or after):
            return []
        out = []
        for side in SIDES:
            ok, kind = criteria(f, p, side)
            if not ok:
                continue
            cand = f.hl if side == "bull" else f.lh
            if kind == "DEV":
                leg = f"P{cand.anchor.bar_t}"
                level = cand.level
                stype = "HL" if side == "bull" else "LH"
            else:
                leg = f"OR{self.day.isoformat()}"
                level = f.or_high if side == "bull" else f.or_low
                stype = "ORB"
            reason = self._blocked(lane, ls, f, p, side, leg, after)
            if reason:
                key = (lane, f.sym, side, leg, reason)
                if key not in self.suppressed:
                    self.suppressed.add(key)
                    out.append({"kind": "suppressed", "day": str(self.day), "lane": lane, "sym": f.sym,
                                "side": side, "leg_id": leg, "reason": reason, "at": f.e})
                continue
            w = self._new(lane, f, side, leg, level, stype, received_at)
            ls.active, ls.count = w, ls.count + 1
            ls.used_legs.add((side, leg))
            if lane == "main":
                self.created_main.append(f.e)
            out.append({"kind": "watch", **asdict(w)})
            break                                   # one watch per symbol per bar per lane
        return out

    def _blocked(self, lane, ls: LaneSym, f: Features, p: Features | None, side, leg, after) -> str | None:
        """The first binding reason, in this fixed order (pre-registered): after 15:00, stale, a watch already
        open, chop lock, cooldown, same leg, symbol cap, hourly cap."""
        if after:
            return "after_15_00"
        if f.stale or (p is not None and p.stale):
            return "stale"
        if ls.active is not None:
            return "active_watch"
        if lane == "main" and f.e < ls.chop_until:
            return "chop_lock"
        if lane == "main" and f.e < ls.cooldown_until:
            return "cooldown"
        if (side, leg) in ls.used_legs:
            return "same_leg"
        if lane == "opening":
            return None
        if ls.count >= self.r.max_per_symbol_day:
            return "symbol_cap"
        recent = [t for t in self.created_main if t > f.e - 60 * ONE]
        if len(recent) >= self.r.max_per_hour:
            return "hourly_cap"
        return None

    def _new(self, lane, f: Features, side, leg, level, stype, received_at) -> Watch:
        spy = next((pct for e, pct in reversed(self.market_pct) if e <= f.e), None)
        ctx = {"rel_vol": _r(f.rel_vol, 2), "sma200": _r(f.sma200, 2),
               "sma200_side": None if f.sma200 is None else ("above" if f.c > f.sma200 else "below"),
               "spy_pct": _r(spy, 3), "spy_dir": None if spy is None else ("UP" if spy > 0 else "DOWN" if spy < 0
                                                                            else "FLAT"),
               "rsi14_5m": _r(f.rsi14, 1), "macd_hist_5m": _r(f.macd_hist, 4), "day_pct": _r(f.day_pct, 3),
               "or_break": stype == "ORB" or (f.or_done and f.or_high is not None and
                                              ((side == "bull" and f.c > f.or_high) or
                                               (side == "bear" and f.c < f.or_low)))}
        if self.context:
            try:
                ctx.update(self.context(f.sym, f.e) or {})
            except Exception as exc:                 # context is informational: never break the observer
                ctx["context_error"] = str(exc)[:120]
        wid = f"{self.day.isoformat()}|{f.sym}|{side}|{lane}|{leg}"
        return Watch(watch_id=wid, day=self.day.isoformat(), lane=lane, sym=f.sym, side=side, leg_id=leg,
                     created_at=f.e, price=f.c, vwap=f.vwap, ema9=f.ema9, ema21=f.ema21, spread=f.spread,
                     spread_ref=f.spread_ref, atr=f.atr, structure_type=stype, level=level,
                     or_high=f.or_high, or_low=f.or_low, or_break=bool(ctx["or_break"]),
                     minutes_since_open=f.minutes_since_open, opening_shadow_only=lane == "opening",
                     context=ctx, received_at=received_at)

    # ---- persistence helpers ----------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Cooldown / chop / cap state (persisted for observability; restart rebuilds it by replay)."""
        return {lane: {s: {"active": ls.active.watch_id if ls.active else None,
                           "cooldown_until": ls.cooldown_until, "chop_until": ls.chop_until,
                           "invalid_times": ls.invalid_times, "count": ls.count,
                           "used_legs": sorted(f"{a}:{b}" for a, b in ls.used_legs)}
                       for s, ls in syms.items()} for lane, syms in self.lanes.items()} | \
            {"created_main": self.created_main, "last_e": {s: st.last_e for s, st in self.states.items()}}


def _r(x, n):
    return None if x is None else round(x, n)
