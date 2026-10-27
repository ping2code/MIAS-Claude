"""Sector board: which of the configured sectors is trending, so focus can move to the strongest one.

A sector's score is the average primary (swing) score of its member stocks; its ETF is shown alongside as a
check. Breadth counts members trending up and down. The leader is the member furthest in the sector's direction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

from . import formatting
from .config import Sector
from .models import SymbolResult
from .trend import direction, label_for


@dataclass
class SectorRead:
    sector: Sector
    swing: float | None
    intraday: float | None
    up: int
    down: int
    members: list[SymbolResult]
    etf: SymbolResult | None


def _score(r: SymbolResult, profile: str | None = None) -> float | None:
    if not r.stock:
        return None
    if profile is None:
        return r.stock.score
    return r.stock.composites.get(profile, (None,))[0]


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def read(results: list[SymbolResult], sectors: list[Sector]) -> list[SectorRead]:
    """Sector reads ranked strongest first; sectors with no data yet go last."""
    by_sym = {r.symbol: r for r in results}
    out = []
    for s in sectors:
        members = [by_sym[x] for x in s.symbols if x in by_sym]
        out.append(SectorRead(
            s, _avg([_score(r) for r in members]), _avg([_score(r, "intraday") for r in members]),
            sum(1 for r in members if r.stock and direction(r.stock.label) == "bull"),
            sum(1 for r in members if r.stock and direction(r.stock.label) == "bear"),
            members, by_sym.get(s.etf) if s.etf else None))
    return sorted(out, key=lambda x: -(x.swing if x.swing is not None else -999))


def _leader(sr: SectorRead) -> SymbolResult | None:
    scored = [r for r in sr.members if _score(r) is not None]
    if not scored:
        return None
    return min(scored, key=_score) if (sr.swing or 0) < 0 else max(scored, key=_score)


def board_lines(results: list[SymbolResult], sectors: list[Sector], cfg) -> list[str]:
    reads = [x for x in read(results, sectors) if x.swing is not None]
    if not reads:
        return []
    lines = []
    for sr in reads:
        lbl = label_for(sr.swing, cfg.trend_strong, cfg.trend_bias)
        parts = [f"{formatting.ICON[direction(lbl)]} <b>{escape(sr.sector.name)}</b> {formatting.label(lbl)} "
                 f"({sr.swing:+.0f})"]
        if sr.intraday is not None:
            parts.append(f"intraday {sr.intraday:+.0f}")
        parts.append(f"{sr.up}↑ {sr.down}↓")
        if sr.etf and sr.etf.stock:
            parts.append(f"{sr.sector.etf} {sr.etf.stock.score:+.0f}")
        lead = _leader(sr)
        if lead:
            parts.append(f"lead {lead.symbol} {_score(lead):+.0f}")
        lines.append(" · ".join(parts))
    if len(reads) > 1:
        best, worst = reads[0], reads[-1]
        focus = f"Focus: <b>{escape(best.sector.name)}</b>"
        if worst.swing < 0:
            focus += f" · weakest: {escape(worst.sector.name)}"
        lines.append(focus)
    return lines


def board(results: list[SymbolResult], sectors: list[Sector], cfg, now: datetime) -> str:
    lines = board_lines(results, sectors, cfg)
    if not lines:
        return "No sector data yet: the board needs a completed check (market hours)."
    head = f"🧭 <b>Sector board</b> ({cfg.trend_primary})  {now:%a %H:%M} ET"
    return "\n".join([head, *lines, "<i>/sector energy (or tech, financials, defensive) for the members</i>"])


def detail(results: list[SymbolResult], sector: Sector, cfg, now: datetime) -> str:
    sr = next(x for x in read(results, [sector]))
    if sr.swing is None:
        return f"No data yet for {escape(sector.name)}: it needs a completed check (market hours)."
    lbl = label_for(sr.swing, cfg.trend_strong, cfg.trend_bias)
    lines = [f"🧭 <b>{escape(sector.name)}</b> {formatting.label(lbl)} ({sr.swing:+.0f}) · "
             f"{sr.up}↑ {sr.down}↓  {now:%a %H:%M} ET"]
    rows = ["SYM     PRICE  DAY%  SWNG INTR OPTS  IV"]
    shown = sorted(sr.members, key=lambda r: -(_score(r) if _score(r) is not None else -999))
    if sr.etf:
        shown.append(sr.etf)
    for r in shown:
        opts = None if (not r.bias or (r.options and r.options.thin)) else r.bias.score
        rows.append(
            f"{r.symbol:<5}{formatting.num(r.price, '.2f'):>9}{formatting.num(r.day_change_pct, '.1f', True):>6}"
            f"{formatting.num(_score(r, 'swing'), '.0f', True):>6}"
            f"{formatting.num(_score(r, 'intraday'), '.0f', True):>5}"
            f"{formatting.num(opts, '.0f', True):>5}"
            f"{formatting.num(r.options.atm_iv_month if r.options else None, '.0f'):>4}")
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")
    if sector.etf:
        lines.append(f"<i>{escape(sector.etf)} (last row) is the sector gauge, not in the averages.</i>")
    return "\n".join(lines)


def find(sectors: list[Sector], name: str) -> Sector | None:
    name = name.lower()
    for s in sectors:
        if name in (s.key, s.name.lower(), (s.etf or "").lower()) or s.key.startswith(name):
            return s
    return None
