"""Options context attached to an Early Watch (added 2026-11-15). CONTEXT ONLY.

It never creates, delays, suppresses, upgrades, downgrades or invalidates a watch, and it is not a gate anywhere.
The frozen v1 / v2 detectors already call an injected ``context(sym, ts_ms)`` when a watch is created and store the
result in the watch's ``context`` column (their own try/except keeps any failure informational). The live service
passes an ``OptionsContext`` there instead of the old one-field lookup; replays keep the old lookup.

Source: the MIAS cycle ``metrics`` rows in data/mias.db, read-only, on one persistent connection with a short
busy timeout (WAL, so readers do not wait on the writer). No Massive request is made. Each MIAS cycle (every 150 s
while the market is open) fetches the option chain for expirations 0-100 days out and strikes within +/-15 % of
spot (config ``chain_max_dte`` / ``chain_strike_pct``; SPX / NDX use narrower bands) and writes, per symbol:

* ``pcr_volume`` = today's put volume / call volume over that chain scope, SAME-DAY (0DTE) EXPIRATIONS INCLUDED
* ``pcr_oi``     = put open interest / call open interest over the same scope (OI is the previous close's figure)
* ``window``     = volume added since the previous cycle per contract (calls / puts; plus the 0DTE part from
                   2026-11-16), and the flow it was classified as (trade price against the quote)
* ``options_components`` = the production options-bias parts: session_flow, recent_flow (last 6 cycles = 15 min),
                   put_call, skew (25-delta risk reversal change since the open), each in -1..+1

Rolling put/call (research): the sum of the per-cycle windows inside (t - N min, t]; "previous" is the window
before that, (t - 2N, t - N]. Exact for what the windows contain: contracts in BOTH consecutive snapshots whose
volume rose, so a contract entering the strike band is missed for one cycle. A rolling value needs >= 80 % of the
window covered by cycles and >= ROLL_MIN_CONTRACTS contracts, otherwise it is left empty.

Interpretation (flow first; PCR and skew can only support or contradict it, never override it):
  flow   = 0.6 x recent_flow + 0.4 x session_flow  -> bullish >= +0.20, bearish <= -0.20, else neutral
  PCR    = direction of the rolling 15-min ratio (non-0DTE when recorded, else all expirations) vs the previous
           15 min: rising (x >= 1.10) supports bearish, falling (<= 1/1.10) supports bullish, else flat
  skew   = skew component: >= +0.20 bullish, <= -0.20 bearish, else neutral
  view   = INSUFFICIENT_DATA | NEUTRAL (flow neutral) | BULLISH_ / BEARISH_CONFIRMATION (flow direction, unless
           PCR AND skew both point the other way -> CONFLICT)
Divergence (research, weak context, never a reversal call): the two latest confirmed 5-minute swing highs (lows)
of the day from the detector's own pivot list, the newer confirmed within the last 60 min. Higher high while the
rolling 15-min PCR at the newer high is <= 0.95 x the PCR at the older one -> POTENTIAL_BEARISH_DIVERGENCE. Lower
low while the PCR at the newer low is <= 0.95 x the older -> POTENTIAL_BULLISH_DIVERGENCE.
Relative to the watch side: CONFIRMING | NEUTRAL | CONFLICTING | INSUFFICIENT_DATA (the message headline);
``class`` (report grouping) is the divergence (BULLISH_DIVERGENCE / BEARISH_DIVERGENCE) when one is present, else
the relation. The message shows a divergence on its own line, so it never hides a flow conflict.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "options-context-v1 (2026-11-15)"
CYCLE_S = 150
FRESH_S = 2 * CYCLE_S + 30           # newest snapshot older than this at watch time -> STALE
ROLL_MIN_CONTRACTS = 1000            # contracts (calls + puts) in a rolling window
ROLL_COVER = 0.8                     # share of the window covered by cycle windows
MIN_QUOTE_COVERAGE = 0.8             # share of contracts with a valid bid / ask (flow side classification)
MAX_TOP_SHARE = 0.25                 # one contract carrying more of today's volume -> concentrated
FLOW_T = 0.20
PCR_STEP = 1.10
DIV_RATIO = 0.95
DIV_RECENT_MS = 60 * 60_000
FIVE_MS = 5 * 60_000
INDEX = ("SPY", "QQQ", "IWM")
MARKET = ("SPY", "QQQ")

BULL_C, BEAR_C, CONFLICT, NEUTRAL, INSUFF = ("BULLISH_CONFIRMATION", "BEARISH_CONFIRMATION", "CONFLICT", "NEUTRAL",
                                             "INSUFFICIENT_DATA")
BULL_DIV, BEAR_DIV = "POTENTIAL_BULLISH_DIVERGENCE", "POTENTIAL_BEARISH_DIVERGENCE"
CONFIRMING, CONFLICTING = "CONFIRMING", "CONFLICTING"


def _dir(x, t=FLOW_T):
    if x is None:
        return None
    return "bullish" if x >= t else "bearish" if x <= -t else "neutral"


def _r(x, n=3):
    return None if x is None else round(x, n)


class Sample:
    __slots__ = ("ts", "opt", "comps", "label", "score", "stock_label", "w_ts", "wc", "wp", "wc0", "wp0", "has0")

    def __init__(self, ts: float, d: dict):
        self.ts = ts
        o = d.get("options") or None
        self.opt = o
        self.comps = d.get("options_components") or {}
        self.label, self.score, self.stock_label = d.get("options_label"), d.get("options_score"), d.get("stock_label")
        w = (o or {}).get("window") or None
        self.w_ts = w["ts"] if w else None
        self.wc, self.wp = (w["call_volume"], w["put_volume"]) if w else (None, None)
        self.has0 = bool(w) and "call_volume_0dte" in w
        self.wc0, self.wp0 = (w.get("call_volume_0dte", 0.0), w.get("put_volume_0dte", 0.0)) if w else (0.0, 0.0)


def view_of(flow_dir, pcr_dir, skew_dir) -> str:
    if flow_dir is None:
        return INSUFF
    if flow_dir == "neutral":
        return NEUTRAL
    other = "bearish" if flow_dir == "bullish" else "bullish"
    pcr_side = {"rising": "bearish", "falling": "bullish"}.get(pcr_dir)
    if pcr_side == other and skew_dir == other:
        return CONFLICT
    return BULL_C if flow_dir == "bullish" else BEAR_C


def relation(view: str | None, side: str) -> str:
    if view in (None, INSUFF):
        return INSUFF
    if view in (BULL_C, BEAR_C):
        return CONFIRMING if (view == BULL_C) == (side == "bull") else CONFLICTING
    return NEUTRAL


def overall(stock_rel: str, market_rel: str | None) -> str:
    """Stock vs market options context relative to the watch: both agree, MIXED if they disagree."""
    if market_rel in (None, INSUFF):
        return stock_rel
    if stock_rel == INSUFF:
        return market_rel
    pair = {stock_rel, market_rel}
    if pair == {CONFIRMING, CONFLICTING}:
        return "MIXED"
    if CONFIRMING in pair:
        return CONFIRMING
    if CONFLICTING in pair:
        return CONFLICTING
    return NEUTRAL


class OptionsContext:
    """callable(sym, ts_ms) -> {"options_bias", "options_score", "options_at", "optx": {...}}. Pure reads."""

    def __init__(self, db_path: Path, tz: ZoneInfo, *, pivots=None, source: str = "live snapshot",
                 clock=time.time, busy_ms: int = 200):
        self.db_path, self.tz, self.source, self.clock = Path(db_path), tz, source, clock
        self.pivots = pivots                 # callable(sym) -> list of structure.Pivot (read-only), or None
        self.busy_ms = busy_ms
        self._db: sqlite3.Connection | None = None
        self._cache: dict[tuple[str, str], list[Sample]] = {}
        self._last: dict[tuple[str, str], float] = {}

    # ---- data -------------------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False,
                                       timeout=self.busy_ms / 1000)
            self._db.execute(f"PRAGMA busy_timeout={int(self.busy_ms)}")
        return self._db

    def samples(self, sym: str, ts: float) -> list[Sample]:
        """Today's samples with ts <= the requested moment (incremental cache; newer rows are fetched once)."""
        day = datetime.fromtimestamp(ts, self.tz).date()
        key = (sym, day.isoformat())
        day0 = datetime.combine(day, datetime.min.time(), self.tz).timestamp()
        have = self._cache.setdefault(key, [])
        last = self._last.get(key, day0 - 1)
        if not have or have[-1].ts < ts:
            rows = self._conn().execute("SELECT ts, data FROM metrics WHERE symbol = ? AND ts > ? AND ts >= ? "
                                        "ORDER BY ts", (sym, last, day0)).fetchall()
            for t, d in rows:
                have.append(Sample(t, json.loads(d)))
            if rows:
                self._last[key] = rows[-1][0]
        if len(self._cache) > 64:                       # keep only today's keys
            for k in [k for k in self._cache if k[1] != key[1]]:
                self._cache.pop(k, None)
                self._last.pop(k, None)
        return have[:bisect_right([s.ts for s in have], ts)]

    # ---- maths ------------------------------------------------------------------------------------

    @staticmethod
    def rolling(ss: list[Sample], end: float, minutes: int, ex0: bool = False) -> dict | None:
        """Put/call over cycle windows with window ts in (end - N, end]. None if not enough coverage / volume."""
        lo = end - minutes * 60
        ws, covered, prev_ts = [], 0.0, None
        for s in ss:
            if s.w_ts is not None and lo < s.w_ts <= end:
                start = prev_ts if prev_ts is not None and s.w_ts - prev_ts <= 2 * CYCLE_S else s.w_ts - CYCLE_S
                covered += s.w_ts - max(start, lo)
                ws.append(s)
            prev_ts = s.ts
        if not ws or (ex0 and not all(s.has0 for s in ws)):
            return None
        c = sum(s.wc - (s.wc0 if ex0 else 0) for s in ws)
        p = sum(s.wp - (s.wp0 if ex0 else 0) for s in ws)
        if covered < ROLL_COVER * minutes * 60 or c <= 0 or c + p < ROLL_MIN_CONTRACTS:
            return None
        return {"pcr": round(p / c, 3), "calls": round(c), "puts": round(p), "cycles": len(ws)}

    def roll_pair(self, ss, end, minutes, ex0=False) -> dict:
        cur = self.rolling(ss, end, minutes, ex0)
        prev = self.rolling(ss, end - minutes * 60, minutes, ex0)
        d = None
        if cur and prev and prev["pcr"] > 0 and cur["pcr"] > 0:
            ratio = cur["pcr"] / prev["pcr"]
            d = "rising" if ratio >= PCR_STEP else "falling" if ratio <= 1 / PCR_STEP else "flat"
        return {"cur": cur and cur["pcr"], "prev": prev and prev["pcr"],
                "slope": None if not (cur and prev) else round(cur["pcr"] - prev["pcr"], 3), "dir": d,
                "contracts": cur and cur["calls"] + cur["puts"]}

    def stock(self, sym: str, ts: float) -> dict:
        """The side-independent options read for one symbol at ts (no divergence)."""
        ss = self.samples(sym, ts)
        s = ss[-1] if ss else None
        if s is None:
            return {"status": "unavailable", "view": INSUFF, "reasons": ["no MIAS options snapshot today"]}
        o = s.opt or {}
        age = ts - s.ts
        reasons = []
        if not o:
            reasons.append("no options data in the latest cycle")
        if age > FRESH_S:
            reasons.append(f"snapshot {age:.0f}s old")
        if o.get("thin"):
            reasons.append("thin options (day volume below thin_options_volume)")
        if o and (o.get("quote_coverage") or 0) < MIN_QUOTE_COVERAGE:
            reasons.append(f"quote coverage {o.get('quote_coverage')}")
        if (o.get("top_volume_share") or 0) > MAX_TOP_SHARE:
            reasons.append(f"one contract is {100 * o['top_volume_share']:.0f}% of today's volume")
        rf, sf, sk = s.comps.get("recent_flow"), s.comps.get("session_flow"), s.comps.get("skew")
        if rf is None and sf is None:
            reasons.append("no flow yet")
        flow = (0.6 * rf + 0.4 * sf) if rf is not None and sf is not None else (rf if rf is not None else sf)
        has0 = s.has0
        r15 = self.roll_pair(ss, s.ts, 15)
        r30 = self.roll_pair(ss, s.ts, 30)
        r15x = self.roll_pair(ss, s.ts, 15, ex0=True) if has0 else None
        r30x = self.roll_pair(ss, s.ts, 30, ex0=True) if has0 else None
        basis = "non-0DTE 15m" if r15x and r15x["dir"] else "all expirations 15m"
        pcr_dir = (r15x if r15x and r15x["dir"] else r15)["dir"]
        cv, pv = o.get("call_volume"), o.get("put_volume")
        c0, p0 = o.get("call_volume_0dte"), o.get("put_volume_0dte")
        coi, poi = o.get("call_oi"), o.get("put_oi")
        coi0, poi0 = o.get("call_oi_0dte"), o.get("put_oi_0dte")
        ratio = lambda a, b: round(a / b, 3) if a is not None and b else None
        stale = age > FRESH_S
        fdir, kdir = _dir(flow), _dir(sk)
        view = INSUFF if reasons else view_of(fdir, pcr_dir, kdir)
        return {
            "status": "stale" if stale else ("insufficient" if reasons else "ok"),
            "view": view, "reasons": reasons,
            "snapshot_ts": round(s.ts, 1), "age_s": round(age, 1), "fresh": not stale,
            "pcr_sample_ts": round(s.ts, 1), "flow_sample_ts": s.w_ts and round(s.w_ts, 1),
            "flow": _r(flow), "flow_dir": fdir, "recent_flow": rf, "recent_dir": _dir(rf),
            "session_flow": sf, "session_dir": _dir(sf), "skew": sk, "skew_dir": kdir,
            "pcr_dir": pcr_dir, "pcr_basis": basis,
            "pcr_volume": o.get("pcr_volume"), "pcr_oi": o.get("pcr_oi"),
            "pcr_volume_0dte": ratio(p0, c0) if c0 is not None else None,
            "pcr_volume_ex0": ratio(None if pv is None or p0 is None else pv - p0,
                                    None if cv is None or c0 is None else cv - c0) if c0 is not None else None,
            "pcr_oi_0dte": ratio(poi0, coi0) if coi0 is not None else None,
            "pcr_oi_ex0": ratio(None if poi is None or poi0 is None else poi - poi0,
                                None if coi is None or coi0 is None else coi - coi0) if coi0 is not None else None,
            "has_0dte_split": c0 is not None,
            "roll15": r15, "roll30": r30, "roll15_ex0": r15x, "roll30_ex0": r30x,
            "day_volume": None if cv is None else round(cv + pv), "quote_coverage": o.get("quote_coverage"),
            "top_volume_share": o.get("top_volume_share"), "thin": o.get("thin"),
            "bias_label": s.label, "bias_score": s.score, "stock_trend": s.stock_label,
        }

    def divergence(self, sym: str, ts: float, t_ms: int) -> dict:
        if self.pivots is None:
            return {"divergence": None, "div_detail": "no pivot source"}
        try:
            pv = list(self.pivots(sym) or [])
        except Exception as exc:
            return {"divergence": None, "div_detail": f"pivots unavailable: {exc}"[:80]}
        day0 = datetime.combine(datetime.fromtimestamp(ts, self.tz).date(), datetime.min.time(), self.tz)
        d0 = int(day0.timestamp() * 1000)
        ss = self.samples(sym, ts)
        found, details = [], []
        for kind, want, tag in (("H", "higher", BEAR_DIV), ("L", "lower", BULL_DIV)):
            ps = [p for p in pv if p.kind == kind and d0 <= p.bar_t and p.confirmed_at <= t_ms]
            if len(ps) < 2 or t_ms - ps[-1].confirmed_at > DIV_RECENT_MS:
                continue
            a, b = ps[-2], ps[-1]
            if not ((b.price > a.price) if kind == "H" else (b.price < a.price)):
                continue
            pa = self.rolling(ss, (a.bar_t + FIVE_MS) / 1000, 15)
            pb = self.rolling(ss, (b.bar_t + FIVE_MS) / 1000, 15)
            if not (pa and pb):
                details.append(f"{want} {'high' if kind == 'H' else 'low'}: PCR n/a at a pivot")
                continue
            det = f"{want} {'high' if kind == 'H' else 'low'} {a.price:.2f}→{b.price:.2f}, PCR15 {pa['pcr']}→{pb['pcr']}"
            details.append(det)
            if pb["pcr"] <= DIV_RATIO * pa["pcr"]:
                found.append((b.confirmed_at, tag))
        div = max(found)[1] if found else None
        return {"divergence": div, "div_detail": "; ".join(details) or None}

    # ---- the hook ---------------------------------------------------------------------------------

    def __call__(self, sym: str, ts_ms: int) -> dict:
        t0 = time.perf_counter()
        ts = ts_ms / 1000
        try:
            st = self.stock(sym, ts)
            if st["status"] in ("ok",):
                st.update(self.divergence(sym, ts, ts_ms))
            else:
                st.update({"divergence": None, "div_detail": None})
            market = {}
            if sym not in INDEX:
                for m in MARKET:
                    mk = self.stock(m, ts)
                    market[m] = {k: mk.get(k) for k in ("status", "view", "flow_dir", "pcr_dir", "skew_dir", "age_s",
                                                        "pcr_volume", "stock_trend")}
            mview = market_view([market[m]["view"] for m in MARKET]) if market else None
            div = st.get("divergence")
            rel, cls, ovr = {}, {}, {}
            for side in ("bull", "bear"):
                rel[side] = relation(st["view"], side)
                cls[side] = rel[side] if rel[side] == INSUFF or not div else div.replace("POTENTIAL_", "")
                ovr[side] = overall(rel[side], relation(mview, side) if mview else None) if mview else rel[side]
            optx = {"version": VERSION, "source": self.source, **st, "market": market or None,
                    "market_view": mview, "relation": rel, "class": cls, "overall": ovr,
                    "computed_at": round(self.clock(), 3)}
        except Exception as exc:                       # context only: never let it reach the detector
            optx = {"version": VERSION, "source": self.source, "status": "unavailable", "view": INSUFF,
                    "error": f"{type(exc).__name__}: {exc}"[:160],
                    "relation": {"bull": INSUFF, "bear": INSUFF}, "class": {"bull": INSUFF, "bear": INSUFF},
                    "overall": {"bull": INSUFF, "bear": INSUFF}, "computed_at": round(self.clock(), 3)}
        optx["compute_ms"] = round(1000 * (time.perf_counter() - t0), 2)
        return {"options_bias": optx.get("bias_label"), "options_score": optx.get("bias_score"),
                "options_at": None if optx.get("snapshot_ts") is None else round(optx["snapshot_ts"]), "optx": optx}

    def sibling(self, pivots=None) -> "OptionsContext":
        """Same connection and cache, a different pivot source (v1 and v2 each pass their own)."""
        o = OptionsContext(self.db_path, self.tz, pivots=pivots, source=self.source, clock=self.clock,
                           busy_ms=self.busy_ms)
        o._db, o._cache, o._last, o._shared = self._db, self._cache, self._last, True
        o._conn = self._conn
        return o

    def warm(self, syms, ts: float) -> None:
        """Load new metrics rows outside the watch path (the service calls this after a bar is processed), so
        the watch-time call finds the day already cached."""
        for sym in syms:
            self.samples(sym, ts)

    def close(self) -> None:
        if getattr(self, "_shared", False):
            return
        if self._db is not None:
            self._db.close()
            self._db = None


def market_view(views: list[str]) -> str:
    """SPY + QQQ: agree -> that view; one directional and the other neutral / insufficient -> that one;
    opposite directions -> CONFLICT; nothing usable -> INSUFFICIENT_DATA."""
    use = [v for v in views if v not in (None, INSUFF)]
    if not use:
        return INSUFF
    dirs = {v for v in use if v in (BULL_C, BEAR_C)}
    if len(dirs) == 2:
        return CONFLICT
    if dirs:
        return dirs.pop()
    return CONFLICT if CONFLICT in use else NEUTRAL


# ---- Telegram lines (compact) -------------------------------------------------------------------------

_HEAD = {CONFIRMING: "✓ Options context: CONFIRMING", CONFLICTING: "⚠️ Options context: CONFLICT",
         NEUTRAL: "○ Options context: NEUTRAL"}
_VIEW_WORD = {BULL_C: "BULLISH", BEAR_C: "BEARISH", NEUTRAL: "NEUTRAL", CONFLICT: "MIXED", INSUFF: "n/a"}


def options_lines(ctx: dict | None, side: str) -> list[str]:
    """What the message shows, derived only from the persisted context (so stored == displayed)."""
    x = (ctx or {}).get("optx")
    if not x or x.get("status") == "unavailable":
        return ["○ Options context: unavailable"]
    if x.get("status") == "stale":
        return ["○ Options context: STALE / INSUFFICIENT DATA"]
    if x.get("status") == "insufficient":
        return ["○ Options context: INSUFFICIENT DATA"]
    lines = [_HEAD.get(x["relation"][side], "○ Options context: NEUTRAL")]
    parts = []
    if x.get("flow_dir"):
        parts.append(f"Flow: {x['flow_dir']}")
    if x.get("pcr_dir"):
        parts.append(f"PCR: {x['pcr_dir']}")
    if x.get("skew_dir"):
        parts.append(f"Skew: {x['skew_dir']}")
    if parts:
        lines.append("   " + " | ".join(parts))
    if x.get("market_view") in (BULL_C, BEAR_C, CONFLICT):
        lines.append(f"   Stock options: {_VIEW_WORD.get(x['view'], 'n/a')} | Market options: "
                     f"{_VIEW_WORD.get(x['market_view'], 'n/a')} | Overall: {x['overall'][side]}")
    if x.get("divergence"):
        lines.append(f"   PCR divergence: potential {'bearish' if x['divergence'] == BEAR_DIV else 'bullish'} "
                     "(weak context, not a reversal signal)")
    return lines
