"""Massive WebSocket client: auth + subscribe, AM bars, reconnect with backoff, watchdog, auth failure, the API key
never in feed logs; the recorder's dup / revised / out-of-order classification."""
import asyncio
import json

import pytest

from mias.observer.stream import AuthError, Backoff, Recorder, StreamClient, parse

KEY = "SECRET-KEY-123"


class FakeWS:
    def __init__(self, script, sent):
        self.script, self.sent = list(script), sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item()
        return json.dumps(item)


def hello():
    return [[{"ev": "status", "status": "connected"}], [{"ev": "status", "status": "auth_success"}]]


def bar(sym, s):
    return {"ev": "AM", "sym": sym, "s": s, "e": s + 60000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}


def test_parse_and_recorder():
    assert parse("not json") == [] and parse('{"ev":"AM"}') == [{"ev": "AM"}]
    r = Recorder()
    a = bar("SPY", 120000)
    assert r.classify(a) == "ok" and r.classify(dict(a)) == "dup"
    assert r.classify(dict(a, c=9)) == "revised"
    assert r.classify(bar("SPY", 180000)) == "ok" and r.classify(bar("SPY", 60000)) == "old"


def test_backoff_doubles_to_the_cap_and_resets():
    b = Backoff(base=1, cap=60, jitter=0)
    assert [b.next() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next() == 1


def test_subscribe_bars_reconnect_and_unsubscribe():
    sent, feed, got = [], [], []
    stop = asyncio.Event()
    scripts = [hello() + [[bar("SPY", 0), bar("QQQ", 0)]] + [ConnectionError("drop")],
               hello() + [[bar("SPY", 60000)]]]

    def connect(url):
        return FakeWS(scripts.pop(0), sent)

    async def on_bar(ev, rec, conn):
        got.append((ev["sym"], ev["s"], conn))
        if conn == 2:
            stop.set()

    c = StreamClient("wss://x", KEY, ["SPY", "QQQ"], on_bar=on_bar, on_feed=lambda k, d="": feed.append((k, d)),
                     connect=connect, backoff=Backoff(base=0.001, cap=0.01, jitter=0))
    asyncio.run(asyncio.wait_for(c.run(stop, lambda: True), 5))
    assert got == [("SPY", 0, 1), ("QQQ", 0, 1), ("SPY", 60000, 2)]
    assert sent[0] == {"action": "auth", "params": KEY}
    assert sent[1] == {"action": "subscribe", "params": "AM.SPY,AM.QQQ"}
    assert {"action": "unsubscribe", "params": "AM.SPY,AM.QQQ"} in sent
    kinds = [k for k, _ in feed]
    assert "disconnect" in kinds and "reconnect_wait" in kinds and kinds.count("subscribed") == 2
    assert all(KEY not in d for _, d in feed)


def test_watchdog_forces_reconnect_during_the_session():
    sent, feed = [], []
    stop = asyncio.Event()
    n = {"c": 0}

    def connect(url):
        n["c"] += 1
        if n["c"] == 2:
            stop.set()
        return FakeWS(hello(), sent)

    async def on_bar(*a):
        pass

    c = StreamClient("wss://x", KEY, ["SPY"], on_bar=on_bar, on_feed=lambda k, d="": feed.append(k),
                     in_session=lambda: True, watchdog_s=0.05, connect=connect,
                     backoff=Backoff(base=0.001, cap=0.01, jitter=0))
    asyncio.run(asyncio.wait_for(c.run(stop, lambda: True), 5))
    assert "watchdog" in feed and n["c"] == 2


def test_auth_failure_raises():
    sent = []
    ws = FakeWS([[{"ev": "status", "status": "connected"}], [{"ev": "status", "status": "auth_failed",
                                                              "message": "bad key"}]], sent)
    c = StreamClient("wss://x", KEY, ["SPY"], on_bar=None)
    with pytest.raises(AuthError):
        asyncio.run(c._handshake(ws))
    assert not any(m.get("action") == "subscribe" for m in sent)
