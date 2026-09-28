"""Fail-closed prop-sim identity. LIVE infrastructure is allowed only for the FTMO tuple."""
from services.ctrader.prop_sim import (
    PROP_SIM_BROKER, PROP_SIM_CTID, PROP_SIM_LIVE_HOST, PROP_SIM_TRADER_LOGIN,
)
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, LIVE_HOST, DemoGuardError

PROP_SIM_HOST = PROP_SIM_LIVE_HOST


def validate_prop_sim_runtime(settings):
    if getattr(settings, 'allow_live_trading', False) is not False:
        raise DemoGuardError('ALLOW_LIVE_TRADING must remain false')
    if getattr(settings, 'execution_enabled', False) is not False:
        raise DemoGuardError('EXECUTION_ENABLED must remain false')
    if getattr(settings, 'trading_mode', 'paper') != 'prop-sim':
        raise DemoGuardError('PROP_SIM_MODE_REQUIRED')
    if not getattr(settings, 'esses_broker_execution', False):
        raise DemoGuardError('ESSES_BROKER_EXECUTION required')
    if not getattr(settings, 'prop_sim_acknowledged_live_environment', False):
        raise DemoGuardError('LIVE environment is not acknowledged')
    if getattr(settings, 'ctrader_environment', '') != 'live':
        raise DemoGuardError('LIVE host required for prop-sim')
    account = str(getattr(settings, 'prop_sim_ctrader_account_id', '') or '').strip()
    login = str(getattr(settings, 'prop_sim_trader_login', '') or '').strip()
    if account != PROP_SIM_CTID or login != PROP_SIM_TRADER_LOGIN:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')


def require_prop_sim_identity(*, account_id, trader_login=None, broker=None, is_live, host,
                              environment, scope):
    if str(account_id) != PROP_SIM_CTID:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
    if trader_login is not None and str(trader_login) != PROP_SIM_TRADER_LOGIN:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
    if broker is not None and str(broker).strip().upper() != PROP_SIM_BROKER:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
    if is_live is not True:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
    if host != PROP_SIM_HOST or host == DEMO_HOST or environment != 'live':
        raise DemoGuardError('LIVE host required for prop-sim')
    if scope != 'trading':
        raise DemoGuardError('Token scope accounts rejected')
    return True


def reject_non_prop_sim_live(*, account_id, trading_mode):
    if str(account_id) == PROP_SIM_CTID and trading_mode != 'prop-sim':
        raise DemoGuardError('LIVE account rejected')
    if str(account_id) in LIVE_ACCOUNT_IDS and trading_mode != 'prop-sim':
        raise DemoGuardError('LIVE account rejected')
    if str(account_id) != PROP_SIM_CTID and str(account_id) in LIVE_ACCOUNT_IDS:
        raise DemoGuardError('OTHER_LIVE_ACCOUNT_BLOCKED')
    return True
