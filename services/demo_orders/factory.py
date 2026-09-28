"""Wire OfficialDemoTransport only when every DEMO gate is already true."""
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, DemoGuardError, validate_settings
from services.demo_orders.transport import OfficialDemoTransport


class UnconfiguredDemoTransport:
    def __init__(self, settings):
        self.account_id = settings.demo_ctrader_account_id
        self.is_live = False
        self.host = DEMO_HOST
        self.environment = 'demo'
        self.scope = 'trading'
        self.name = 'unconfigured'

    def snapshot(self):
        raise DemoGuardError('DEMO transport is not activated')

    def submit_market(self, order):
        raise DemoGuardError('DEMO transport is not activated')

    def reconcile(self, signal_id):
        return None

    def close_position(self, position_id):
        raise DemoGuardError('DEMO transport is not activated')

    def close(self):
        return None


def official_conditions(settings, *, rollout, token_scope, db_session):
    if settings.trading_mode != 'demo-orders':
        return False
    if not settings.demo_execution_enabled:
        return False
    if rollout not in ('canary', 'enabled'):
        return False
    if token_scope != 'trading':
        return False
    account = str(settings.demo_ctrader_account_id or '')
    if not account or account in LIVE_ACCOUNT_IDS:
        return False
    if settings.ctrader_environment != 'demo':
        return False
    if db_session is None:
        return False
    try:
        from services.demo_orders.preflight import require_verified_preflight
        require_verified_preflight(db_session, settings)
    except DemoGuardError:
        return False
    return True


def build_gateway(settings, redis_client=None, *, db_session=None, token_scope='trading',
                  protobuf_session=None, rollout=None):
    validate_settings(settings)
    if str(settings.demo_ctrader_account_id or '') in LIVE_ACCOUNT_IDS:
        raise DemoGuardError('LIVE account rejected')
    if rollout is None and db_session is not None:
        from services.demo_orders.service import control
        rollout = control(db_session).rollout
    rollout = rollout or 'disabled'
    if not official_conditions(settings, rollout=rollout, token_scope=token_scope,
                               db_session=db_session):
        return DemoCTraderExecutionGateway(UnconfiguredDemoTransport(settings))
    if protobuf_session is None:
        return DemoCTraderExecutionGateway(UnconfiguredDemoTransport(settings))
    from services.ctrader.budget import RequestBudget
    budget = None
    if redis_client is not None:
        budget = RequestBudget(redis_client, settings.demo_ctrader_account_id,
                               settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute)
    transport = OfficialDemoTransport(settings, session=protobuf_session,
                                      token_scope=token_scope, budget=budget)
    transport.name = 'official-demo'
    return DemoCTraderExecutionGateway(transport)


def open_shadow_session(settings, redis_client=None, *, demo=None, driver=None, budget=None):
    """Persistent DEMO socket for shadow/canary/enabled. Orders still require the barrier."""
    from services.ctrader.budget import RequestBudget
    from services.demo_orders.barrier import TradingMessageBarrier
    from services.demo_orders.sdk_session import SdkDemoSessionFactory
    if settings.trading_mode != 'demo-orders':
        return None
    barrier = TradingMessageBarrier.for_rollout(
        settings, demo, redis_client=redis_client, trading_permission='UNVERIFIED')
    if budget is None and redis_client is not None:
        budget = RequestBudget(redis_client, settings.demo_ctrader_account_id,
                               settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute)
    return SdkDemoSessionFactory().open(
        settings, barrier=barrier, budget=budget, driver=driver, redis_client=redis_client)
