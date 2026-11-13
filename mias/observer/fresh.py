"""Research instrumentation (measurement only, added 2026-11-12): a FRESH same-direction setup that forms while an
older same-symbol / same-direction watch is still open (the NVDA 2026-11-12 case).

Nothing here changes observer state. The frozen rules still forbid a second watch; these records only say that the
new setup existed, what the old watch was, how long it still had to live, and whether the new setup would otherwise
have qualified:

  v1  the criteria held on 2 closes on a NEW leg and the only binding reason was the open watch. "Would otherwise
      qualify" = the frozen suppression check (Observer._blocked, a pure read) finds no other reason once the open
      watch is ignored. Exact, no simulation needed.
  v2  a NEW candidate was blocked with status open_watch_exists. Its persistence closes are then followed by a
      shadow copy of the frozen pending check (V2Observer._pendings run on a private shadow, never on the real
      observer), so "would otherwise qualify" = the shadow candidate reached 'watched'.

Outcomes (5/10/15/30/60 min) are scored from bars by the report, from the new candidate's time and price.
"""
from __future__ import annotations

from dataclasses import replace

from .rules import ONE
from .v2 import NEED, WATCH_MINUTES, Pending, V2Observer

KIND = "fresh_while_open"
UPDATE = "fresh_update"


class V1Fresh:
    def __init__(self, ob):
        self.ob = ob

    def after(self, sym: str, p, events: list[dict]) -> list[dict]:
        """Call after ob.process(sym, ...) with p = ob.prev[sym] as it was BEFORE that call."""
        ob, out = self.ob, []
        f = ob.prev.get(sym)
        for ev in events:
            if ev.get("kind") != "suppressed" or ev.get("reason") != "active_watch" or f is None:
                continue
            lane = ev["lane"]
            ls = ob.lanes[lane][sym]
            old = ls.active
            if old is None or old.side != ev["side"] or old.leg_id == ev["leg_id"]:
                continue                                   # opposite side, or the same leg: not a fresh setup
            after = lane == "main" and f.t >= ob.t_main0 and f.e > ob.t_main1
            other = ob._blocked(lane, replace(ls, active=None), f, p, ev["side"], ev["leg_id"], after)
            out.append({"kind": KIND, "fresh_id": f"{ob.day}|v1|{lane}|{sym}|{ev['side']}|{ev['leg_id']}",
                        "day": str(ob.day), "version": "v1", "lane": lane, "family": "v1",
                        "setup": "ORB" if ev["leg_id"].startswith("OR") else ("HL" if ev["side"] == "bull" else "LH"),
                        "sym": sym, "side": ev["side"], "leg_id": ev["leg_id"], "cand_at": f.e, "cand_price": f.c,
                        "old_watch_id": old.watch_id, "old_created_at": old.created_at, "old_leg_id": old.leg_id,
                        "remaining_min": (old.created_at + ob.r.watch_minutes * ONE - f.e) / ONE,
                        "would_qualify": int(other is None), "qualify_detail": other or "no other blocking reason"})
        return out


class _Shadow:
    """Private stand-in for V2Observer's pending table: the frozen pending check runs on THIS, never on the real
    observer, so it cannot create, end or block anything real."""

    def __init__(self):
        self.pending: dict[tuple, Pending] = {}
        self.by_cand: dict[str, str] = {}          # cand_id -> fresh_id

    _cand_end = V2Observer._cand_end

    def _watch(self, key, p: Pending, f) -> dict:
        return {"kind": "shadow_watched", "fresh_id": p.cand.get("fresh_id"), "at": f.e}


class V2Fresh:
    def __init__(self, v2: V2Observer):
        self.v2 = v2
        self.sh = _Shadow()

    def after(self, sym: str, events: list[dict]) -> list[dict]:
        """Call after v2.process(sym, ...)."""
        v2, out = self.v2, []
        hist = v2.sym[sym].hist
        if not hist:
            return out
        f = hist[-1]
        # 1) earlier fresh candidates: the frozen persistence check, on the shadow only
        for ev in V2Observer._pendings(self.sh, sym, f):
            if ev["kind"] == "v2_candidate_end":
                fid = self.sh.by_cand.pop(ev["cand_id"], None)
                if fid and ev["status"] != "watched":
                    out.append({"kind": UPDATE, "fresh_id": fid, "would_qualify": 0,
                                "qualify_detail": f"persistence: {ev['status']}", "at": ev["end_at"]})
            elif ev["kind"] == "shadow_watched":
                fid = ev["fresh_id"]
                if fid:
                    out.append({"kind": UPDATE, "fresh_id": fid, "would_qualify": 1,
                                "qualify_detail": "persistence: watched", "at": ev["at"]})
        # 2) new candidates blocked only because a same-direction watch of the family is open
        for ev in events:
            if ev.get("kind") != "v2_candidate" or ev.get("status") != "open_watch_exists":
                continue
            fam, side = ev["family"], ev["side"]
            key = (fam, sym, side)
            old = v2.open.get(key)
            if old is None or old["leg_id"] == ev["leg_id"]:
                continue
            fid = f"{v2.day}|v2|{fam}|{sym}|{side}|{ev['leg_id']}|{ev['candidate_at']}"
            rec = {"kind": KIND, "fresh_id": fid, "day": str(v2.day), "version": "v2", "lane": None, "family": fam,
                   "setup": fam, "sym": sym, "side": side, "leg_id": ev["leg_id"], "cand_at": ev["candidate_at"],
                   "cand_price": ev["candidate_price"], "old_watch_id": old["watch_id"],
                   "old_created_at": old["watch_at"], "old_leg_id": old["leg_id"],
                   "remaining_min": (old["watch_at"] + WATCH_MINUTES * ONE - ev["candidate_at"]) / ONE,
                   "would_qualify": None, "qualify_detail": "persistence pending"}
            if key in v2.pending:
                rec.update(would_qualify=0, qualify_detail="a pending candidate of this family already exists")
            else:
                anchor = level = None
                if fam == "structure":
                    c = f.hl if side == "bull" else f.lh
                    anchor, level = (c.anchor.bar_t, c.level) if c is not None else (None, None)
                elif fam == "or":
                    level = f.or_high if side == "bull" else f.or_low
                if fam == "structure" and anchor is None:
                    rec.update(would_qualify=0, qualify_detail="no developing level at the candidate")
                else:
                    if key in self.sh.pending:               # an older fresh candidate on this key is superseded
                        old_p = self.sh.pending[key]
                        self.sh.by_cand.pop(old_p.cand["cand_id"], None)
                        out.append({"kind": UPDATE, "fresh_id": old_p.cand["fresh_id"], "would_qualify": None,
                                    "qualify_detail": "superseded by a newer fresh candidate", "at": f.e})
                    self.sh.pending[key] = Pending(dict(ev, fresh_id=fid), NEED[fam], anchor, level)
                    self.sh.by_cand[ev["cand_id"]] = fid
            out.append(rec)
        return out
