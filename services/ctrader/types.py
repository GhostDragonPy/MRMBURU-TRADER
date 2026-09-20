from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Protocol
from pydantic import Field
from core.contracts import Contract, NonNegative, Positive, MarketState, AccountState


class Tick(Contract):
    symbol: str
    bid: Positive
    ask: Positive
    spread_bps: NonNegative
    as_of: datetime
    source: Literal['ctrader'] = 'ctrader'

    @property
    def mid(self):
        return (self.bid + self.ask) / 2


class OhlcBar(Contract):
    symbol: str
    timeframe: str
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume: NonNegative = Decimal('0')
    closed_at: datetime
    source: Literal['ctrader'] = 'ctrader'


class SymbolInfo(Contract):
    name: str
    digits: int = Field(ge=0)
    pip_position: int | None = None
    lot_size: Positive | None = None


class Position(Contract):
    symbol: str
    side: Literal['buy', 'sell']
    volume: Positive
    entry: Positive
    unrealized: Decimal
    position_id: str | None = None


class AccountSnapshot(Contract):
    balance: Positive
    equity: Positive
    currency: str = Field(min_length=3, max_length=3)
    as_of: datetime
    positions: tuple[Position, ...] = ()
    source: Literal['ctrader'] = 'ctrader'


class CTraderAuthRequired(RuntimeError):
    pass


class CTraderUnavailable(RuntimeError):
    pass


class MarketFeed(Protocol):
    def tick(self, symbol: str) -> Tick: ...
    def ohlc(self, symbol: str, timeframe: str, count: int = 100) -> list[OhlcBar]: ...
    def symbols(self) -> list[SymbolInfo]: ...
    def positions(self) -> list[Position]: ...
    def account(self) -> AccountSnapshot: ...


def spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    if mid <= 0:
        return Decimal('0')
    return ((ask - bid) / mid) * Decimal('10000')


def market_state_from_tick(tick: Tick, *, news_known=True, news_checked_at=None,
                           high_impact_events=(), slippage_bps='0', volatility='0.01',
                           connected=True, platform_ready=True) -> MarketState:
    now = news_checked_at or tick.as_of
    return MarketState(
        as_of=tick.as_of,
        connected=connected,
        platform_ready=platform_ready,
        spread_bps=tick.spread_bps,
        slippage_bps=slippage_bps,
        volatility=volatility,
        news_known=news_known,
        news_checked_at=now,
        high_impact_events=high_impact_events,
        bid=tick.bid,
        ask=tick.ask,
        source='ctrader',
    )


def account_state_from_feed(snapshot: AccountSnapshot, *, initial_balance, risk_day,
                            enabled, open_risk='0', trades_today=0, consecutive_losses=0) -> AccountState:
    return AccountState(
        initial_balance=initial_balance,
        balance=snapshot.balance,
        equity=snapshot.equity,
        day_start_balance=snapshot.balance,
        risk_day=risk_day,
        open_risk=open_risk,
        open_positions=len(snapshot.positions),
        trades_today=trades_today,
        consecutive_losses=consecutive_losses,
        as_of=snapshot.as_of,
        enabled=enabled,
    )
