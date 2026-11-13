"""2026-11-12 delivery + measurement change: no MIAS hourly Telegram cap (v1 never had one at delivery, v2's is
removed), stale / duplicate protection kept, Telegram 429 handling (retry_after respected, bounded attempts, pacing),
detection vs delivery stages, short-horizon scoring, and the fresh-setup instrumentation that must never change
observer state."""
import asyncio
from dataclasses import asdict

import httpx

from mias.indicators import Bar
from mias.observer import delivery as DL
from mias.observer.fresh import V1Fresh, V2Fresh
from mias.observer.log import ObserverDB
from mias.observer.report import Bars
from mias.observer.rules import ONE
from mias.observer.telegram import SendError, WatchSender
from mias.observer.v2 import V2Observer, V2Telegram
from observer_synth import ET
from test_observer_telegram import T, Sender, drive, runner, watch
from test_observer_v2 import _w
from test_observer_watch import DAY, H1, H2, Feed, Observer, feat, hl, kinds, ms


# ---- 1. no hourly cap at delivery -------------------------------------------------------------------------

def test_v1_every_fresh_main_watch_is_sent_however_many_in_an_hour(tmp_path):
    snd = Sender()
    r = runner(tmp_path, snd, T + 2000)
    drive(r, [[watch(sym=f"S{i}")] for i in range(10)])
    assert len(snd.texts) == 10 and r.counts["tg_sent"] == 10
    assert all(w["telegram_sent"] == 1 for w in r.db.watches())


def test_v2_policy_has_no_hourly_cap_but_keeps_duplicate_stale_and_lane_protection():
    p = V2Telegram()
    t0 = ms(11, 0)
    sends = [p.decide(_w(sym=f"S{i}", leg="P1", t=t0 + i * 6 * ONE), t0 + i * 6 * ONE + 1000) for i in range(10)]
    assert [v for v, _ in sends] == ["send"] * 10                       # 10 in one hour: all sent
    assert p.decide(_w(sym="S0", leg="P1", t=t0), t0 + 2000) == ("skip", "setup already sent")
    assert p.decide(_w(sym="X", leg="P1", t=t0), t0 + 181_000) == ("skip", "found late")
    assert p.decide(_w(sym="Y", leg="P1", t=t0, lane="opening"), t0 + 1000)[0] == "skip"
    assert p.decide(_w(sym="Y", leg="P1", t=t0, lane="late"), t0 + 1000)[0] == "skip"
    assert "hourly" not in repr(p.__dict__)


def test_v1_stale_watch_still_not_sent(tmp_path):
    snd = Sender()
    r = runner(tmp_path, snd, T + 181_000)
    drive(r, [[watch()]])
    assert snd.texts == [] and "old when found" in r.db.watches()[0]["telegram_error"]


def test_v1_telegram_failure_never_changes_watch_state(tmp_path):
    ok, bad = runner(tmp_path / "a", Sender(), T + 1000), runner(tmp_path / "b", Sender(fail=True), T + 1000)
    for r in (ok, bad):
        drive(r, [[watch()], [watch(sym="SPY")]])
    strip = lambda rows: [{k: v for k, v in w.items() if not k.startswith("telegram") and k != "sent"} for w in rows]
    assert strip(ok.db.watches()) == strip(bad.db.watches())


# ---- 2. Telegram API safety -------------------------------------------------------------------------------

class FakeTime:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def clock(self):
        return self.now

    async def sleep(self, s):
        self.slept.append(round(s, 3))
        self.now += s


def sender(responses, chats=("-100chat",), **kw):
    seen, ft = [], FakeTime()

    def handler(req):
        seen.append(req)
        return responses.pop(0) if responses else httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    s = WatchSender("TOKEN123", list(chats), transport=httpx.MockTransport(handler), sleep=ft.sleep, clock=ft.clock,
                    **kw)
    return s, seen, ft


def test_429_retry_after_is_respected_then_sent():
    s, seen, ft = sender([httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})])
    assert asyncio.run(s.send("x")) == ["7"]
    assert len(seen) == 2 and ft.slept == [7.0] and s.rate_limited == 1


def test_two_429s_fail_after_two_attempts_without_a_loop():
    r429 = lambda: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}})
    s, seen, ft = sender([r429(), r429(), r429()])
    try:
        asyncio.run(s.send("x"))
        raise AssertionError("expected SendError")
    except SendError as exc:
        assert "retry_after 2s" in str(exc) and "TOKEN123" not in str(exc)
    assert len(seen) == 2


def test_long_retry_after_fails_fast_and_is_recorded():
    s, seen, ft = sender([httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 600}})])
    try:
        asyncio.run(s.send("x"))
        raise AssertionError("expected SendError")
    except SendError as exc:
        assert "not sent" in str(exc)
    assert len(seen) == 1 and ft.slept == []                    # never slept 600 s


def test_group_chat_pacing_spaces_messages_without_dropping_any():
    s, seen, ft = sender([])

    async def go():
        return [await s.send(f"m{i}") for i in range(4)]
    assert asyncio.run(go()) == [["7"]] * 4
    assert len(seen) == 4 and ft.slept == [3.0, 3.0, 3.0]


# ---- 3. stages and short-horizon scoring -------------------------------------------------------------------

def test_stage_classification():
    assert DL.v1_stage({"telegram_sent": 1, "lane": "main", "mode": "live"}) == (DL.SENT, None)
    assert DL.v1_stage({"lane": "main", "mode": "live", "telegram_error": "not sent: 400s old when found"})[0] == DL.SUPP
    assert DL.v1_stage({"lane": "main", "mode": "live", "telegram_error": "SendError: HTTP 400"})[0] == DL.FAILED
    assert DL.v1_stage({"lane": "opening", "mode": "live"})[1].startswith("outside Telegram window")
    assert DL.v1_stage({"lane": "main", "mode": "replay"})[0] == DL.NA
    assert DL.v2_stage({"mode": "live", "telegram_error": "not sent: hourly cap"})[1].startswith("hourly_cap (historical")
    assert DL.v2_stage({"mode": "live", "telegram_error": "not sent: found late"})[1].startswith("stale")
    assert DL.v2_stage({"mode": "live", "telegram_error": "not sent: setup already sent"})[1].startswith("already sent")
    assert DL.v2_stage({"mode": "live", "telegram_error": "merged into x"})[1].startswith("combined")
    assert DL.v2_stage({"mode": "live", "telegram_error": "TimeoutError: "})[0] == DL.FAILED


def test_horizon_scores_signed_returns_and_fixed_window_excursions(tmp_path):
    db = ObserverDB(tmp_path / "b.db")
    t0 = ms(10, 0)
    for i in range(70):                      # close climbs 0.1 per minute; minute 3 has a spike / dip
        c = 100 + 0.1 * (i + 1)
        h, l = (c + 2.0, c - 1.0) if i == 3 else (c + 0.05, c - 0.05)
        db.bar1(str(DAY), "META", Bar(t0 + i * ONE, c - 0.1, h, l, c, 100), "rest")
    bars = Bars(db)
    bull = DL.horizon_scores(bars, str(DAY), "META", "bull", t0, 100.0, ET)
    bear = DL.horizon_scores(bars, str(DAY), "META", "bear", t0, 100.0, ET)
    assert abs(bull["r5"] - 0.5) < 1e-9 and abs(bull["r10"] - 1.0) < 1e-9 and abs(bull["r60"] - 6.0) < 1e-9
    assert abs(bear["r15"] + 1.5) < 1e-9
    assert abs(bull["mfe_5"] - 2.4) < 1e-9 and abs(bull["mae_5"] + 0.6) < 1e-9     # minute 3 low 99.4
    assert abs(bear["mae_5"] + 2.4) < 1e-9                      # the spike is adverse for a bear watch
    assert abs(bull["mfe_60"] - 6.05) < 1e-9
    late = DL.horizon_scores(bars, str(DAY), "META", "bull", ms(15, 30), 100.0, ET)
    assert late["r30"] is not None and late["r60"] is None      # window past 16:00 is left out


# ---- 4. fresh setups while a watch is open: logged, never changes state --------------------------------------

def _v1_feats():
    new = hl(anchor=H2, level=100.5)
    return [feat(10, 10), feat(10, 11), feat(10, 20, hl=new), feat(10, 21, hl=new), feat(10, 22, hl=new)]


def test_v1_fresh_setup_is_logged_and_the_state_machine_is_unchanged():
    ob_a, fa = Observer(["META"], DAY, ET), None
    fa = Feed(ob_a)
    plain = fa.run(_v1_feats())
    ob_b = Observer(["META"], DAY, ET)
    fb = Feed(ob_b)
    inst, fresh = [], V1Fresh(ob_b)
    for f in _v1_feats():
        p = ob_b.prev.get("META")
        evs = fb.run([f])
        inst += evs
        inst += fresh.after("META", p, evs)
    rec = [e for e in inst if e["kind"] == "fresh_while_open"]
    assert [e for e in inst if e["kind"] != "fresh_while_open"] == plain
    assert len(kinds(plain, "watch")) == 1
    assert asdict(ob_a.lanes["main"]["META"].active) == asdict(ob_b.lanes["main"]["META"].active)
    assert len(rec) == 1
    r = rec[0]
    assert r["leg_id"] == f"P{H2.bar_t}" and r["old_leg_id"] == f"P{H1.bar_t}" and r["side"] == "bull"
    assert r["cand_at"] == ms(10, 22) and r["would_qualify"] == 1 and r["remaining_min"] == 50


def _v2_run(instrument):
    ob = V2Observer(["META"], DAY, ET)
    q = {"META": []}
    ob.states["META"].on_minute = lambda bar, gap=False: q["META"].pop(0)
    fresh = V2Fresh(ob) if instrument else None
    c, c2 = hl(), hl(anchor=H2, level=100.5)
    feats = [feat(10, 10, hl=None), feat(10, 11, hl=c, dev_events=(("DEV_HL", c),)), feat(10, 12, hl=c),
             feat(10, 20, hl=c2, dev_events=(("DEV_HL", c2),)), feat(10, 21, hl=c2), feat(10, 22, hl=c2)]
    out = []
    for f in feats:
        q["META"].append(f)
        evs = ob.process("META", Bar(f.t, f.o, f.h, f.l, f.c, f.v))
        out += evs
        if fresh:
            out += fresh.after("META", evs)
    return out, ob


def test_v2_fresh_setup_is_followed_on_a_shadow_and_never_creates_a_watch():
    plain, ob_a = _v2_run(False)
    inst, ob_b = _v2_run(True)
    assert [e for e in inst if not e["kind"].startswith("fresh")] == plain
    assert ob_a.open.keys() == ob_b.open.keys() and ob_a.pending.keys() == ob_b.pending.keys()
    assert ob_a.used_legs == ob_b.used_legs
    assert len([e for e in plain if e["kind"] == "v2_watch"]) == 1
    rec = [e for e in inst if e["kind"] == "fresh_while_open"]
    upd = [e for e in inst if e["kind"] == "fresh_update"]
    assert len(rec) == 1 and rec[0]["family"] == "structure" and rec[0]["leg_id"] == f"P{H2.bar_t}"
    assert rec[0]["would_qualify"] is None and upd[0]["would_qualify"] == 1   # persistence held on the shadow


def test_fresh_rows_are_stored_and_updated(tmp_path):
    inst, _ = _v2_run(True)
    from mias.observer.v2_log import V2DB
    db = V2DB(tmp_path / "v2.db")
    db.apply_v2(inst, "replay")
    row = db.db.execute("SELECT family, would_qualify, qualify_detail FROM fresh_while_open").fetchall()
    assert row == [("structure", 1, "persistence: watched")]
