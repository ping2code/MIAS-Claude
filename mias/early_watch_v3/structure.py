"""V3 copy of mias/observer/structure.py (frozen V2-A engine; V3 owns this copy; V1 / V2 never import it).

Market structure without look-ahead.

CONFIRMED pivots (completed 5-minute bars, k = 2): bar i is a swing high when its high is strictly above the highs
of the k bars before and the k bars after it (a swing low mirrors this with lows). That is only knowable once bar
i + k has completed, so every pivot carries both ``bar_t`` (when the extreme happened) and ``confirmed_at`` (the
close of bar i + k, the first moment MIAS can know it). Nothing before ``confirmed_at`` ever sees the pivot.
Each new pivot is labelled against the previous pivot of the same kind: HH / LH for highs, HL / LL for lows.

DEVELOPING structure (completed 1-minute bars):
  bullish  after the latest confirmed swing high H, track the lowest 1-minute low since H's bar (``dev_low``).
           A developing Higher Low exists when dev_low is above the swing low that preceded H and a completed
           1-minute close is >= dev_atr_mult x ATR above dev_low. Its level is frozen when it forms; a later
           1-minute close below the level fails it.
  bearish  mirror: after the latest swing low L, the highest high since L (``dev_high``) below the swing high that
           preceded L, with a close >= dev_atr_mult x ATR below it -> developing Lower High.
No ZigZag, nothing is ever rewritten: pivots are append-only and candidates only form or fail going forward.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..indicators import Bar
from .config import FIVE, DetectRules as Rules

RULES = Rules()


@dataclass(frozen=True)
class Pivot:
    kind: str            # "H" or "L"
    price: float
    bar_t: int           # start of the 5-minute pivot bar (epoch ms)
    confirmed_at: int    # close of the k-th following bar: when it became knowable
    label: str | None    # HH / LH / EH for highs, HL / LL / EL for lows (vs the previous pivot of the same kind)


@dataclass(frozen=True)
class Candidate:
    side: str            # "bull" (developing HL) or "bear" (developing LH)
    level: float         # frozen dev_low / dev_high at formation
    anchor: Pivot        # the swing high (bull) / swing low (bear) this leg started from: the leg id
    ref: Pivot           # the swing low (bull) / high (bear) it must stay above / below
    formed_at: int       # end of the 1-minute bar that formed it


class Structure:
    def __init__(self, rules: Rules = RULES):
        self.k = rules.pivot_k
        self.mult = rules.dev_atr_mult
        self.bars5: list[Bar] = []
        self.bars1: list[Bar] = []
        self.pivots: list[Pivot] = []
        self.cand: dict[str, Candidate | None] = {"bull": None, "bear": None}
        self._ext: dict[str, tuple[int, float | None]] = {}   # side -> (anchor bar_t, running extreme)

    # ---- confirmed pivots -------------------------------------------------------------------------

    def on_five(self, bar: Bar) -> list[Pivot]:
        """A completed 5-minute bar. Returns pivots that became known at this bar's close."""
        self.bars5.append(bar)
        k, n = self.k, len(self.bars5)
        i = n - 1 - k
        if i - k < 0:
            return []
        win = self.bars5[i - k:i + k + 1]
        b = self.bars5[i]
        others = [x for j, x in enumerate(win) if j != k]
        found = []
        confirmed_at = bar.t + FIVE
        if all(b.h > x.h for x in others):
            found.append(self._add("H", b.h, b.t, confirmed_at))
        if all(b.l < x.l for x in others):
            found.append(self._add("L", b.l, b.t, confirmed_at))
        return found

    def _add(self, kind: str, price: float, bar_t: int, confirmed_at: int) -> Pivot:
        prev = self.last(kind)
        label = None
        if prev:
            up, down = ("HH", "LH") if kind == "H" else ("HL", "LL")
            label = up if price > prev.price else down if price < prev.price else ("EH" if kind == "H" else "EL")
        p = Pivot(kind, price, bar_t, confirmed_at, label)
        self.pivots.append(p)
        side = "bull" if kind == "H" else "bear"          # a new swing high starts a new bullish pullback leg
        self.cand[side] = None
        self._ext.pop(side, None)
        return p

    def last(self, kind: str, before_t: int | None = None) -> Pivot | None:
        for p in reversed(self.pivots):
            if p.kind == kind and (before_t is None or p.bar_t < before_t):
                return p
        return None

    # ---- developing structure ---------------------------------------------------------------------

    def on_one(self, bar: Bar, atr: float | None) -> list[tuple[str, Candidate]]:
        """A completed 1-minute bar. Returns ("DEV_HL" | "DEV_LH" | "DEV_HL_FAIL" | "DEV_LH_FAIL", candidate)."""
        self.bars1.append(bar)
        out = []
        for side, kind, ref_kind in (("bull", "H", "L"), ("bear", "L", "H")):
            anchor = self.last(kind)
            if anchor is None:
                continue
            start = anchor.bar_t + FIVE
            ext = self._extreme(side, anchor, start, bar)
            name = "HL" if side == "bull" else "LH"
            c = self.cand[side]
            sg = 1 if side == "bull" else -1
            if c is not None and sg * (bar.c - c.level) < 0:
                out.append((f"DEV_{name}_FAIL", c))
                self.cand[side] = c = None
            ref = self.last(ref_kind, before_t=anchor.bar_t)
            if c is None and ref is not None and ext is not None and atr:
                beyond_ref = ext > ref.price if side == "bull" else ext < ref.price
                if beyond_ref and sg * (bar.c - ext) >= self.mult * atr:
                    c = Candidate(side, ext, anchor, ref, bar.t + 60_000)
                    self.cand[side] = c
                    out.append((f"DEV_{name}", c))
        return out

    def _extreme(self, side: str, anchor: Pivot, start: int, bar: Bar) -> float | None:
        pick = min if side == "bull" else max
        val = (lambda b: b.l) if side == "bull" else (lambda b: b.h)
        cur = self._ext.get(side)
        if cur is None or cur[0] != anchor.bar_t:          # new leg: the extreme since the anchor bar, from history
            xs = [val(b) for b in self.bars1 if b.t >= start]
            ext = pick(xs) if xs else None
        else:
            ext = cur[1]
            if bar.t >= start:
                ext = val(bar) if ext is None else pick(ext, val(bar))
        self._ext[side] = (anchor.bar_t, ext)
        return ext

    def trim(self, keep_from_t: int) -> None:
        """Drop 1-minute history before keep_from_t (day roll); pivots and 5-minute bars are kept."""
        self.bars1 = [b for b in self.bars1 if b.t >= keep_from_t]
