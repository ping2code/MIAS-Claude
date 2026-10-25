"""Daily spread picks: at most one bull call spread and one bear put spread, chosen by fixed rules.

Candidate rules (per side, mirror for puts):
  tier A  swing STRONG UP (>= strong threshold), confidence MEDIUM/HIGH, options not against the trade
  tier B  swing UP (>= bias threshold), confidence MEDIUM/HIGH, options not against the trade
  always  options not thin; index-style instruments (e.g. NDX) are skipped (contracts too large)
Ranking: swing score + 0.5 x intraday score + 0.5 x options score (signed toward the trade).

Spread: expiry 30-60 days (closest to 45), long leg about 0.50 delta, short leg about 0.25 delta further out
of the money, both with open interest >= 1,000 and a quoted spread <= 5% of mid.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .massive import MassiveClient
from .models import SymbolResult
from .options import Contract, parse_contract
from .verdict import verdict

log = logging.getLogger(__name__)

DTE = (30, 60)
TARGET_DTE = 45
LONG_DELTA, SHORT_DELTA = 0.50, 0.25
MIN_OI = 1000
MAX_LEG_SPREAD_PCT = 5.0
TAKE_PROFIT = 0.55          # share of max profit
TIME_STOP_DAYS = 10         # trading days
HARD_EXIT_DAYS = 7          # calendar days before expiry
CANDIDATES_PER_SIDE = 3     # chains fetched per side, best first


@dataclass
class Spread:
    symbol: str
    side: str               # "call" (bullish) | "put" (bearish)
    tier: str
    expiry: date
    dte: int
    long_strike: float
    short_strike: float
    long_mid: float
    short_mid: float
    natural: tuple[float, float]
    long_oi: float
    short_oi: float
    price: float
    swing: float
    intraday: float
    options: float | None
    confidence: str

    @property
    def cost(self) -> float:
        return round(self.long_mid - self.short_mid, 2)

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)

    @property
    def max_profit(self) -> float:
        return round(self.width - self.cost, 2)

    @property
    def breakeven(self) -> float:
        return self.long_strike + self.cost if self.side == "call" else self.long_strike - self.cost


def _signed(r: SymbolResult, side: str) -> tuple[float, float, float]:
    sign = 1 if side == "call" else -1
    intra = r.stock.composites.get("intraday", (0.0, ""))[0]
    opt = r.bias.score if r.bias and r.bias.components else 0.0
    return sign * r.stock.score, sign * intra, sign * opt


def candidates(results: list[SymbolResult], side: str, cfg) -> list[tuple[str, float, SymbolResult]]:
    """Qualifying symbols for one side, best first, as (tier, rank, result)."""
    out = []
    tfs = list(cfg.trend_profiles[cfg.trend_primary])
    for r in results:
        if not r.ok or not r.stock or not r.options or r.options.thin or r.symbol in cfg.instruments:
            continue
        swing, intra, opt = _signed(r, side)
        if opt <= -cfg.options_bias:                  # options money against the trade
            continue
        if verdict(r, tfs).level not in ("HIGH", "MEDIUM"):
            continue
        tier = "A" if swing >= cfg.trend_strong else "B" if swing >= cfg.trend_bias else None
        if tier:
            out.append((tier, swing + 0.5 * intra + 0.5 * opt, r))
    return sorted(out, key=lambda x: (x[0], -x[1]))


def closest(results: list[SymbolResult], side: str) -> SymbolResult | None:
    rs = [r for r in results if r.ok and r.stock]
    if not rs:
        return None
    return max(rs, key=lambda r: _signed(r, side)[0])


def build_spread(contracts: list[Contract], side: str) -> tuple[Contract, Contract] | None:
    kind = side
    usable = [c for c in contracts if c.kind == kind and DTE[0] <= c.dte <= DTE[1] and c.delta is not None
              and c.mid and c.oi >= MIN_OI and 100 * (c.ask - c.bid) / c.mid <= MAX_LEG_SPREAD_PCT]
    if not usable:
        return None
    dte_of = {c.expiry: c.dte for c in usable}
    for exp in sorted(dte_of, key=lambda e: abs(dte_of[e] - TARGET_DTE)):
        legs = [c for c in usable if c.expiry == exp]
        long = min(legs, key=lambda c: abs(abs(c.delta) - LONG_DELTA))
        further = [c for c in legs if (c.strike > long.strike if kind == "call" else c.strike < long.strike)]
        if not further or abs(abs(long.delta) - LONG_DELTA) > 0.12:
            continue
        short = min(further, key=lambda c: abs(abs(c.delta) - SHORT_DELTA))
        if abs(abs(short.delta) - SHORT_DELTA) > 0.12 or long.mid - short.mid <= 0:
            continue
        return long, short
    return None


async def pick_side(client: MassiveClient, results: list[SymbolResult], side: str, cfg, today: date,
                    tz) -> tuple[Spread | None, SymbolResult | None]:
    """Best qualifying spread for one side, or (None, closest non-qualifying symbol)."""
    for tier, _, r in candidates(results, side, cfg)[:CANDIDATES_PER_SIDE]:
        try:
            raw, _ = await client.option_chain(r.symbol, spot=r.price, today=today, max_dte=DTE[1],
                                               strike_pct=0.20, max_pages=cfg.max_chain_pages)
        except Exception as exc:
            log.warning("picks: %s chain failed: %s", r.symbol, exc)
            continue
        contracts = [c for c in (parse_contract(i, today, tz) for i in raw) if c]
        legs = build_spread(contracts, side)
        if not legs:
            continue
        long, short = legs
        swing, intra, opt = _signed(r, side)
        sign = 1 if side == "call" else -1
        return Spread(
            r.symbol, side, tier, long.expiry, long.dte, long.strike, short.strike, long.mid, short.mid,
            (round(long.bid - short.ask, 2), round(long.ask - short.bid, 2)), long.oi, short.oi, r.price,
            sign * swing, sign * intra, (sign * opt) if r.bias and r.bias.components else None,
            verdict(r, list(cfg.trend_profiles[cfg.trend_primary])).level), None
    return None, closest(results, side)


def add_trading_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


async def daily_picks(client: MassiveClient, results: list[SymbolResult], cfg, now: datetime) -> dict:
    today = now.astimezone(cfg.tz).date()
    call, call_near = await pick_side(client, results, "call", cfg, today, cfg.tz)
    put, put_near = await pick_side(client, results, "put", cfg, today, cfg.tz)
    return {"call": call, "put": put, "call_near": call_near, "put_near": put_near, "today": today}
