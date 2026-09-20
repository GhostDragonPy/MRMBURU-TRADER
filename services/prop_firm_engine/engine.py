from core.contracts import AccountState, PropRules, Signal


def evaluate(signal: Signal, account: AccountState, rules: PropRules) -> tuple[str, ...]:
    """Generic static-balance simulation. Never claims FTMO compliance."""
    reasons = []
    projected = account.equity-account.open_risk-signal.risk_amount
    if projected <= account.day_start_balance-account.initial_balance*rules.daily_loss_fraction:
        reasons.append('PROP_DAILY_LOSS_LIMIT')
    if projected <= account.initial_balance*(1-rules.total_loss_fraction):
        reasons.append('PROP_TOTAL_LOSS_LIMIT')
    if account.balance >= account.initial_balance*(1+rules.profit_target_fraction):
        reasons.append('PROP_TARGET_REACHED_REVIEW_REQUIRED')
    return tuple(reasons)
