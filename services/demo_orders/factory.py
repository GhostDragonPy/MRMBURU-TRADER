"""Refuse LIVE wiring. Real DEMO sockets stay unconfigured until activation."""
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, DemoGuardError, validate_settings


class UnconfiguredDemoTransport:
    def __init__(self, settings):
        self.account_id = settings.demo_ctrader_account_id
        self.is_live = False
        self.host = DEMO_HOST
        self.environment = 'demo'
        self.scope = 'trading'

    def snapshot(self):
        raise DemoGuardError('DEMO transport is not activated')

    def submit_market(self, order):
        raise DemoGuardError('DEMO transport is not activated')

    def reconcile(self, signal_id):
        return None

    def close_position(self, position_id):
        raise DemoGuardError('DEMO transport is not activated')


def build_gateway(settings, redis_client=None):
    validate_settings(settings)
    if str(settings.demo_ctrader_account_id) in LIVE_ACCOUNT_IDS:
        raise DemoGuardError('LIVE account rejected')
    return DemoCTraderExecutionGateway(UnconfiguredDemoTransport(settings))
