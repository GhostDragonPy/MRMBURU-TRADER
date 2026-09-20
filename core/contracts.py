from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)

class Signal(Contract):
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal['buy', 'sell']
    entry: Positive
    stop_loss: Positive
    take_profit: Positive
    quantity: Positive
    # Account-currency value of a price-unit move for one quantity unit.
    # Must come from instrument metadata/FX conversion in a future broker adapter.
    value_per_price_unit: Positive
    cost_reserve: NonNegative = Decimal('0')
    timeframe: str = Field(min_length=1, max_length=16)
    strategy_version: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime
    reasons: tuple[str, ...] = ()
    context: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_bracket(self):
        valid = (self.stop_loss < self.entry < self.take_profit if self.side == 'buy'
                 else self.take_profit < self.entry < self.stop_loss)
        if not valid:
            raise ValueError('Invalid directional stop/entry/target bracket')
        return self

    @property
    def risk_amount(self):
        return abs(self.entry-self.stop_loss)*self.quantity*self.value_per_price_unit+self.cost_reserve

class RiskPolicy(Contract):
    risk_per_trade: Fraction = Decimal('0.005')
    max_open_risk: Fraction = Decimal('0.01')
    daily_loss_fraction: Fraction = Decimal('0.02')
    total_loss_fraction: Fraction = Decimal('0.05')
    max_positions: int = Field(default=3, gt=0)
    max_trades_daily: int = Field(default=5, gt=0)
    max_consecutive_losses: int = Field(default=3, gt=0)
    max_spread_bps: Positive = Decimal('5')
    max_slippage_bps: Positive = Decimal('3')
    max_volatility: Positive = Decimal('0.03')
    max_data_age_seconds: int = Field(default=30, gt=0)
    news_window_minutes: int = Field(default=30, ge=0)

class AccountState(Contract):
    initial_balance: Positive
    balance: Positive
    equity: Positive
    day_start_balance: Positive
    risk_day: date
    open_risk: NonNegative
    open_positions: int = Field(ge=0)
    trades_today: int = Field(ge=0)
    consecutive_losses: int = Field(ge=0)
    as_of: AwareDatetime
    enabled: bool

class MarketState(Contract):
    as_of: AwareDatetime
    connected: bool
    platform_ready: bool
    spread_bps: NonNegative
    slippage_bps: NonNegative
    volatility: NonNegative
    news_known: bool
    news_checked_at: AwareDatetime
    high_impact_events: tuple[AwareDatetime, ...] = ()

class PropRules(Contract):
    # Generic simulation profile, NOT certified FTMO rules.
    name: str = 'generic-paper'
    verified: bool = False
    timezone: str = 'Europe/Prague'
    daily_loss_fraction: Fraction = Decimal('0.05')
    total_loss_fraction: Fraction = Decimal('0.10')
    profit_target_fraction: Fraction = Decimal('0.10')
    loss_model: Literal['static_initial_balance'] = 'static_initial_balance'

class RiskDecision(Contract):
    allowed: bool
    reasons: tuple[str, ...]
    risk_amount: NonNegative
    checked_at: AwareDatetime
    mode: Literal['paper'] = 'paper'
    executable: Literal[False] = False
