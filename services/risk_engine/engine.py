from datetime import datetime
from zoneinfo import ZoneInfo
from core.contracts import AccountState, MarketState, RiskDecision, RiskPolicy, Signal


def evaluate(signal: Signal, account: AccountState, market: MarketState,
             policy: RiskPolicy, *, killed: bool, now: datetime,
             risk_timezone: str = 'UTC') -> RiskDecision:
    """Pure deterministic gate. Missing/invalid facts fail validation before evaluation.

    Only scenario validation in v0.2: an approval is NEVER an execution permit.
    """
    if now.tzinfo is None:
        raise ValueError('Timezone-aware clock required')
    reasons = []
    if killed: reasons.append('GLOBAL_KILL_SWITCH')
    if not account.enabled: reasons.append('ACCOUNT_DISABLED')
    if not market.connected: reasons.append('DISCONNECTED')
    if not market.platform_ready: reasons.append('PLATFORM_NOT_READY')
    for name, stamp in [('SIGNAL', signal.created_at), ('ACCOUNT', account.as_of),
                        ('MARKET', market.as_of), ('NEWS', market.news_checked_at)]:
        age = (now-stamp).total_seconds()
        if age < 0 or age > policy.max_data_age_seconds:
            reasons.append(f'{name}_STALE_OR_FUTURE')
    if account.risk_day != now.astimezone(ZoneInfo(risk_timezone)).date():
        reasons.append('DAILY_BASELINE_NOT_ROLLED')
    if not market.news_known: reasons.append('NEWS_UNKNOWN')
    if any(abs((event-now).total_seconds()) <= policy.news_window_minutes*60
           for event in market.high_impact_events):
        reasons.append('HIGH_IMPACT_NEWS')
    risk = signal.risk_amount
    if risk > account.equity*policy.risk_per_trade: reasons.append('TRADE_RISK_LIMIT')
    if account.open_risk+risk > account.equity*policy.max_open_risk:
        reasons.append('OPEN_RISK_LIMIT')
    # Conservative projection: all reserved stop risk and costs could be lost.
    projected = account.equity-account.open_risk-risk
    if projected <= account.day_start_balance-account.initial_balance*policy.daily_loss_fraction:
        reasons.append('DAILY_LOSS_LIMIT')
    if projected <= account.initial_balance*(1-policy.total_loss_fraction):
        reasons.append('TOTAL_LOSS_LIMIT')
    if account.open_positions >= policy.max_positions: reasons.append('POSITION_LIMIT')
    if account.trades_today >= policy.max_trades_daily: reasons.append('DAILY_TRADE_LIMIT')
    if account.consecutive_losses >= policy.max_consecutive_losses:
        reasons.append('LOSING_STREAK_LIMIT')
    if market.spread_bps > policy.max_spread_bps: reasons.append('SPREAD_LIMIT')
    if market.slippage_bps > policy.max_slippage_bps: reasons.append('SLIPPAGE_LIMIT')
    if market.volatility > policy.max_volatility: reasons.append('VOLATILITY_LIMIT')
    return RiskDecision(allowed=not reasons, reasons=tuple(reasons), risk_amount=risk, checked_at=now)
