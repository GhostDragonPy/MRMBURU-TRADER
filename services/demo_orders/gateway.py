"""DEMO-only execution adapter. Tests inject a fake transport; no sockets here."""
from services.demo_orders.guards import DEMO_HOST, DemoGuardError, reject_live_identity


class UncertainBrokerResult(RuntimeError):
    pass


class DemoCTraderExecutionGateway:
    def __init__(self, transport):
        self.transport = transport

    def _assert_demo(self):
        reject_live_identity(
            account_id=self.transport.account_id,
            is_live=self.transport.is_live,
            host=self.transport.host,
            environment=self.transport.environment,
            scope=self.transport.scope,
        )
        if self.transport.host != DEMO_HOST or self.transport.environment != 'demo':
            raise DemoGuardError('LIVE endpoint rejected')

    def snapshot(self):
        self._assert_demo()
        snap = self.transport.snapshot()
        if snap is None:
            raise DemoGuardError('Broker snapshot unavailable')
        for key in ('balance', 'equity', 'open_positions', 'open_orders'):
            if snap.get(key) is None:
                raise DemoGuardError('Broker snapshot unavailable')
        return snap

    def submit_market(self, order):
        self._assert_demo()
        if order.get('symbol') != 'EURUSD':
            raise DemoGuardError('Only EURUSD is allowed')
        if not order.get('stop_loss') or not order.get('take_profit'):
            raise DemoGuardError('SL/TP required')
        try:
            result = self.transport.submit_market(order)
        except TimeoutError as exc:
            raise UncertainBrokerResult('timeout') from exc
        if result.get('uncertain'):
            raise UncertainBrokerResult('uncertain broker response')
        return result

    def reconcile(self, signal_id):
        self._assert_demo()
        return self.transport.reconcile(signal_id)

    def close_owned(self, position_id, owned_ids):
        self._assert_demo()
        if str(position_id) not in {str(x) for x in owned_ids}:
            raise DemoGuardError('Refusing to manage a foreign position')
        return self.transport.close_position(position_id)
