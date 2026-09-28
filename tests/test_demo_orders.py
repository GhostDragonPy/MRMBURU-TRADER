from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from core.models import DemoControl, DemoOrderIntent, KillSwitch
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from services.demo_orders.guards import DemoGuardError, next_rollout, reject_live_identity
from services.demo_orders import service
from services.execution_engine.gateway import ExecutionDisabled, ExecutionGateway
from tests.test_api import settings
from tests.test_discord_control import HEADERS, API_KEY
from fastapi.testclient import TestClient
from apps.api.main import create_app
from tests.test_api import Cache

NOW = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)


def demo_settings(**kw):
    values = dict(
        trading_mode='demo-orders', demo_execution_enabled=True,
        esses_broker_execution=True, allow_live_trading=False,
        demo_ctrader_account_id='1001', ctrader_environment='demo',
        ctrader_account_id='')
    values.update(kw)
    return settings(**values)


class FakeTransport:
    def __init__(self, *, is_live=False, host='demo.ctraderapi.com', environment='demo',
                 scope='trading', account_id='1001', timeout=False, sl_confirmed=True,
                 open_positions=0):
        self.is_live = is_live
        self.host = host
        self.environment = environment
        self.scope = scope
        self.account_id = account_id
        self.timeout = timeout
        self.sl_confirmed = sl_confirmed
        self.open_positions = open_positions
        self.submits = []
        self.reconcile_map = {}

    def snapshot(self):
        return {
            'balance': '100000', 'equity': '100000', 'open_positions': self.open_positions,
            'open_orders': 0, 'day_start_balance': '100000', 'initial_balance': '100000',
            'instrument': {'min_volume': '1000', 'max_volume': '1000000',
                           'step_volume': '1000', 'lot_size': '100000'},
        }

    def submit_market(self, order):
        self.submits.append(order)
        if self.timeout:
            raise TimeoutError('broker timeout')
        return {
            'order_id': 'o-%s' % len(self.submits),
            'position_id': 'p-%s' % len(self.submits),
            'fill_price': '1.10',
            'stop_loss': order['stop_loss'],
            'take_profit': order['take_profit'],
            'sl_confirmed': self.sl_confirmed,
            'status': 'ORDER_FILLED',
        }

    def reconcile(self, signal_id):
        return self.reconcile_map.get(signal_id)

    def close_position(self, position_id):
        return {'closed': position_id}


def opened_event(risk='20', units='1000'):
    return {'kind': 'opened', 'position': {
        'side': 'buy', 'units': units, 'entry': '1.10', 'stop': '1.09', 'target': '1.13',
        'risk': risk, 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-a'},
    }}


def run(session, settings, events, transport, kill=False, rollout='enabled', reset=True, **kw):
    seed_preflight = kw.pop('seed_preflight', True)
    session.get(KillSwitch, 1).active = kill
    demo = service.control(session)
    demo.rollout = rollout
    if reset:
        demo.blocked = False
        demo.protection_failed = False
        demo.canary_consumed = False
    if rollout in ('canary', 'enabled') and seed_preflight:
        if demo.armed_at is None:
            demo.armed_at = NOW - timedelta(seconds=5)
        from services.demo_orders.preflight import record_preflight
        record_preflight(session, settings, now=NOW - timedelta(seconds=5), ttl_seconds=86400)
    gw = DemoCTraderExecutionGateway(transport)
    return service.on_paper_cycle(session, settings, {'events': events}, now=NOW,
                                  gateway=gw, token_scope=transport.scope, **kw)


def test_paper_mode_default_and_live_modes_still_locked():
    s = settings()
    assert s.trading_mode == 'paper'
    assert s.allow_live_trading is False
    with pytest.raises(ValidationError):
        settings(trading_mode='live')
    with pytest.raises(ValidationError):
        settings(allow_live_trading=True)
    with pytest.raises(ValidationError):
        settings(trading_mode='demo-orders', demo_execution_enabled=True,
                 esses_broker_execution=True, demo_ctrader_account_id='48803059',
                 ctrader_environment='demo')
    with pytest.raises(ValidationError):
        settings(trading_mode='demo-orders', demo_execution_enabled=True,
                 esses_broker_execution=True, demo_ctrader_account_id='1001',
                 ctrader_environment='live')
    with pytest.raises(ValidationError):
        demo_settings(ctrader_account_id='48803059')


def test_reject_live_account_and_endpoint_and_accounts_scope():
    with pytest.raises(DemoGuardError, match='LIVE account'):
        reject_live_identity(account_id='48803059', is_live=False, host='demo.ctraderapi.com',
                             environment='demo', scope='trading')
    with pytest.raises(DemoGuardError, match='LIVE account'):
        reject_live_identity(account_id='1', is_live=True, host='demo.ctraderapi.com',
                             environment='demo', scope='trading')
    with pytest.raises(DemoGuardError, match='LIVE endpoint'):
        reject_live_identity(account_id='1', is_live=False, host='live.ctraderapi.com',
                             environment='live', scope='trading')
    with pytest.raises(DemoGuardError, match='accounts'):
        reject_live_identity(account_id='1', is_live=False, host='demo.ctraderapi.com',
                             environment='demo', scope='accounts')
    assert reject_live_identity(account_id='1001', is_live=False, host='demo.ctraderapi.com',
                                environment='demo', scope='trading')


def test_token_trading_demo_accepted(factory):
    cfg = demo_settings()
    transport = FakeTransport(scope='trading')
    with factory.begin() as session:
        out = run(session, cfg, [opened_event()], transport)
    assert out['results'][0]['status'] == 'filled'
    assert len(transport.submits) == 1
    assert ExecutionGateway().submit is not None
    with pytest.raises(ExecutionDisabled):
        ExecutionGateway().submit({'approved': True})


def test_token_accounts_rejected(factory):
    cfg = demo_settings()
    with factory.begin() as session:
        out = run(session, cfg, [opened_event()], FakeTransport(scope='accounts'))
        assert 'accounts' in out['blocked']
        assert session.get(KillSwitch, 1).active is False
        assert session.get(DemoControl, 1).blocked is True


def test_live_transport_rejected_without_submit(factory):
    cfg = demo_settings()
    transport = FakeTransport(is_live=True)
    with factory.begin() as session:
        out = run(session, cfg, [opened_event()], transport)
    assert out['blocked']
    assert transport.submits == []


def test_shadow_does_not_send(factory):
    cfg = demo_settings()
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, cfg, [opened_event()], transport, rollout='shadow')
    assert out['results'][0]['status'] == 'shadow'
    assert transport.submits == []


def test_disabled_sends_nothing(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport, rollout='disabled')
    assert out['skipped'] == 'disabled'
    assert transport.submits == []


def test_cannot_skip_disabled_to_enabled(factory):
    with factory.begin() as session:
        service.control(session)
        with pytest.raises(DemoGuardError, match='one step'):
            service.advance_rollout(session, 'enabled', confirmed=True)
        service.advance_rollout(session, 'shadow', confirmed=True)
        service.advance_rollout(session, 'canary', confirmed=True)
        assert service.advance_rollout(session, 'enabled', confirmed=True)['rollout'] == 'enabled'
        with pytest.raises(DemoGuardError):
            service.advance_rollout(session, 'enabled', confirmed=False)


def test_canary_uses_min_volume_once(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        first = run(session, demo_settings(), [opened_event(units='5000')], transport, rollout='canary')
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '5000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-canary-2'},
        }}
        second = run(session, demo_settings(), [other], transport, rollout='canary')
    assert first['results'][0]['status'] == 'filled'
    assert transport.submits[0]['volume'] == '1000'
    assert second['results'][0]['status'] == 'failed'


def test_duplicate_signal_sends_once(factory):
    transport = FakeTransport()
    events = [opened_event()]
    with factory.begin() as session:
        run(session, demo_settings(), events, transport)
        again = run(session, demo_settings(), events, transport)
    assert len(transport.submits) == 1
    assert again['results'][0]['duplicate'] is True


def test_restart_does_not_duplicate(factory):
    transport = FakeTransport()
    events = [opened_event()]
    with factory.begin() as session:
        run(session, demo_settings(), events, transport)
    with factory.begin() as session:
        again = run(session, demo_settings(), events, transport)
    assert len(transport.submits) == 1
    assert again['results'][0]['duplicate'] is True


def test_timeout_unknown_emergency_stop(factory):
    transport = FakeTransport(timeout=True)
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport)
        assert out['results'][0]['status'] == 'uncertain'
        assert out['results'][0]['reason'] == 'UNKNOWN'
        assert service.control(session).blocked is True
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '1000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-unknown'},
        }}
        second = run(session, demo_settings(), [other], transport, reset=False)
    assert second['blocked'] == 'UNKNOWN'
    assert len(transport.submits) == 1


def test_timeout_reconciles_without_blind_resend(factory):
    transport = FakeTransport(timeout=True)
    sid_payload = {
        'account': '1001', 'setup_id': 'setup-a', 'side': 'buy', 'stop': '1.09',
        'target': '1.13', 'bar': NOW.isoformat(), 'day': NOW.date().isoformat(),
    }
    signal_id = service.signal_id_for(sid_payload)
    transport.reconcile_map[signal_id] = {
        'order_id': 'rec-1', 'position_id': 'p-rec', 'fill_price': '1.10',
        'stop_loss': '1.09', 'take_profit': '1.13', 'sl_confirmed': True,
    }
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport)
    assert out['results'][0]['status'] == 'filled'
    assert len(transport.submits) == 1


def test_daily_two_trade_cap(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        run(session, demo_settings(), [opened_event()], transport)
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '1000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-b'},
        }}
        run(session, demo_settings(), [other], transport)
        third = {'kind': 'opened', 'position': {
            'side': 'buy', 'units': '1000', 'entry': '1.10', 'stop': '1.09', 'target': '1.13',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-c'},
        }}
        out = run(session, demo_settings(), [third], transport)
    assert len(transport.submits) == 2
    assert out['results'][0]['reason'] == 'DAILY_TRADE_LIMIT'


def test_risk_above_quarter_percent_rejected(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event(risk='300')], transport)
    assert out['results'][0]['reason'] == 'TRADE_RISK_LIMIT'
    assert transport.submits == []


def test_existing_position_blocks(factory):
    transport = FakeTransport(open_positions=1)
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport)
    assert out['results'][0]['reason'] == 'POSITION_LIMIT'


def test_outside_window_blocked(factory):
    transport = FakeTransport()
    night = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
    with factory.begin() as session:
        session.get(KillSwitch, 1).active = False
        demo = service.control(session)
        demo.rollout = 'enabled'
        demo.armed_at = night - timedelta(seconds=5)
        out = service.on_paper_cycle(session, demo_settings(), {'events': [opened_event()]},
                                     now=night, gateway=DemoCTraderExecutionGateway(transport),
                                     token_scope='trading')
    assert out['blocked'] == 'outside_esses_window'
    assert transport.submits == []


def test_kill_switch_blocks(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport, kill=True)
    assert out['blocked'] == 'GLOBAL_KILL_SWITCH'


def test_missing_sl_blocked(factory):
    transport = FakeTransport()
    event = opened_event()
    event['position']['stop'] = ''
    with factory.begin() as session:
        out = run(session, demo_settings(), [event], transport)
    assert out['results'][0]['reason'] == 'SL_TP_REQUIRED'


def test_unconfirmed_sl_blocks_new_entries(factory):
    transport = FakeTransport(sl_confirmed=False)
    with factory.begin() as session:
        first = run(session, demo_settings(), [opened_event()], transport)
        assert first['results'][0]['reason'] == 'SL_UNCONFIRMED'
        transport.sl_confirmed = True
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '1000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-b'},
        }}
        second = run(session, demo_settings(), [other], transport, reset=False)
    assert second['blocked']


def test_paper_cycle_still_works(factory):
    from tests.test_esses import test_position_management_survives_missing_history_and_news
    test_position_management_survives_missing_history_and_news(factory)


def test_discord_cannot_enable_live(factory):
    from tests.test_api import ADMIN
    configured = settings(discord_api_key=API_KEY)
    with TestClient(create_app(configured, factory, Cache())) as client:
        r = client.post('/internal/discord/demo-rollout', headers=HEADERS,
                        json={'interaction_id': 'x1', 'target': 'live', 'confirmed': True,
                              'reason': 'nope', 'allow_live_trading': True})
        assert r.status_code == 403
        status = client.get('/internal/discord/demo-status', headers=HEADERS).json()
        assert status['allow_live_trading'] is False
        assert status['rollout'] == 'disabled'
        stop = client.post('/internal/discord/demo-emergency-stop', headers=HEADERS,
                           json={'interaction_id': 'x2', 'reason': 'halt demo'})
        assert stop.status_code == 200
        assert stop.json()['positions_closed'] is False


def test_foreign_position_cannot_be_closed():
    gw = DemoCTraderExecutionGateway(FakeTransport())
    with pytest.raises(DemoGuardError, match='foreign'):
        gw.close_owned('999', owned_ids={'1'})


def test_rollout_order():
    assert next_rollout('disabled', 'shadow') == 'shadow'
    with pytest.raises(DemoGuardError):
        next_rollout('disabled', 'enabled')
