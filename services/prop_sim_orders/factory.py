"""Wire OfficialPropSimTransport only when every prop-sim gate is already true."""
from services.ctrader.prop_sim import PROP_SIM_CTID
from services.demo_orders.guards import DemoGuardError
from services.prop_sim_orders.gateway import PropSimCTraderExecutionGateway
from services.prop_sim_orders.guards import validate_prop_sim_runtime
from services.prop_sim_orders.transport import OfficialPropSimTransport, UnconfiguredPropSimTransport


def official_conditions(settings, *, rollout, token_scope, db_session):
    if settings.trading_mode != 'prop-sim':
        return False
    if not settings.prop_sim_execution_enabled:
        return False
    if rollout not in ('canary', 'enabled'):
        return False
    if token_scope != 'trading':
        return False
    if str(settings.prop_sim_ctrader_account_id or '') != PROP_SIM_CTID:
        return False
    if settings.ctrader_environment != 'live':
        return False
    if db_session is None:
        return False
    try:
        from services.prop_sim_orders.preflight import require_verified_preflight
        require_verified_preflight(db_session, settings)
    except DemoGuardError:
        return False
    return True


def build_gateway(settings, redis_client=None, *, db_session=None, token_scope='trading',
                  protobuf_session=None, rollout=None):
    validate_prop_sim_runtime(settings)
    if rollout is None and db_session is not None:
        from services.prop_sim_orders.service import control
        rollout = control(db_session).rollout
    rollout = rollout or 'disabled'
    if not official_conditions(settings, rollout=rollout, token_scope=token_scope,
                               db_session=db_session):
        return PropSimCTraderExecutionGateway(UnconfiguredPropSimTransport(settings))
    if protobuf_session is None:
        return PropSimCTraderExecutionGateway(UnconfiguredPropSimTransport(settings))
    from services.ctrader.budget import RequestBudget
    budget = None
    if redis_client is not None:
        budget = RequestBudget(
            redis_client, f'prop-sim:{PROP_SIM_CTID}',
            settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute)
    transport = OfficialPropSimTransport(settings, session=protobuf_session,
                                         token_scope=token_scope, budget=budget)
    transport.name = 'official-prop-sim'
    return PropSimCTraderExecutionGateway(transport)


def open_shadow_session(settings, redis_client=None, *, demo=None, driver=None, budget=None):
    """Persistent LIVE socket for the FTMO tuple. Orders still require the barrier."""
    from services.ctrader.budget import RequestBudget
    from services.demo_orders.barrier import TradingMessageBarrier
    from services.prop_sim_orders.sdk_session import SdkPropSimSessionFactory
    if settings.trading_mode != 'prop-sim':
        return None
    barrier = TradingMessageBarrier.for_rollout(
        settings, demo, redis_client=redis_client, trading_permission='UNVERIFIED')
    if budget is None and redis_client is not None:
        budget = RequestBudget(
            redis_client, f'prop-sim:{PROP_SIM_CTID}',
            settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute)
    return SdkPropSimSessionFactory().open(
        settings, barrier=barrier, budget=budget, driver=driver, redis_client=redis_client)
