"""Async Massive REST client: retries with backoff, bounded concurrency, safe pagination.

The API key is sent only as an Authorization header. It never appears in URLs, logs or errors.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class MassiveError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class MassiveClient:
    def __init__(self, api_key: str, base_url: str = "https://api.massive.com", *, timeout: float = 20,
                 max_retries: int = 4, max_concurrency: int = 8, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).netloc
        self._retries = max_retries
        self._sem = asyncio.Semaphore(max_concurrency)
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=max_concurrency * 2, max_keepalive_connections=max_concurrency),
        )
        self.requests = 0

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self._base + path_or_url
        if urlsplit(url).netloc != self._host:
            raise MassiveError(f"refusing request to foreign host {urlsplit(url).netloc}")
        label = urlsplit(url).path
        for attempt in range(self._retries + 1):
            try:
                async with self._sem:
                    self.requests += 1
                    resp = await self._http.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._retries:
                    raise MassiveError(f"{label}: {type(exc).__name__}") from None
                await self._backoff(attempt, label, type(exc).__name__)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise MassiveError(f"{label}: invalid JSON", 200) from None
            if resp.status_code in RETRY_STATUS and attempt < self._retries:
                retry_after = _retry_after(resp)
                await self._backoff(attempt, label, f"HTTP {resp.status_code}", retry_after)
                continue
            raise MassiveError(f"{label}: HTTP {resp.status_code} {_message(resp)}", resp.status_code)
        raise MassiveError(f"{label}: retries exhausted")

    async def _backoff(self, attempt: int, label: str, why: str, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(10.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3)
        log.warning("massive %s failed (%s), retry %d in %.1fs", label, why, attempt + 1, delay)
        await asyncio.sleep(delay)

    async def paginate(self, path: str, params: dict, max_pages: int) -> tuple[list[dict], bool]:
        """Return (results, truncated)."""
        out: list[dict] = []
        body = await self.get(path, params)
        pages = 1
        out.extend(body.get("results") or [])
        while body.get("next_url"):
            if pages >= max_pages:
                return out, True
            body = await self.get(body["next_url"])
            pages += 1
            out.extend(body.get("results") or [])
        return out, False

    # ---- endpoints -------------------------------------------------------------------------

    async def market_status(self) -> dict:
        return await self.get("/v1/marketstatus/now")

    async def upcoming_holidays(self) -> list[dict]:
        body = await self.get("/v1/marketstatus/upcoming")
        return body if isinstance(body, list) else body.get("results", [])

    async def aggregates(self, symbol: str, multiplier: int, timespan: str, start: date, end: date) -> list[dict]:
        body = await self.get(
            f"/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        return body.get("results") or []

    async def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 250):
            chunk = symbols[i:i + 250]
            body = await self.get("/v3/snapshot", {"ticker.any_of": ",".join(chunk), "limit": 250})
            for item in body.get("results") or []:
                if item.get("ticker"):
                    out[item["ticker"]] = item
        return out

    async def option_chain(self, symbol: str, *, spot: float, today: date, max_dte: int, strike_pct: float,
                           max_pages: int) -> tuple[list[dict], bool]:
        from datetime import timedelta
        params = {
            "limit": 250,
            "expiration_date.gte": today.isoformat(),
            "expiration_date.lte": (today + timedelta(days=max_dte)).isoformat(),
            "strike_price.gte": f"{spot * (1 - strike_pct):.2f}",
            "strike_price.lte": f"{spot * (1 + strike_pct):.2f}",
        }
        return await self.paginate(f"/v3/snapshot/options/{symbol}", params, max_pages)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(30.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error") or "")[:200]
    except ValueError:
        return ""
