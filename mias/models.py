from __future__ import annotations

from dataclasses import dataclass

from .options import OptionsBias, OptionsSnapshot
from .trend import StockTrend


@dataclass
class SymbolResult:
    symbol: str
    stock: StockTrend | None = None
    options: OptionsSnapshot | None = None
    bias: OptionsBias | None = None
    stock_price: float | None = None     # from the stock feed (delayed on Stocks Starter)
    live_price: float | None = None       # implied from options quotes when available
    prev_close: float | None = None
    chain_truncated: bool = False
    errors: list[str] | None = None

    @property
    def price(self) -> float | None:
        return self.live_price or self.stock_price

    @property
    def price_is_live(self) -> bool:
        return self.live_price is not None

    @property
    def day_change_pct(self) -> float | None:
        if self.price and self.prev_close:
            return round(100 * (self.price / self.prev_close - 1), 2)
        return None

    @property
    def ok(self) -> bool:
        return self.stock is not None or self.options is not None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price, "price_live": self.price_is_live, "stock_price": self.stock_price,
            "prev_close": self.prev_close, "day_change_pct": self.day_change_pct,
            "stock_score": self.stock.score if self.stock else None,
            "stock_label": self.stock.label if self.stock else None,
            "timeframes": {tf: {"score": t.score, "label": t.label, "components": t.components,
                                "adx": t.adx, "rsi": t.rsi}
                           for tf, t in (self.stock.timeframes.items() if self.stock else [])},
            "options_score": self.bias.score if self.bias else None,
            "options_label": self.bias.label if self.bias else None,
            "options_components": self.bias.components if self.bias else None,
            "session_net_premium": self.bias.session_net_premium if self.bias else None,
            "options": self.options.to_dict() if self.options else None,
            "chain_truncated": self.chain_truncated,
            "errors": self.errors or [],
        }
