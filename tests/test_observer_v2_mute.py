"""2026-11-16 delivery-policy change: V2-A Structure is a Telegram-only mute. Structure is still detected, stored,
given options context, tracked and scored; it never sends a message of its own. VWAP / opening-range delivery, v1,
detection and the options context are unchanged."""
import asyncio
from datetime import datetime

from mias.indicators import Bar
from mias.observer import delivery as DL
from mias.observer.log import ObserverDB
from mias.observer.service import LiveRunner
from mias.observer.settings import Settings
from mias.observer.v2 import V2Telegram
from mias.observer.v2_log import V2DB
from observer_synth import ET, daily, history
from test_observer_telegram import Sender
from test_observer_v2 import DAYS, SYMS, FakeClient, Src, _w
from test_observer_watch import DAY, ms

MUTE = ("structure",)
CTX = {"vwap": 100.0, "rel_vol": 1.2, "spy_pct": 0.3,
       "optx": {"status": "ok", "relation": {"bull": "CONFIRMING", "bear": "CONFLICTING"},
                "class": {"bull": "CONFIRMING", "bear": "CONFLICTING"}, "source": "live snapshot"}}


# ---- the pure policy ----------------------------------------------------------------------------------------

def test_policy_mutes_structure_only_and_leaves_its_state_untouched():
    p, now = V2Telegram(muted=MUTE), ms(10, 13) + 2000
    assert p.decide(_w("structure"), now) == ("muted", "structure_telegram_muted")
    assert p.sent_setups == set() and p.recent == {}                 # as if it had never been offered
    assert p.decide(_w("vwap", leg="VW1", t=ms(10, 14)), ms(10, 14) + 1000)[0] == "send"
    assert p.decide(_w("or", leg="OR1", sym="SPY"), now)[0] == "send"
    assert p.decide(_w("structure", lane="opening"), now)[0] == "skip"     # outside the window stays that
    assert p.decide(_w("structure", leg="P7"), now + 600_000)[0] == "muted"  # policy reason, never 'found late'


def test_without_the_mute_the_policy_is_unchanged():
    p, now = V2Telegram(), ms(10, 13) + 2000
    assert p.decide(_w("structure"), now)[0] == "send"
    assert p.decide(_w("vwap", leg="VW1", t=ms(10, 14)), ms(10, 14) + 1000)[0] == "merged"


# ---- the service: stored, context kept, combined messages still sent ----------------------------------------

class FakeV2:
    """Stands in for V2Observer: returns prepared watch events and keeps 'open' like the real one."""

    def __init__(self, batches):
        self.batches, self.open = list(batches), {}

    def process(self, sym, b, gap, received_at):
        evs = self.batches.pop(0)
        for e in evs:
            self.open[(e["family"], e["sym"], e["side"])] = e
        return evs


def ev(fam, sym="META", side="bull", leg=None, t=None):
    w = _w(fam, sym, side, t or ms(10, 13), leg or {"structure": "P1", "vwap": "VW1", "or": "OR1"}[fam])
    w.update(kind="v2_watch", watch_id=f"{DAY}|{fam}|{sym}|{side}|{w['leg_id']}", cand_id=f"c|{fam}|{sym}",
             candidate_at=w["watch_at"] - 60_000, candidate_price=100.9, pre_ema=True, ema_candidate={},
             leg_start={}, context=dict(CTX), state="WATCH", rules_version="v2-test")
    return w


def live(tmp_path, batches, muted=MUTE):
    s = Settings(True, ["SPY", "META"], "wss://x", tmp_path / "o.db", tmp_path / "c", 5, 150, datetime.min.time(),
                 datetime.max.time(), 20, False, 180, True, tmp_path / "v2.db", True, v2_telegram_muted=muted)

    class C:
        tz = ET
    snd = Sender()
    r = LiveRunner(C(), s, None, ObserverDB(s.db_path), None, clock=lambda: (ms(10, 13) + 2000) / 1000,
                   sender=snd, v2db=V2DB(s.v2_db_path))
    r.day, r.v2 = DAY, FakeV2(batches)
    stop = asyncio.Event()

    async def go():
        task = asyncio.create_task(r.v2_send_worker(stop))
        for _ in batches:
            r._process_v2("META", Bar(ms(10, 13), 1, 1, 1, 1, 1), "ws", None, False)
        for _ in range(50):
            if r.v2_outbox.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop.set()
        await task
    asyncio.run(go())
    return r, snd, {w["watch_id"]: w for w in r.v2db.v2_watches()}


def test_structure_only_is_stored_with_context_and_not_sent(tmp_path):
    r, snd, rows = live(tmp_path, [[ev("structure")]])
    w = rows[f"{DAY}|structure|META|bull|P1"]
    assert snd.texts == [] and r.counts["v2_tg_muted"] == 1 and r.counts["v2_tg_failed"] == 0
    assert w["state"] == "WATCH" and w["context"]["optx"]["relation"]["bull"] == "CONFIRMING"
    assert w["telegram_sent"] == 0 and w["telegram_error"] == "muted: structure_telegram_muted"
    assert DL.v2_stage(w) == (DL.MUTED, "structure_telegram_muted")


def test_structure_plus_vwap_sends_once_and_lists_structure(tmp_path):
    for order in (["structure", "vwap"], ["vwap", "structure"]):
        r, snd, rows = live(tmp_path / order[0], [[ev(f) for f in order]])
        assert len(snd.texts) == 1 and snd.texts[0].startswith("🧭 V2 EARLY WATCH · META · BULLISH")
        assert "✓ Structure" in snd.texts[0] and "✓ VWAP reclaim / loss" in snd.texts[0]
        s = rows[f"{DAY}|structure|META|bull|P1"]
        assert rows[f"{DAY}|vwap|META|bull|VW1"]["telegram_sent"] == 1 and s["telegram_sent"] == 0
        assert DL.v2_stage(s) == (DL.MUTED, f"structure_telegram_muted · {DL.SHOWN}")


def test_structure_plus_opening_range_sends(tmp_path):
    r, snd, _ = live(tmp_path, [[ev("structure"), ev("or")]])
    assert len(snd.texts) == 1 and "✓ Structure" in snd.texts[0] and "✓ Opening range" in snd.texts[0]


def test_structure_vwap_and_opening_range_send_exactly_once(tmp_path):
    r, snd, rows = live(tmp_path, [[ev("structure"), ev("vwap"), ev("or")]])
    assert len(snd.texts) == 1 and r.counts["v2_tg_merged"] == 1 and r.counts["v2_tg_muted"] == 1
    assert all(f"✓ {x}" in snd.texts[0] for x in ("Structure", "VWAP reclaim / loss", "Opening range"))


def test_vwap_only_and_opening_range_only_unchanged(tmp_path):
    for fam in ("vwap", "or"):
        muted = live(tmp_path / f"m{fam}", [[ev(fam)]])
        plain = live(tmp_path / f"p{fam}", [[ev(fam)]], muted=())
        assert muted[1].texts == plain[1].texts and len(muted[1].texts) == 1
        assert "○ Structure" in muted[1].texts[0]


def test_mute_off_still_sends_structure(tmp_path):
    r, snd, _ = live(tmp_path, [[ev("structure")]], muted=())
    assert len(snd.texts) == 1 and snd.texts[0].startswith("🧭 V2 STRUCTURE WATCH")


# ---- muting never reaches detection, storage or v1 ----------------------------------------------------------

def _full_day(tmp_path, name, muted):
    h = history(SYMS, DAYS, seed=13)
    s = Settings(True, SYMS, "wss://x", tmp_path / f"{name}.db", tmp_path / "cache", 5, 150, datetime.min.time(),
                 datetime.max.time(), 20, True, 180, True, tmp_path / f"{name}_v2.db", True, v2_telegram_muted=muted)

    class C:
        tz = ET
    end = datetime.combine(DAYS[-1], datetime.min.time().replace(hour=15, minute=30), ET).timestamp()
    r = LiveRunner(C(), s, FakeClient(h, daily(h)), ObserverDB(s.db_path), Src(), clock=lambda: end,
                   sender=Sender(), v2db=V2DB(s.v2_db_path))
    asyncio.run(r.start_day(DAYS[-1]))
    strip = lambda rows: [{k: v for k, v in w.items() if not k.startswith("telegram") and k != "sent"} for w in rows]
    cands = r.v2db.db.execute("SELECT * FROM v2_candidate_log ORDER BY cand_id").fetchall()
    return strip(r.v2db.v2_watches()), strip(r.db.watches()), cands, sorted(r.v2.open), r


def test_muting_changes_no_watch_id_time_count_state_context_or_v1(tmp_path):
    on, off = _full_day(tmp_path, "mute", MUTE), _full_day(tmp_path, "plain", ())
    assert on[:4] == off[:4]                          # v2 watches, v1 watches, candidates, open detector state
    assert any(w["family"] == "structure" for w in on[0])
    assert on[4].counts["v2_tg_sent"] == off[4].counts["v2_tg_sent"] == 0  # found late at 15:30 here: none sent


# ---- reports -------------------------------------------------------------------------------------------------

def _row(fam, sym, t, err=None, sent=0, end=None, recv=None):
    w = ev(fam, sym, t=t)
    w.update(mode="live", received_at=recv or (t / 1000 + 1), end_at=end, telegram_sent=sent, telegram_error=err,
             r5=0.1, r15=0.2, r60=-0.1, mfe_15=0.3, mae_15=-0.1, mfe_60=0.4, mae_60=-0.2)
    w["stage"], w["stage_reason"] = DL.v2_stage(w)
    return w


def test_report_shows_muted_structure_with_metrics_and_the_policy_estimate():
    rows = [_row("structure", "META", ms(10, 13), sent=1),                      # recorded before the change
            _row("vwap", "META", ms(10, 14), err="merged into x"),
            _row("structure", "SPY", ms(11, 0), err="muted: structure_telegram_muted"),
            _row("vwap", "AMD", ms(12, 0), sent=1)]
    assert [w["stage"] for w in rows] == [DL.SENT, DL.SUPP, DL.MUTED, DL.SENT]
    msgs = DL.policy_replay(rows, MUTE)
    assert [m["w"]["watch_id"].split("|")[1:3] for m in msgs] == [["vwap", "META"], ["vwap", "AMD"]]
    assert DL.combo(msgs[0]["fams"]) == "structure + vwap" and msgs[0]["listed"] == [rows[0]["watch_id"]]
    assert len(DL.policy_replay(rows, ())) == 3
    text = "\n".join(DL.mute_lines(rows, MUTE, {"structure": "V2-A Structure"}))
    assert "### 8a. V2-A Structure Telegram-only mute" in text
    assert "watches created 2 · sent 1" in text and "muted 1 (of which 0 listed" in text
    assert "| structure only | 1 | 2 | 0 |" in text and "| **total** | **2** | **3** | **2** |" in text
    assert "| muted alone (no message) | 15m | 1 | 100% |" in text
    assert "| listed in a combined message | 15m | 1 | 100% |" in text
    assert "muted alone · CONFIRMING" in text
    funnel = "\n".join(DL.funnel_lines([("V2-A Structure", 2, [w for w in rows if w["family"] == "structure"])]))
    assert "TELEGRAM MUTED (policy)" in funnel and "| V2-A Structure | 2 | 2 | 1 | 1 | 0 | 0 | 1 | 0 |" in funnel
    assert "failed" not in funnel.split("\n")[-1]


def test_a_recorded_send_is_never_rewritten_as_muted(tmp_path):
    r, snd, _ = live(tmp_path, [[ev("structure")]], muted=())       # sent under the old policy
    r.v2tg, r.v2.batches = V2Telegram(muted=MUTE), [[ev("vwap")]]
    stop = asyncio.Event()

    async def go():
        r.v2_outbox = asyncio.Queue()                                  # a fresh loop needs a fresh queue
        task = asyncio.create_task(r.v2_send_worker(stop))
        r._process_v2("META", Bar(ms(10, 13), 1, 1, 1, 1, 1), "ws", None, False)
        await asyncio.sleep(0.1)
        stop.set()
        await task
    asyncio.run(go())
    s = {w["watch_id"]: w for w in r.v2db.v2_watches()}[f"{DAY}|structure|META|bull|P1"]
    assert len(snd.texts) == 2 and s["telegram_sent"] == 1 and s["telegram_error"] is None
