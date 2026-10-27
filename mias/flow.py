"""Trade-by-trade options flow for the unusual-activity report, and next-day open-interest confirmation.

Each scan, the contracts that traded unusually since the previous report get their trades and quotes for that
window (two requests per contract). Every print is classified on its own against the quote in force when it
printed (Lee-Ready): at or above mid + 10% of the spread = buyer-initiated, at or below mid - 10% = seller-
initiated, in between = mid (no side). Prints flagged by the exchange as multi-leg (spreads, rolls, combos) or
tied to stock (buy-writes, married puts) go into their own bucket: their direction can't be read from one leg.
Sweeps (intermarket sweep orders) are counted. Cancelled prints are dropped.

Volume above open interest does not prove new positions: intraday volume includes closing trades and the same
contracts changing hands. Open interest is published once a day, so the next morning's OI change shows how much
of yesterday's flagged volume was opening (OI up by about the volume) versus closing or turnover.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

CANCELED = {201, 203, 205, 207}
MULTI_LEG = {232, 233, 234, 235, 236, 238, 239, 246, 247}
STOCK_TIED = {237, 240, 241, 242, 243, 244, 245}
SWEEP = 219
ONE_SIDED = 0.60        # share of a window's premium needed to call it bullish or bearish


@dataclass
class Tape:
    ticker: str
    kind: str = "call"
    trades: int = 0
    contracts: float = 0.0
    buy: float = 0.0         # premium, $
    sell: float = 0.0
    mid: float = 0.0
    multi: float = 0.0
    sweeps: int = 0
    sweep_premium: float = 0.0
    unquoted: int = 0        # prints with no quote to compare against (counted as mid)

    @property
    def total(self) -> float:
        return self.buy + self.sell + self.mid + self.multi

    @property
    def bull(self) -> float:
        return self.buy if self.kind == "call" else self.sell

    @property
    def bear(self) -> float:
        return self.sell if self.kind == "call" else self.buy

    @property
    def lean(self) -> str:
        """bull / bear when one side has >= 60% of all premium, else mixed."""
        if self.total <= 0:
            return "mixed"
        if self.bull >= ONE_SIDED * self.total:
            return "bull"
        if self.bear >= ONE_SIDED * self.total:
            return "bear"
        return "mixed"

    def pct(self, part: float) -> int:
        return round(100 * part / self.total) if self.total else 0

    def add(self, other: "Tape") -> None:
        for k in ("trades", "contracts", "buy", "sell", "mid", "multi", "sweeps", "sweep_premium", "unquoted"):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    def to_dict(self) -> dict:
        return asdict(self)


def classify(ticker: str, kind: str, trades: list[dict], quotes: list[dict]) -> Tape:
    """Aggregate prints into a Tape. trades and quotes are API results sorted by sip_timestamp ascending."""
    t = Tape(ticker, kind)
    qi, cur = 0, None
    for tr in trades:
        conds = set(tr.get("conditions") or [])
        if conds & CANCELED:
            continue
        price, size, ts = tr.get("price"), tr.get("size"), tr.get("sip_timestamp") or 0
        if not price or not size:
            continue
        prem = price * size * 100
        t.trades += 1
        t.contracts += size
        if SWEEP in conds:
            t.sweeps += 1
            t.sweep_premium += prem
        if conds & (MULTI_LEG | STOCK_TIED):
            t.multi += prem
            continue
        while qi < len(quotes) and (quotes[qi].get("sip_timestamp") or 0) <= ts:
            cur = quotes[qi]
            qi += 1
        bid, ask = (cur or {}).get("bid_price"), (cur or {}).get("ask_price")
        if not bid or not ask or ask <= bid:
            t.unquoted += 1
            t.mid += prem
            continue
        m, spread = (bid + ask) / 2, ask - bid
        if price >= m + 0.1 * spread:
            t.buy += prem
        elif price <= m - 0.1 * spread:
            t.sell += prem
        else:
            t.mid += prem
    return t


async def tape(client, ticker: str, kind: str, start_ns: int, end_ns: int) -> Tape:
    """Trades and quotes for one contract over [start_ns, end_ns), classified."""
    trades, _ = await client.paginate(f"/v3/trades/{ticker}", {
        "timestamp.gte": start_ns, "timestamp.lt": end_ns, "limit": 50000, "order": "asc", "sort": "timestamp"}, 3)
    if not trades:
        return Tape(ticker, kind)
    first = trades[0].get("sip_timestamp") or start_ns
    quotes, _ = await client.paginate(f"/v3/quotes/{ticker}", {
        "timestamp.gte": first - 120 * 10**9, "timestamp.lte": trades[-1].get("sip_timestamp") or end_ns,
        "limit": 50000, "order": "asc", "sort": "timestamp"}, 4)
    return classify(ticker, kind, trades, quotes)


async def tapes(client, contracts: list[tuple[str, str]], start_ns: int, end_ns: int) -> dict[str, Tape]:
    """Tapes for (ticker, kind) pairs; a failed contract is skipped (the report falls back to the last trade)."""
    async def one(tk, kind):
        try:
            return tk, await tape(client, tk, kind, start_ns, end_ns)
        except Exception as exc:
            log.warning("tape %s failed: %s", tk, exc)
            return tk, None
    out = await asyncio.gather(*(one(tk, k) for tk, k in contracts))
    return {tk: t for tk, t in out if t is not None}


# ---- next-day open interest ----------------------------------------------------------------------

def oi_verdict(volume: float, oi_before: float, oi_after: float) -> tuple[str, float]:
    """('opened' | 'partly' | 'closed', share of the volume that showed up as new open interest)."""
    share = (oi_after - oi_before) / volume if volume else 0.0
    if share >= 0.5:
        return "opened", share
    if share >= 0.15:
        return "partly", share
    return "closed", share


async def oi_check(client, day: dict) -> list[dict]:
    """Yesterday's flagged contracts with today's open interest. `day` is the kv 'unusual_day' record."""
    items = sorted(day.get("contracts", {}).items(), key=lambda kv: -kv[1].get("premium", 0))[:40]
    if not items:
        return []
    snaps = await client.snapshots([tk for tk, _ in items])
    out = []
    for tk, c in items:
        oi = (snaps.get(tk) or {}).get("open_interest")
        if oi is None:
            continue
        verdict, share = oi_verdict(c["volume"], c["oi"], float(oi))
        out.append(dict(c, ticker=tk, oi_after=float(oi), verdict=verdict, share=round(share, 2)))
    return out
