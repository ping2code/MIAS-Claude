"""Massive stocks WebSocket, per-minute aggregates (AM) only, one connection, the observer's symbols only.

Protocol: connect -> [{"ev":"status","status":"connected"}] -> {"action":"auth","params":KEY} ->
"auth_success" -> {"action":"subscribe","params":"AM.SPY,AM.QQQ,..."}; bars arrive as JSON arrays of
{"ev":"AM","sym","o","h","l","c","v","vw","a","av","op","z","s","e"}. On shutdown: unsubscribe, close.

Reconnect with exponential backoff (1, 2, 4 ... 60 s, +/-20% jitter; reset after a session that lasted 2 min).
Disconnects are detected by websocket ping/pong and, during the regular session, by a watchdog: no AM message for
watchdog_seconds forces a reconnect. The API key is sent only in the auth message and is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

log = logging.getLogger("mias.observer.stream")


class AuthError(Exception):
    pass


class Backoff:
    def __init__(self, base: float = 1.0, cap: float = 60.0, jitter: float = 0.2, rng: random.Random | None = None):
        self.base, self.cap, self.jitter = base, cap, jitter
        self.n = 0
        self.rng = rng or random.Random()

    def next(self) -> float:
        d = min(self.cap, self.base * 2 ** self.n)
        self.n += 1
        return d * (1 + self.rng.uniform(-self.jitter, self.jitter))

    def reset(self) -> None:
        self.n = 0


class Recorder:
    """Classifies each AM bar: ok (new), dup (same bar again), revised (same window, different values),
    old (a window before the newest one already seen for that symbol: out of order)."""

    FIELDS = ("o", "h", "l", "c", "v")

    def __init__(self):
        self.seen: dict[tuple, tuple] = {}
        self.newest: dict[str, int] = {}

    def classify(self, ev: dict) -> str:
        key = (ev.get("sym"), ev.get("s"), ev.get("e"))
        vals = tuple(ev.get(f) for f in self.FIELDS)
        if key in self.seen:
            return "dup" if self.seen[key] == vals else "revised"
        self.seen[key] = vals
        sym, s = ev.get("sym"), ev.get("s") or 0
        if sym in self.newest and s < self.newest[sym]:
            return "old"
        self.newest[sym] = max(s, self.newest.get(sym, s))
        return "ok"


def parse(raw) -> list[dict]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class StreamClient:
    def __init__(self, url: str, api_key: str, symbols: list[str], *, on_bar, on_connect=None, on_feed=None,
                 in_session=None, watchdog_s: float = 150, connect=None, backoff: Backoff | None = None,
                 sleep=asyncio.sleep):
        self.url, self._key, self.symbols = url, api_key, list(symbols)
        self.on_bar, self.on_connect = on_bar, on_connect
        self.on_feed = on_feed or (lambda kind, detail="": None)
        self.in_session = in_session or (lambda: False)
        self.watchdog_s = watchdog_s
        self._connect = connect
        self.backoff = backoff or Backoff()
        self.sleep = sleep
        self.conn_id = 0
        self.connected = False
        self.last_msg = 0.0
        self.reconnects = 0

    def _params(self) -> str:
        return ",".join(f"AM.{s}" for s in self.symbols)

    async def run(self, stop: asyncio.Event, active) -> None:
        """Keep one connection open while active() is true; idle otherwise."""
        while not stop.is_set():
            if not active():
                await self._wait(stop, 30)
                continue
            started = time.monotonic()
            try:
                await self._once(stop, active)
            except AuthError as exc:
                self.on_feed("auth_failed", str(exc))
                log.error("observer stream auth failed: %s", exc)
                await self._wait(stop, 300)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.on_feed("disconnect", f"{type(exc).__name__}: {exc}")
                log.warning("observer stream disconnected: %s", exc)
            finally:
                self.connected = False
            if stop.is_set():
                return
            if time.monotonic() - started > 120:
                self.backoff.reset()
            if active():
                self.reconnects += 1
                delay = self.backoff.next()
                self.on_feed("reconnect_wait", f"{delay:.1f}s")
                await self._wait(stop, delay)

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _once(self, stop: asyncio.Event, active) -> None:
        connect = self._connect
        if connect is None:
            import websockets
            connect = lambda url: websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=15,
                                                     close_timeout=5, max_size=2 ** 22)
        async with connect(self.url) as ws:
            await self._handshake(ws)
            self.conn_id += 1
            self.connected = True
            self.last_msg = time.time()
            self.on_feed("subscribed", f"conn {self.conn_id}: {len(self.symbols)} symbols")
            if self.on_connect:
                await self.on_connect(self.conn_id)
            try:
                while not stop.is_set() and active():
                    timeout = self.watchdog_s if self.in_session() else 300
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        if self.in_session():
                            self.on_feed("watchdog", f"no message for {timeout:.0f}s")
                            raise ConnectionError("watchdog: stream silent during the session")
                        continue
                    received = time.time()
                    self.last_msg = received
                    for ev in parse(raw):
                        if ev.get("ev") == "AM":
                            await self.on_bar(ev, received, self.conn_id)
                        elif ev.get("ev") == "status":
                            st = ev.get("status")
                            if st not in ("success",):
                                self.on_feed(f"status_{st}", str(ev.get("message", ""))[:200])
            finally:
                try:
                    await ws.send(json.dumps({"action": "unsubscribe", "params": self._params()}))
                except Exception:
                    pass

    async def _handshake(self, ws) -> None:
        deadline = time.monotonic() + 15
        authed = False
        await ws.send(json.dumps({"action": "auth", "params": self._key}))
        while not authed:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            for ev in parse(raw):
                st = ev.get("status")
                if ev.get("ev") != "status":
                    continue
                if st == "auth_success":
                    authed = True
                elif st in ("auth_failed", "auth_timeout", "max_connections"):
                    raise AuthError(f"{st}: {ev.get('message', '')}")
                else:
                    self.on_feed(f"status_{st}", str(ev.get("message", ""))[:200])
        await ws.send(json.dumps({"action": "subscribe", "params": self._params()}))
