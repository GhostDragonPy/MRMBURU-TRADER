"""Fail-closed DEMO-only gates. Never imports network clients."""
LIVE_ACCOUNT_IDS = frozenset({'48803059'})
DEMO_HOST = 'demo.ctraderapi.com'
LIVE_HOST = 'live.ctraderapi.com'
ROLLOUT_ORDER = ('disabled', 'shadow', 'canary', 'enabled')


class DemoGuardError(RuntimeError):
    pass


def validate_settings(settings):
    if getattr(settings, 'allow_live_trading', False) is not False:
        raise ValueError('ALLOW_LIVE_TRADING must remain false')
    if getattr(settings, 'execution_enabled', False) is not False:
        raise ValueError('EXECUTION_ENABLED must remain false')
    mode = getattr(settings, 'trading_mode', 'paper')
    if mode == 'paper':
        return
    if mode != 'demo-orders':
        raise ValueError('Unsupported trading mode')
    if not settings.demo_execution_enabled:
        raise ValueError('demo-orders requires DEMO_EXECUTION_ENABLED=true')
    if not settings.esses_broker_execution:
        raise ValueError('demo-orders requires ESSES_BROKER_EXECUTION=true')
    account = (settings.demo_ctrader_account_id or '').strip()
    if not account:
        raise ValueError('demo-orders requires DEMO_CTRADER_ACCOUNT_ID')
    if account in LIVE_ACCOUNT_IDS:
        raise ValueError('LIVE cTrader account is forbidden')
    if (settings.ctrader_account_id or '') in LIVE_ACCOUNT_IDS:
        raise ValueError('LIVE CTRADER_ACCOUNT_ID cannot be used with demo-orders')
    if settings.ctrader_environment != 'demo':
        raise ValueError('demo-orders requires CTRADER_ENVIRONMENT=demo')


def reject_live_identity(*, account_id, is_live, host, environment, scope):
    if str(account_id) in LIVE_ACCOUNT_IDS or is_live is True:
        raise DemoGuardError('LIVE account rejected')
    if environment != 'demo' or host != DEMO_HOST:
        raise DemoGuardError('LIVE endpoint rejected')
    if scope != 'trading':
        raise DemoGuardError('Token scope accounts rejected')
    return True


def next_rollout(current, target):
    if current not in ROLLOUT_ORDER or target not in ROLLOUT_ORDER:
        raise DemoGuardError('Invalid rollout state')
    if ROLLOUT_ORDER.index(target) - ROLLOUT_ORDER.index(current) != 1:
        raise DemoGuardError('Rollout must advance one step')
    return target
