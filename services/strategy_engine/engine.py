from datetime import datetime
from decimal import Decimal
from typing import Protocol
from pydantic import Field, model_validator
from core.contracts import Contract, Positive, Signal

class Candle(Contract):
    closed_at: datetime
    close: Positive

    @model_validator(mode='after')
    def aware(self):
        if self.closed_at.tzinfo is None: raise ValueError('Timezone required')
        return self

class SmaConfig(Contract):
    fast: int = Field(default=5, ge=2)
    slow: int = Field(default=20, ge=3)
    stop_fraction: Decimal = Field(default=Decimal('0.01'), gt=0, lt=1, allow_inf_nan=False)
    reward_risk: Positive = Decimal('2')

    @model_validator(mode='after')
    def periods(self):
        if self.fast >= self.slow: raise ValueError('fast must be below slow')
        if self.stop_fraction*self.reward_risk >= 1: raise ValueError('Invalid short target')
        return self

class Strategy(Protocol):
    def generate(self, candles: list[Candle], *, now: datetime, symbol: str,
                 timeframe: str, quantity: Decimal, value_per_price_unit: Decimal) -> Signal | None: ...

class SmaCross:
    """Deterministic reference strategy; illustrative, no profitability claim.

    Uses only fully closed, strictly ordered bars. Intended fill is next bar,
    never the closing price of the bar that generated the signal in a backtest.
    """
    def __init__(self, config: SmaConfig, version: str = 'sma-cross:1'):
        self.config, self.version = config, version

    def generate(self, candles, *, now, symbol, timeframe, quantity, value_per_price_unit):
        if now.tzinfo is None: raise ValueError('Timezone required')
        if any(c.closed_at > now for c in candles): raise ValueError('Future candle')
        if any(a.closed_at >= b.closed_at for a,b in zip(candles, candles[1:])):
            raise ValueError('Candles must be unique and chronological')
        if len(candles) < self.config.slow+1: return None
        closes = [c.close for c in candles]
        def average(values, n): return sum(values[-n:])/n
        old = average(closes[:-1], self.config.fast)-average(closes[:-1], self.config.slow)
        new = average(closes, self.config.fast)-average(closes, self.config.slow)
        side = 'buy' if old <= 0 < new else 'sell' if old >= 0 > new else None
        if side is None: return None
        entry = closes[-1]; distance = entry*self.config.stop_fraction
        direction = 1 if side == 'buy' else -1
        return Signal(symbol=symbol, side=side, entry=entry,
                      stop_loss=entry-direction*distance,
                      take_profit=entry+direction*distance*self.config.reward_risk,
                      quantity=quantity, value_per_price_unit=value_per_price_unit,
                      timeframe=timeframe, strategy_version=self.version, created_at=now,
                      reasons=('SMA_CROSS',), context={'last_closed_bar': candles[-1].closed_at.isoformat(),
                                                     'parameters': self.config.model_dump(mode='json')})
