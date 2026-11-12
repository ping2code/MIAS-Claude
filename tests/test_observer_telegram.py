"""Telegram pilot: only the initial message of a fresh main-lane watch is sent; opening-lane candidates, late
(backfilled) watches, ends and suppressions are not; a failed send is recorded and never blocks the observer."""
import asyncio
from datetime import date, datetime
from pathlib import Path

import httpx

from mias.observer.log import ObserverDB
from mias.observer.service import LiveRunner
from mias.observer.settings import Settings
from mias.observer.telegram import SendError, WatchSender
from observer_synth import ET

DAY = date(2026, 11, 12)
T = int(datetime.combine(DAY, datetime.min.time().replace(hour=10, minute=12), ET).timestamp() * 1000)


def watch(lane="main", created=T, sym="META"):
    return {"kind": "watch", "watch_id": f"2026-11-12|{sym}|bull|{lane}|P1", "day": str(DAY), "lane": lane,
            "sym": sym, "side": "bull", "leg_id": "P1", "created_at": created, "price": 641.2, "vwap": 640.0,
            "ema9": 640.5, "ema21": 640.1, "spread": 0.4, "spread_ref": 0.3, "atr": 1.0, "structure_type": "HL",
            "level": 638.9, "or_high": 642.0, "or_low": 636.0, "or_break": False, "minutes_since_open": 42,
            "opening_shadow_only": lane == "opening", "context": {"rel_vol": 0.9}, "received_at": None,
            "state": "WATCH", "end_at": None, "end_reason": None, "upgrade_source": None, "upgrade_at": None,
            "upgrade_price": None, "rules_version": "early-watch-v1"}


class FakeOb:
    def __init__(self, events):
        self.events = events

    def process(self, sym, bar, gap, received_at):
        return self.events.pop(0)


class Sender:
    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    async def send(self, text):
        self.texts.append(text)
        if self.fail:
            raise SendError("HTTP 400: chat not found")
        return ["4711"]


def runner(tmp_path, sender, now_ms, telegram=True):
    s = Settings(True, ["SPY", "META"], "wss://x", tmp_path / "o.db", tmp_path / "c", 5, 150,
                 datetime.min.time(), datetime.max.time(), 20, telegram, 180)

    class C:
        tz = ET
    r = LiveRunner(C(), s, None, ObserverDB(s.db_path), None, clock=lambda: now_ms / 1000, sender=sender)
    r.day = DAY
    return r


def drive(r, batches):
    from mias.indicators import Bar
    r.ob = FakeOb(list(batches))
    stop = asyncio.Event()

    async def go():
        task = asyncio.create_task(r.send_worker(stop))
        for _ in batches:
            r._process("META", Bar(T, 1, 1, 1, 1, 1), "ws", None)
        for _ in range(50):
            if r.outbox.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop.set()
        await task
    asyncio.run(go())


def test_fresh_main_watch_is_sent_once_with_message_id(tmp_path):
    snd = Sender()
    r = runner(tmp_path, snd, T + 2000)
    end = {"kind": "end", "watch_id": watch()["watch_id"], "state": "INVALIDATED", "end_at": T + 60000,
           "end_reason": "close_beyond_level", "upgrade_source": None, "upgrade_at": None, "upgrade_price": None,
           "lane": "main", "sym": "META", "cooldown_until": 0, "chop_until": 0}
    supp = {"kind": "suppressed", "day": str(DAY), "lane": "main", "sym": "META", "side": "bull", "leg_id": "P2",
            "reason": "hourly_cap", "at": T}
    drive(r, [[watch()], [watch()], [end, supp]])          # the repeated watch (restart replay) is not new
    assert len(snd.texts) == 1 and snd.texts[0].startswith("👀 EARLY WATCH · META · BULLISH · 10:12 ET @ $641.20")
    row = r.db.watches()[0]
    assert row["telegram_sent"] == 1 and row["telegram_message_id"] == "4711" and row["state"] == "INVALIDATED"


def test_opening_lane_and_late_watches_are_not_sent(tmp_path):
    snd = Sender()
    r = runner(tmp_path, snd, T + 600_000)                  # found 10 minutes late (e.g. restart backfill)
    drive(r, [[watch("opening")], [watch("main", sym="SPY")]])
    assert snd.texts == []
    rows = {w["lane"]: w for w in r.db.watches()}
    assert rows["main"]["telegram_sent"] == 0 and "not sent" in rows["main"]["telegram_error"]
    assert rows["opening"]["telegram_sent"] == 0 and rows["opening"]["telegram_error"] is None


def test_failed_send_is_recorded_and_does_not_block(tmp_path):
    r = runner(tmp_path, Sender(fail=True), T + 1000)
    drive(r, [[watch()], [watch("main", sym="SPY")]])
    rows = r.db.watches()
    assert len(rows) == 2 and all(w["telegram_sent"] == 0 and "chat not found" in w["telegram_error"] for w in rows)
    assert r.counts["tg_failed"] == 2 and r.counts["processed"] == 2


def test_disabled_sends_nothing(tmp_path):
    snd = Sender()
    r = runner(tmp_path, snd, T + 1000, telegram=False)
    drive(r, [[watch()]])
    assert snd.texts == [] and r.db.watches()[0]["telegram_sent"] == 0


def test_sender_posts_plain_text_and_never_leaks_the_token():
    seen = []

    def handler(req):
        seen.append(req)
        if b'"chat_id":"bad"' in req.content.replace(b" ", b""):
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    s = WatchSender("TOKEN123", ["c1"], transport=httpx.MockTransport(handler))
    assert asyncio.run(s.send("x < y")) == ["99"]
    assert b"parse_mode" not in seen[0].content
    s2 = WatchSender("TOKEN123", ["bad"], transport=httpx.MockTransport(handler))
    try:
        asyncio.run(s2.send("x"))
        raise AssertionError("expected SendError")
    except SendError as exc:
        assert "TOKEN123" not in str(exc) and "chat not found" in str(exc)
