"""Alert rules. Edge-triggered with confirmation and cooldowns, state persisted across restarts.

Per symbol, one message per cycle combines every reason that fired:
  trend_change    composite stock trend class changed (bull / neutral / bear), confirmed N cycles
  options_flip    options bias class changed, confirmed N cycles
  aligned         stock and options agree on a direction (newly)
  divergence      stock and options point opposite ways with a meaningful options bias (newly)
  block           a single contract traded >= block_premium since the previous cycle
  iv_spike        front ATM IV rose >= N vol points within the window
  wall_break      live price crossed the previous cycle's call wall (up) or put wall (down)
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import SymbolResult
from .trend import direction

DIR_WORD = {"bull": "BULLISH", "bear": "BEARISH", "neutral": "NEUTRAL"}


@dataclass
class Reason:
    rule: str
    direction: str
    text: str


def _confirm(slot: dict, new: str, need: int) -> tuple[str, str] | None:
    """Track a confirmed class. Returns (old, new) once a different class has held `need` cycles."""
    cur = slot.get("cls")
    if cur is None:
        slot.update(cls=new, pending=None, count=0)
        return None
    if new == cur:
        slot.update(pending=None, count=0)
        return None
    if slot.get("pending") == new:
        slot["count"] = slot.get("count", 0) + 1
    else:
        slot.update(pending=new, count=1)
    if slot["count"] >= need:
        slot.update(cls=new, pending=None, count=0)
        return cur, new
    return None


def _cooled(state: dict, key: str, now: float, minutes: float) -> bool:
    last = state.setdefault("last", {}).get(key)
    if last is not None and now - last < minutes * 60:
        return False
    state["last"][key] = now
    return True


def evaluate(r: SymbolResult, state: dict, cfg: Config, now: float,
             iv_history: list[tuple[float, float]]) -> list[Reason]:
    """Update `state` in place and return the reasons to alert on for this symbol."""
    reasons: list[Reason] = []
    stock_change = opt_change = None

    if r.stock and r.stock.timeframes:
        stock_change = _confirm(state.setdefault("stock", {}), direction(r.stock.label), cfg.confirm_cycles)
        if stock_change and _cooled(state, "trend_change", now, cfg.cooldown_minutes):
            old, new = stock_change
            reasons.append(Reason("trend_change", new,
                                  f"Stock trend {DIR_WORD[old]} → {DIR_WORD[new]} ({r.stock.score:+.0f})"))

    if r.bias and r.bias.components and not (r.options and r.options.thin):
        opt_change = _confirm(state.setdefault("options", {}), direction(r.bias.label), cfg.confirm_cycles)
        if opt_change and _cooled(state, "options_flip", now, cfg.cooldown_minutes):
            old, new = opt_change
            reasons.append(Reason("options_flip", new,
                                  f"Options bias {DIR_WORD[old]} → {DIR_WORD[new]} ({r.bias.score:+.0f})"))

    s_cls = state.get("stock", {}).get("cls")
    o_cls = state.get("options", {}).get("cls")
    if s_cls and o_cls:
        relation = "none"
        if s_cls == o_cls and s_cls != "neutral":
            relation = f"aligned_{s_cls}"
        elif {s_cls, o_cls} == {"bull", "bear"} and r.bias and direction(r.bias.label) == o_cls and \
                abs(r.bias.score) >= cfg.divergence_min_options_score:
            relation = f"diverge_{o_cls}"
        prev = state.get("relation")
        state["relation"] = relation
        if prev is not None and relation != prev and relation != "none":
            if relation.startswith("aligned") and _cooled(state, "aligned", now, cfg.cooldown_minutes):
                reasons.append(Reason("aligned", s_cls,
                                      f"Stock and options ALIGNED {DIR_WORD[s_cls]}"))
            elif relation.startswith("diverge") and _cooled(state, "divergence", now, cfg.cooldown_minutes):
                reasons.append(Reason("divergence", o_cls,
                                      f"DIVERGENCE: stock {DIR_WORD[s_cls]} but options {DIR_WORD[o_cls]}"))

    if r.options:
        for p in r.options.blocks:
            if p.side == "mid":
                continue
            if _cooled(state, f"block:{p.ticker}", now, cfg.block_cooldown_minutes):
                bullish = (p.side == "buy") == (p.kind == "call")
                reasons.append(Reason("block", "bull" if bullish else "bear",
                                      f"Block: {p.side.upper()} {p.volume:,.0f}x {p.expiry} {p.strike:g}"
                                      f"{p.kind[0].upper()} @ {p.price or 0:.2f} = ${p.premium / 1e6:.1f}M"))

        iv = r.options.atm_iv_front
        if iv is not None and iv_history:
            low = min(v for _, v in iv_history)
            if iv - low >= cfg.iv_spike_points and _cooled(state, "iv_spike", now, cfg.cooldown_minutes):
                d = "bear" if (r.day_change_pct or 0) < 0 else "neutral"
                reasons.append(Reason("iv_spike", d, f"IV spike: front ATM IV {low:.1f}% → {iv:.1f}% "
                                                     f"in {cfg.iv_spike_window_minutes}m"))

        price = r.price
        walls = state.get("walls") or {}
        prev_price = state.get("price")
        if price and prev_price and r.price_is_live:
            cw, pw = walls.get("call"), walls.get("put")
            if cw and prev_price < cw <= price and _cooled(state, "wall_up", now, cfg.cooldown_minutes):
                reasons.append(Reason("wall_break", "bull", f"Broke ABOVE call wall {cw:g} (now {price:.2f})"))
            if pw and prev_price > pw >= price and _cooled(state, "wall_down", now, cfg.cooldown_minutes):
                reasons.append(Reason("wall_break", "bear", f"Broke BELOW put wall {pw:g} (now {price:.2f})"))
        if price:
            state["price"] = price
        state["walls"] = {"call": r.options.call_wall, "put": r.options.put_wall}

    # Direction changes lead the message.
    order = {"trend_change": 0, "options_flip": 1, "aligned": 2, "divergence": 2, "wall_break": 3,
             "block": 4, "iv_spike": 5}
    reasons.sort(key=lambda x: order.get(x.rule, 9))
    return reasons


def market_composite(results: list[SymbolResult], symbols: list[str]) -> tuple[float, float | None] | None:
    rs = [r for r in results if r.symbol in symbols and r.stock]
    if not rs:
        return None
    stock = round(sum(r.stock.score for r in rs) / len(rs), 1)
    ob = [r.bias.score for r in rs if r.bias and r.bias.components]
    return stock, (round(sum(ob) / len(ob), 1) if ob else None)
