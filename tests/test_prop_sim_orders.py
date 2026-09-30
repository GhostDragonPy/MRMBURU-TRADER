from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from core.models import KillSwitch, PropSimControl, PropSimOrderIntent
from services.demo_orders.guards import DemoGuardError
from services.prop_sim_orders.gateway import PropSimCTraderExecutionGateway
from services.prop_sim_orders.guards import require_prop_sim_identity
from services.prop_sim_orders import service
from tests.test_api import Cache, settings
from tests.test_discord_control import HEADERS, API_KEY
from fastapi.testclient import TestClient
from apps.api.main import create_app

NOW = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)


def prop_sim_settings(**kw):
    values = dict(
        trading_mode='prop-sim', esses_broker_execution=True,
        prop_sim_execution_enabled=True, prop_sim_acknowledged_live_environment=True,
        prop_sim_ctrader_account_id='48803059', prop_sim_trader_login='17204978',
        prop_sim_allowed_account_ids='48803059', ctrader_environment='live',
        allow_live_trading=False, execution_enabled=False, ctrader_account_id='48803059',
    )
    values.update(kw)
    return settings(**values)


class FakeTransport:
    def __init__(self, *, is_live=True, host='live.ctraderapi.com', environment='live',
                 scope='trading', account_id='48803059', timeout=False, sl_confirmed=True,
                 open_positions=0):
        self.is_live = is_live
        self.host = host
        self.environment = environment
        self.scope = scope
        self.account_id = account_id
        self.trader_login = '17204978'
        self.broker = 'FTMO'
        self.timeout = timeout
        self.sl_confirmed = sl_confirmed
        self.open_positions = open_positions
        self.submits = []
        self.reconcile_map = {}
        self.session = self
        self.closed_ids = []

    def quote(self, symbol_id):
        return {'bid': '1.10000', 'ask': '1.10010'}

    def snapshot(self):
        return {
            'balance': '100000', 'equity': '100000', 'open_positions': self.open_positions,
            'open_orders': 0, 'day_start_balance': '100000', 'initial_balance': '100000',
            'instrument': {
                'symbol_id': 1, 'min_volume': '1000', 'max_volume': '1000000',
                'step_volume': '1000', 'lot_size': '100000', 'digits': 5, 'pip_position': 4,
            },
        }

    def submit_market(self, order):
        self.submits.append(order)
        if self.timeout:
            raise TimeoutError('broker timeout')
        self.open_positions = 1
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
        self.open_positions = 0
        self.closed_ids.append(position_id)
        return {'closed': position_id}


def opened_event(risk='20', units='1000'):
    return {'kind': 'opened', 'position': {
        'side': 'buy', 'units': units, 'entry': '1.10', 'stop': '1.09', 'target': '1.13',
        'risk': risk, 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-a'},
    }}


def run(session, cfg, events, transport, kill=False, rollout='enabled', reset=True, **kw):
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
        from services.prop_sim_orders.preflight import record_preflight
        record_preflight(session, cfg, now=NOW - timedelta(seconds=5), ttl_seconds=86400)
    gw = PropSimCTraderExecutionGateway(transport)
    return service.on_paper_cycle(session, cfg, {'events': events}, now=NOW,
                                  gateway=gw, token_scope=transport.scope, **kw)


def test_prop_sim_mode_requires_tuple_and_keeps_generic_live_locked():
    s = prop_sim_settings()
    assert s.trading_mode == 'prop-sim'
    assert s.allow_live_trading is False
    assert s.execution_enabled is False
    assert s.prop_sim_execution_enabled is True
    with pytest.raises(ValidationError):
        settings(allow_live_trading=True)
    with pytest.raises(ValidationError):
        settings(execution_enabled=True)
    with pytest.raises(ValidationError):
        prop_sim_settings(prop_sim_ctrader_account_id='999')
    with pytest.raises(ValidationError):
        prop_sim_settings(prop_sim_trader_login='1')
    with pytest.raises(ValidationError):
        prop_sim_settings(prop_sim_acknowledged_live_environment=False)
    with pytest.raises(ValidationError):
        prop_sim_settings(ctrader_environment='demo')
    with pytest.raises(ValidationError):
        prop_sim_settings(esses_broker_execution=False)


def test_48803059_cannot_execute_outside_prop_sim():
    with pytest.raises(ValidationError):
        settings(trading_mode='demo-orders', demo_execution_enabled=True,
                 esses_broker_execution=True, demo_ctrader_account_id='48803059',
                 ctrader_environment='demo')
    with pytest.raises(DemoGuardError, match='PROP_SIM_TUPLE_MISMATCH'):
        require_prop_sim_identity(
            account_id='1', trader_login='17204978', broker='FTMO', is_live=True,
            host='live.ctraderapi.com', environment='live', scope='trading')


def test_other_live_identity_rejected():
    with pytest.raises(DemoGuardError, match='PROP_SIM_TUPLE_MISMATCH'):
        require_prop_sim_identity(
            account_id='48803059', trader_login='999', broker='FTMO', is_live=True,
            host='live.ctraderapi.com', environment='live', scope='trading')
    with pytest.raises(DemoGuardError, match='LIVE host required'):
        require_prop_sim_identity(
            account_id='48803059', trader_login='17204978', broker='FTMO', is_live=True,
            host='demo.ctraderapi.com', environment='demo', scope='trading')


def test_same_signal_id_for_paper_and_prop_sim():
    event = opened_event()
    payload = service.esses_signal_payload(event['position'], NOW)
    assert payload['side'] == 'buy'
    assert payload['stop'] == '1.09'
    assert payload['target'] == '1.13'
    assert 'account' not in payload
    assert service.signal_id_for(payload) == service.signal_id_for(payload)


def test_shadow_does_not_send(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [opened_event()], transport, rollout='shadow')
    assert out['results'][0]['status'] == 'shadow'
    assert transport.submits == []


def test_disabled_sends_nothing(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [opened_event()], transport, rollout='disabled')
    assert out['skipped'] == 'disabled'
    assert transport.submits == []


def test_enabled_sends_one_order_with_sl_tp(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [opened_event()], transport)
        intent = session.get(PropSimOrderIntent, out['results'][0]['signal_id'])
    assert out['results'][0]['status'] == 'filled'
    assert len(transport.submits) == 1
    assert transport.submits[0]['stop_loss'] == '1.09'
    assert transport.submits[0]['take_profit'] == '1.13'
    assert intent.status == 'filled'


def test_never_increase_volume_to_minimum(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [opened_event(units='500')], transport)
    assert out['results'][0]['reason'] == 'volume below minimum'
    assert transport.submits == []


def test_canary_uses_min_volume_without_raising_risk(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        first = run(session, prop_sim_settings(), [opened_event(units='5000')], transport,
                    rollout='canary')
        other = {'kind': 'opened', 'position': {
            'side': 'sell', 'units': '5000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
            'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-canary-2'},
        }}
        second = run(session, prop_sim_settings(), [other], transport, rollout='canary')
    assert first['results'][0]['status'] == 'filled'
    assert transport.submits[0]['volume'] == '1000'
    assert second['results'][0]['status'] == 'failed'


def test_canary_consumed_survives_restart(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        run(session, prop_sim_settings(), [opened_event()], transport, rollout='canary')
        assert session.get(PropSimControl, 1).canary_consumed is True
    other = {'kind': 'opened', 'position': {
        'side': 'sell', 'units': '5000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
        'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-canary-2'},
    }}
    with factory.begin() as session:
        again = run(session, prop_sim_settings(), [other], transport, rollout='canary',
                    reset=False)
    assert again['results'][0]['status'] == 'failed'
    assert len(transport.submits) == 1


def test_timeout_unknown_emergency_stop_keeps_paper(factory):
    transport = FakeTransport(timeout=True)
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [opened_event()], transport)
        assert out['results'][0]['reason'] == 'UNKNOWN'
        assert service.control(session).blocked is True
        stopped = service.emergency_stop(session, 'UNKNOWN')
        assert stopped['paper_available'] is True


def test_duplicate_signal_sends_once(factory):
    transport = FakeTransport()
    events = [opened_event()]
    with factory.begin() as session:
        run(session, prop_sim_settings(), events, transport)
        again = run(session, prop_sim_settings(), events, transport)
    assert len(transport.submits) == 1
    assert again['results'][0]['duplicate'] is True


def test_missing_sl_blocked(factory):
    transport = FakeTransport()
    event = opened_event()
    event['position']['stop'] = ''
    with factory.begin() as session:
        out = run(session, prop_sim_settings(), [event], transport)
    assert out['results'][0]['reason'] == 'SL_TP_REQUIRED'


def test_discord_cannot_change_identity_or_enable_live(factory):
    configured = settings(discord_api_key=API_KEY)
    with TestClient(create_app(configured, factory, Cache())) as client:
        r = client.post('/internal/discord/prop-sim-rollout', headers=HEADERS, json={
            'interaction_id': 'x1', 'target': 'shadow', 'confirmed': True, 'reason': 'nope',
            'prop_sim_ctrader_account_id': '1', 'trader_login': '2', 'broker': 'other',
            'host': 'demo.ctraderapi.com',
        })
        assert r.status_code == 403
        live = client.post('/internal/discord/prop-sim-rollout', headers=HEADERS, json={
            'interaction_id': 'x2', 'target': 'live', 'confirmed': True, 'reason': 'nope',
            'allow_live_trading': True,
        })
        assert live.status_code == 403
        enabled = client.post('/internal/discord/prop-sim-rollout', headers=HEADERS, json={
            'interaction_id': 'x3', 'target': 'shadow', 'confirmed': True, 'reason': 'nope',
            'prop_sim_execution_enabled': True,
        })
        assert enabled.status_code == 403
        status = client.get('/internal/discord/prop-sim-status', headers=HEADERS).json()
        assert status['allow_live_trading'] is False
        assert status['execution_enabled'] is False
        stop = client.post('/internal/discord/prop-sim-emergency-stop', headers=HEADERS,
                           json={'interaction_id': 'x4', 'reason': 'halt prop-sim'})
        assert stop.status_code == 200
        assert stop.json()['paper_available'] is True


def test_diagnostic_canary_roundtrip(factory):
    transport = FakeTransport()
    cfg = prop_sim_settings()
    with factory.begin() as session:
        demo = service.control(session)
        demo.rollout = 'canary'
        demo.blocked = False
        from services.prop_sim_orders.preflight import record_preflight
        record_preflight(session, cfg, now=NOW, extra_detail={
            'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})
        gw = PropSimCTraderExecutionGateway(transport)
        result = service.place_diagnostic_canary(session, cfg, gw, now=NOW)
        assert result['closed'] is True
        assert result['sl_confirmed'] is True
        assert result['residual_positions'] == 0
        assert transport.submits[0]['label'] == 'MRMBURU-CANARY'
        assert session.get(PropSimControl, 1).canary_consumed is True
        with pytest.raises(DemoGuardError, match='CANARY_ALREADY_USED'):
            service.place_diagnostic_canary(session, cfg, gw, now=NOW)


def test_diagnostic_canary_failure_commits_block(factory):
    class Boom(FakeTransport):
        def submit_market(self, order):
            self.submits.append(order)
            raise DemoGuardError('Position not open. PositionId=1')

    cfg = prop_sim_settings()
    error = None
    with factory.begin() as session:
        demo = service.control(session)
        demo.rollout = 'canary'
        demo.blocked = False
        from services.prop_sim_orders.preflight import record_preflight
        record_preflight(session, cfg, now=NOW, extra_detail={
            'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})
        gw = PropSimCTraderExecutionGateway(Boom())
        try:
            service.place_diagnostic_canary(session, cfg, gw, now=NOW)
        except DemoGuardError as exc:
            error = exc
    assert error is not None
    with factory.begin() as session:
        intent = session.get(PropSimOrderIntent, service.CANARY_SIGNAL_ID)
        demo = service.control(session)
        assert intent is not None
        assert intent.status == 'failed'
        assert demo.blocked is True
        with pytest.raises(DemoGuardError, match='CANARY_ALREADY_USED|blocked|Position not open'):
            gw = PropSimCTraderExecutionGateway(Boom())
            demo.rollout = 'canary'
            service.place_diagnostic_canary(session, cfg, gw, now=NOW)


def test_paper_mode_skips_prop_sim(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = service.on_paper_cycle(
            session, settings(), {'events': [opened_event()]}, now=NOW,
            gateway=PropSimCTraderExecutionGateway(transport), token_scope='trading')
    assert out['skipped'] == 'paper-mode'
    assert transport.submits == []


def test_renew_preflight_skips_when_fresh_and_rewrites_near_expiry(factory):
    from core.models import PropSimPreflight
    from services.prop_sim_orders.preflight import (
        record_preflight, renew_preflight_if_needed, require_verified_preflight,
    )
    cfg = prop_sim_settings()
    with factory.begin() as session:
        record_preflight(session, cfg, now=NOW, ttl_seconds=86400, extra_detail={
            'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})
        assert renew_preflight_if_needed(
            session, cfg, trading_permission='VERIFIED', now=NOW,
            renew_within_seconds=21600) is False
        assert renew_preflight_if_needed(
            session, cfg, trading_permission='UNVERIFIED', now=NOW) is False
        near = NOW + timedelta(hours=19)
        assert renew_preflight_if_needed(
            session, cfg, trading_permission='VERIFIED', now=near,
            renew_within_seconds=21600) is True
        row = session.get(PropSimPreflight, 1)
        assert row.expires_at == near + timedelta(seconds=86400)
        assert (row.detail or {}).get('renewed') is True
        require_verified_preflight(session, cfg, now=near)


def test_renew_preflight_rewrites_expired_row(factory):
    from services.prop_sim_orders.preflight import (
        record_preflight, renew_preflight_if_needed, require_verified_preflight,
    )
    cfg = prop_sim_settings()
    with factory.begin() as session:
        record_preflight(session, cfg, now=NOW - timedelta(days=2), ttl_seconds=86400,
                         extra_detail={'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})
        with pytest.raises(DemoGuardError, match='PREFLIGHT_EXPIRED'):
            require_verified_preflight(session, cfg, now=NOW)
        assert renew_preflight_if_needed(
            session, cfg, trading_permission='VERIFIED', now=NOW) is True
        require_verified_preflight(session, cfg, now=NOW)


def test_worker_maintains_prop_sim_preflight_on_verified_socket(factory, monkeypatch):
    import services.prop_sim_orders.preflight as preflight_mod
    from apps.worker.main import maintain_broker_socket
    from core.models import PropSimPreflight

    cfg = prop_sim_settings()
    cache = type('C', (), {
        'store': {},
        'set': lambda self, k, v, ex=None: self.store.__setitem__(k, v),
        'get': lambda self, k: self.store.get(k),
    })()
    calls = []
    real_renew = preflight_mod.renew_preflight_if_needed

    class Sess:
        healthy = True
        trading_permission = 'VERIFIED'

        def snapshot_status(self):
            return {'healthy': True}

    with factory.begin() as session:
        demo = service.control(session)
        demo.rollout = 'enabled'
        demo.blocked = False
        preflight_mod.record_preflight(
            session, cfg, now=NOW - timedelta(hours=20), ttl_seconds=86400,
            extra_detail={'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})

    def fake_renew(session, settings, *, trading_permission='VERIFIED', **kw):
        calls.append(trading_permission)
        return real_renew(session, settings, trading_permission=trading_permission,
                          now=NOW, **kw)

    monkeypatch.setattr(preflight_mod, 'renew_preflight_if_needed', fake_renew)
    maintain_broker_socket(cfg, factory, cache, {'session': Sess()})
    assert calls == ['VERIFIED']
    with factory.begin() as session:
        row = session.get(PropSimPreflight, 1)
        assert row is not None
        assert (row.detail or {}).get('renewed') is True
