"""Synthetic market data and a fake Massive API (httpx.MockTransport)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")


def session_5m_bars(days: list[date], start_price: float, drift_per_bar: float, noise: float = 0.0) -> list[dict]:
    bars, p, i = [], start_price, 0
    for d in days:
        t = datetime.combine(d, time(9, 30), ET)
        for _ in range(78):
            wobble = noise * math.sin(i * 1.7)
            o = p
            c = p + drift_per_bar + wobble
            bars.append({"t": int(t.astimezone(timezone.utc).timestamp() * 1000), "o": o, "h": max(o, c) + 0.05,
                         "l": min(o, c) - 0.05, "c": c, "v": 1000})
            p, t, i = c, t + timedelta(minutes=5), i + 1
    return bars


def daily_bars(days: list[date], start_price: float, drift: float) -> list[dict]:
    out, p = [], start_price
    for d in days:
        t = datetime.combine(d, time(0, 0), ET)
        c = p + drift
        out.append({"t": int(t.timestamp() * 1000), "o": p, "h": max(p, c) + 0.5, "l": min(p, c) - 0.5, "c": c,
                    "v": 1e6})
        p = c
    return out


def trading_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def contract(sym: str, kind: str, strike: float, expiry: date, *, spot: float, volume: float = 0, oi: float = 100,
             iv: float = 0.3, now_ns: int, last_at: str = "mid", day_today: bool = True,
             spread: float = 0.04) -> dict:
    """Contract with a mid priced by intrinsic + flat time value, so put-call parity holds at `spot`."""
    tv = 1.0
    intrinsic = max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    mid = intrinsic + tv
    bid, ask = mid - spread / 2, mid + spread / 2
    last = {"buy": ask, "sell": bid, "mid": mid}[last_at]
    m = abs(spot - strike) / spot
    delta = (0.5 - m * 5) if kind == "call" else -(0.5 - m * 5)
    if (kind == "call" and strike < spot) or (kind == "put" and strike > spot):
        delta = (0.5 + m * 5) if kind == "call" else -(0.5 + m * 5)
    delta = max(-0.99, min(0.99, delta))
    day_ns = now_ns if day_today else now_ns - 86400 * 10**9 * 3
    tick = f"O:{sym}{expiry:%y%m%d}{kind[0].upper()}{int(strike * 1000):08d}"
    return {
        "details": {"contract_type": kind, "strike_price": strike, "expiration_date": expiry.isoformat(),
                    "ticker": tick},
        "day": {"volume": volume, "vwap": mid, "last_updated": day_ns},
        "greeks": {"delta": delta, "gamma": 0.02},
        "implied_volatility": iv,
        "open_interest": oi,
        "last_quote": {"bid": bid, "ask": ask, "last_updated": now_ns},
        "last_trade": {"price": last, "sip_timestamp": now_ns},
    }


def chain(sym: str, spot: float, today: date, now_ns: int, *, volumes: dict | None = None,
          last_at: dict | None = None, iv: float = 0.3) -> list[dict]:
    """volumes / last_at keyed by (kind, strike, dte)."""
    volumes, last_at = volumes or {}, last_at or {}
    out = []
    for dte in (1, 8, 30):
        exp = today + timedelta(days=dte)
        for k in range(int(spot) - 10, int(spot) + 11, 5):
            for kind in ("call", "put"):
                key = (kind, float(k), dte)
                out.append(contract(sym, kind, float(k), exp, spot=spot, volume=volumes.get(key, 10),
                                    oi=1000, iv=iv, now_ns=now_ns, last_at=last_at.get(key, "mid")))
    return out


class FakeMassive:
    """Routes the endpoints the engine uses. Mutate attributes between cycles to move the market."""

    def __init__(self, today: date, now: datetime, symbols: dict[str, float]):
        self.today, self.now = today, now
        self.spots = dict(symbols)
        self.drift = {s: 0.05 for s in symbols}
        self.volumes: dict[str, dict] = {s: {} for s in symbols}
        self.last_at: dict[str, dict] = {s: {} for s in symbols}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        assert request.headers["Authorization"] == "Bearer test-key"
        if path == "/v1/marketstatus/now":
            return _json({"market": "open"})
        if path == "/v3/snapshot":
            syms = request.url.params["ticker.any_of"].split(",")
            return _json({"results": [{"ticker": s, "session": {"price": self.spots[s] - 0.3,
                                                                "previous_close": self.spots[s] - 2}}
                                      for s in syms if s in self.spots]})
        parts = path.split("/")
        if path.startswith("/v2/aggs/ticker/"):
            sym, mult, span, start, end = parts[4], int(parts[6]), parts[7], parts[8], parts[9]
            if sym in self.fail:
                return httpx.Response(500, json={"error": "boom"})
            s, e = date.fromisoformat(start), date.fromisoformat(end)
            if span == "day":
                days = [d for d in trading_days(e, 300) if d >= s]
                return _json({"results": daily_bars(days, self.spots[sym] - 300 * self.drift[sym] * 10,
                                                    self.drift[sym] * 10)})
            days = trading_days(self.today, 30)
            start_p = self.spots[sym] - len(days) * 78 * self.drift[sym]
            bars = session_5m_bars(days, start_p, self.drift[sym])
            lo = int(datetime.combine(s, time(0), ET).timestamp() * 1000)
            hi = int(datetime.combine(e + timedelta(days=1), time(0), ET).timestamp() * 1000)
            return _json({"results": [b for b in bars if lo <= b["t"] < hi]})
        if path.startswith("/v3/snapshot/options/"):
            sym = parts[4]
            if sym in self.fail:
                return httpx.Response(500, json={"error": "boom"})
            now_ns = int(self.now.timestamp() * 1e9)
            return _json({"results": chain(sym, self.spots[sym], self.today, now_ns,
                                           volumes=self.volumes[sym], last_at=self.last_at[sym])})
        return httpx.Response(404, json={"error": "not found"})


def _json(body) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
