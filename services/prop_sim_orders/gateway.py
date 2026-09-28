"""Prop-sim execution adapter. LIVE host is allowed only for the FTMO tuple."""
from services.demo_orders.gateway import UncertainBrokerResult
from services.demo_orders.guards import DemoGuardError
from services.prop_sim_orders.guards import require_prop_sim_identity


class PropSimCTraderExecutionGateway:
    def __init__(self, transport):
        self.transport = transport

    def _assert_prop_sim(self):
        require_prop_sim_identity(
            account_id=self.transport.account_id,
            trader_login=getattr(self.transport, 'trader_login', None),
            broker=getattr(self.transport, 'broker', None),
            is_live=self.transport.is_live,
            host=self.transport.host,
            environment=self.transport.environment,
            scope=self.transport.scope,
        )

    def snapshot(self):
        self._assert_prop_sim()
        snap = self.transport.snapshot()
        if snap is None:
            raise DemoGuardError('Broker snapshot unavailable')
        for key in ('balance', 'equity', 'open_positions', 'open_orders'):
            if snap.get(key) is None:
                raise DemoGuardError('Broker snapshot unavailable')
        return snap

    def submit_market(self, order):
        self._assert_prop_sim()
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
        self._assert_prop_sim()
        return self.transport.reconcile(signal_id)

    def close_owned(self, position_id, owned_ids):
        self._assert_prop_sim()
        if str(position_id) not in {str(x) for x in owned_ids}:
            raise DemoGuardError('Refusing to manage a foreign position')
        return self.transport.close_position(position_id)
