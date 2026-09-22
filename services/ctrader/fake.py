from datetime import datetime, timezone, timedelta
from decimal import Decimal
from services.ctrader.types import (
    AccountSnapshot, MarketFeed, OhlcBar, Position, SymbolInfo, Tick, spread_bps,
)


class FakeCTraderFeed:
    """Deterministic cTrader-shaped feed for tests. Never used as a live broker."""

    def __init__(self, *, symbol='EURUSD', bid='1.10000', ask='1.10020',
                 closes=None, balance='10000', equity='10000', positions=None):
        self.symbol = symbol
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)
        self.closes = [Decimal(str(v)) for v in (closes or [1.1]*30)]
        self.balance = Decimal(balance)
        self.equity = Decimal(equity)
        self._positions = positions or []

    def tick(self, symbol: str) -> Tick:
        now = datetime.now(timezone.utc)
        return Tick(symbol=symbol, bid=self.bid, ask=self.ask,
                    spread_bps=spread_bps(self.bid, self.ask), as_of=now)

    def ohlc(self, symbol: str, timeframe: str, count: int = 100) -> list[OhlcBar]:
        now = datetime.now(timezone.utc)
        bars = []
        values = self.closes[-count:]
        for i, close in enumerate(values):
            bars.append(OhlcBar(
                symbol=symbol, timeframe=timeframe, open=close, high=close, low=close,
                close=close, closed_at=now - timedelta(minutes=len(values)-i),
            ))
        return bars

    def symbols(self) -> list[SymbolInfo]:
        return [SymbolInfo(name=self.symbol, digits=5, pip_position=4, lot_size='100000')]

    def positions(self) -> list[Position]:
        return list(self._positions)

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            balance=self.balance, equity=self.equity, currency='USD',
            as_of=datetime.now(timezone.utc), positions=tuple(self._positions),
        )
