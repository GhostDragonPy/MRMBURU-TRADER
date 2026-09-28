from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest

from core.models import KillSwitch
from services.demo_orders import service
from services.demo_orders.factory import UnconfiguredDemoTransport, build_gateway
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from services.demo_orders.guards import DEMO_HOST, DemoGuardError
from services.demo_orders.transport import (
    LABEL, OfficialDemoTransport, client_order_id, normalize_volume, sanitize,
)
from tests.test_demo_orders import NOW, demo_settings, opened_event


class FakeDemoSession:
    def __init__(self, *, host=DEMO_HOST, environment='demo',
                 permission_scope='trading', accounts=None, timeout=False,
                 sl_confirmed=True, amend_confirms=True, foreign_label='OTHER'):
        self.host = host
        self.environment = environment
        self.permission_scope = permission_scope
        self.accounts = accounts if accounts is not None else [
            {'ctidTraderAccountId': 1001, 'isLive': False},
        ]
        self.timeout = timeout
        self.sl_confirmed = sl_confirmed
        self.amend_confirms = amend_confirms
        self.foreign_label = foreign_label
        self.new_orders = []
        self.reconcile_calls = []
        self.closes = []
        self.amends = []
        self.closed = False
        self.store = {}
        self.positions = {}
        self.seq = 0
        self.instrument = {
            'symbol_id': 1, 'min_volume': '1000', 'max_volume': '1000000',
            'step_volume': '1000', 'lot_size': '100000',
        }

    def authenticate(self, **kwargs):
        assert kwargs.get('access_token') == '[injected]'
        return {
            'permission_scope': self.permission_scope,
            'accounts': self.accounts,
            'host': self.host,
        }

    def snapshot(self, account_id):
        return {
            'balance': '100000', 'equity': '100000', 'open_positions': len(self.positions),
            'open_orders': 0, 'instrument': self.instrument,
            'day_start_balance': '100000', 'initial_balance': '100000',
        }

    def symbol(self, name, account_id):
        assert name == 'EURUSD'
        return self.instrument

    def new_order(self, account_id, request):
        self.new_orders.append(request)
        if self.timeout:
            self.timeout = False
            self.seq += 1
            self.store[request['client_order_id']] = {
                'order_id': f'o-{self.seq}',
                'position_id': f'p-{self.seq}',
                'fill_price': '1.10',
                'stop_loss': request['stop_loss'],
                'take_profit': request['take_profit'],
                'sl_confirmed': True,
                'tp_confirmed': True,
                'status': 'ORDER_FILLED',
                'label': request['label'],
                'client_order_id': request['client_order_id'],
                'volume': request['volume'],
            }
            raise TimeoutError('broker timeout')
        self.seq += 1
        fill = {
            'order_id': f'o-{self.seq}',
            'position_id': f'p-{self.seq}',
            'fill_price': '1.10',
            'stop_loss': request['stop_loss'] if self.sl_confirmed else '',
            'take_profit': request['take_profit'] if self.sl_confirmed else '',
            'sl_confirmed': self.sl_confirmed,
            'tp_confirmed': self.sl_confirmed,
            'status': 'ORDER_FILLED',
            'label': request['label'],
            'client_order_id': request['client_order_id'],
            'volume': request['volume'],
        }
        self.store[request['client_order_id']] = fill
        self.positions[fill['position_id']] = fill
        return fill

    def find_by_client_order_id(self, account_id, cid):
        self.reconcile_calls.append(cid)
        return self.store.get(cid)

    def amend_sl_tp(self, account_id, position_id, stop_loss, take_profit):
        self.amends.append(position_id)
        ok = self.amend_confirms
        if position_id in self.positions:
            self.positions[position_id]['stop_loss'] = stop_loss
            self.positions[position_id]['take_profit'] = take_profit
            self.positions[position_id]['sl_confirmed'] = ok
        return {'sl_confirmed': ok, 'stop_loss': stop_loss, 'take_profit': take_profit}

    def position(self, account_id, position_id):
        row = self.positions.get(str(position_id))
        if row is None:
            return {'label': self.foreign_label, 'volume': 1000}
        return row

    def close_position(self, account_id, position_id, volume):
        self.closes.append(position_id)
        return {'status': 'ORDER_FILLED'}

    def close(self):
        self.closed = True


def transport(session=None, **kw):
    session = session or FakeDemoSession()
    budget = kw.pop('budget', Mock())
    token_scope = kw.pop('token_scope', 'trading')
    return OfficialDemoTransport(
        demo_settings(**kw), session=session, token_scope=token_scope, budget=budget)


def run_official(db, t, events=None, rollout='enabled'):
    db.get(KillSwitch, 1).active = False
    demo = service.control(db)
    demo.rollout = rollout
    demo.blocked = False
    demo.protection_failed = False
    return service.on_paper_cycle(
        db, demo_settings(), {'events': events or [opened_event()]},
        now=NOW, gateway=DemoCTraderExecutionGateway(t), token_scope='trading')


def test_live_account_rejected_before_any_order():
    session = FakeDemoSession(accounts=[{'ctidTraderAccountId': 1001, 'isLive': True}])
    t = transport(session)
    with pytest.raises(DemoGuardError, match='LIVE account'):
        t.connect()
    assert session.new_orders == []


def test_forbidden_account_48803059_rejected():
    from pydantic import ValidationError
    from types import SimpleNamespace
    with pytest.raises(ValidationError, match='LIVE'):
        demo_settings(demo_ctrader_account_id='48803059')
    with pytest.raises(DemoGuardError, match='LIVE account'):
        OfficialDemoTransport(
            SimpleNamespace(
                demo_ctrader_account_id='48803059', ctrader_account_id='',
                ctrader_environment='demo', allow_live_trading=False,
                execution_enabled=False, ctrader_client_id=None,
                ctrader_client_secret=None),
            session=FakeDemoSession(), token_scope='trading', budget=Mock())


def test_live_host_rejected():
    session = FakeDemoSession(host='live.ctraderapi.com', environment='live')
    with pytest.raises(DemoGuardError, match='LIVE endpoint'):
        OfficialDemoTransport(demo_settings(), session=session, token_scope='trading', budget=Mock())
    assert session.new_orders == []


def test_accounts_scope_token_rejected():
    with pytest.raises(DemoGuardError, match='accounts'):
        transport(FakeDemoSession(permission_scope='accounts'), token_scope='accounts')


def test_demo_trading_token_accepted():
    t = transport()
    listed = t.connect()
    assert listed['permission_scope'] == 'trading'
    assert t.host == DEMO_HOST
    assert t.is_live is False


def test_open_sends_exactly_one_new_order():
    session = FakeDemoSession()
    t = transport(session)
    t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-1',
    })
    assert len(session.new_orders) == 1
    assert session.new_orders[0]['label'] == LABEL
    assert session.new_orders[0]['client_order_id'] == client_order_id('sig-1')


def test_concurrent_duplicate_sends_once():
    session = FakeDemoSession()
    t = transport(session)
    order = {
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-dup',
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: t.submit_market(order), [1, 2]))
    assert len(session.new_orders) == 1
    assert results[0]['order_id'] == results[1]['order_id']


def test_timeout_reconciles_before_retry():
    session = FakeDemoSession(timeout=True)
    t = transport(session)
    result = t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-to',
    })
    assert result['order_id'] == 'o-1'
    assert len(session.new_orders) == 1
    t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-to',
    })
    assert len(session.new_orders) == 1


def test_volume_normalized_to_step():
    assert normalize_volume('2500', {
        'min_volume': '1000', 'step_volume': '1000', 'max_volume': '1000000'}) == 2000
    session = FakeDemoSession()
    t = transport(session)
    t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '2500',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-vol',
    })
    assert session.new_orders[0]['volume'] == 2000


def test_sl_tp_confirmed_by_broker():
    session = FakeDemoSession(sl_confirmed=True)
    t = transport(session)
    result = t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-sl',
    })
    assert result['sl_confirmed'] is True
    assert result['stop_loss'] == '1.09'
    assert result['take_profit'] == '1.13'


def test_sl_failure_activates_emergency_stop(factory):
    session = FakeDemoSession(sl_confirmed=False, amend_confirms=False)
    t = transport(session)
    with factory.begin() as db:
        first = run_official(db, t)
        assert first['results'][0]['reason'] == 'SL_UNCONFIRMED'
        assert service.control(db).blocked is True
        assert service.control(db).protection_failed is True
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '1000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-b'},
        }}
        second = service.on_paper_cycle(
            db, demo_settings(), {'events': [other]}, now=NOW,
            gateway=DemoCTraderExecutionGateway(t), token_scope='trading')
        assert second.get('blocked')
    assert len(session.new_orders) == 1


def test_close_only_mrmburu_positions():
    session = FakeDemoSession()
    t = transport(session)
    gw = DemoCTraderExecutionGateway(t)
    t.connect()
    with pytest.raises(DemoGuardError, match='foreign'):
        gw.close_owned('999', owned_ids={'999'})
    filled = t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-own',
    })
    gw.close_owned(filled['position_id'], owned_ids={filled['position_id']})
    assert session.closes == [filled['position_id']]


def test_restart_does_not_duplicate_new_order():
    session = FakeDemoSession()
    t = transport(session)
    order = {
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-rst',
    }
    first = t.submit_market(order)
    restarted = transport(session)
    second = restarted.submit_market(order)
    assert first['order_id'] == second['order_id']
    assert len(session.new_orders) == 1


def test_factory_still_unconfigured():
    gw = build_gateway(demo_settings())
    assert isinstance(gw.transport, UnconfiguredDemoTransport)
    with pytest.raises(DemoGuardError, match='not activated'):
        gw.submit_market({
            'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
            'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'x',
        })


def test_sanitize_strips_tokens():
    cleaned = sanitize({'access_token': 'abc', 'order_id': '1'})
    assert 'access_token' not in cleaned
    assert cleaned['order_id'] == '1'


def test_request_budget_required():
    session = FakeDemoSession()
    t = OfficialDemoTransport(demo_settings(), session=session, token_scope='trading', budget=None)
    with pytest.raises(DemoGuardError, match='RequestBudget'):
        t.connect()


def test_zero_real_socket_use(monkeypatch):
    connect = Mock(side_effect=AssertionError('network'))
    monkeypatch.setattr('socket.create_connection', connect)
    session = FakeDemoSession()
    t = transport(session)
    t.connect()
    t.submit_market({
        'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
        'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'sig-net',
    })
    t.close()
    connect.assert_not_called()
    assert session.closed is True


def test_paper_path_unchanged(factory):
    from tests.test_esses import test_position_management_survives_missing_history_and_news
    test_position_management_survives_missing_history_and_news(factory)
