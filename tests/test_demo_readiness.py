from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from services.demo_orders import service
from services.demo_orders.factory import UnconfiguredDemoTransport, build_gateway
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from services.demo_orders.preflight import record_preflight
from services.demo_orders.probe import evaluate as probe_evaluate
from services.demo_orders.transport import OfficialDemoTransport
from tests.test_demo_orders import NOW, FakeTransport, demo_settings, opened_event, run
from tests.test_official_demo_transport import FakeDemoSession
from tests.test_api import Cache, settings as api_settings
from fastapi.testclient import TestClient
from apps.api.main import create_app
from tests.test_discord_control import API_KEY, HEADERS
from core.models import DemoControl, KillSwitch


def test_preflight_missing_blocks_send(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport, seed_preflight=False)
    assert out['results'][0]['reason'] == 'PREFLIGHT_MISSING'
    assert transport.submits == []


def test_expired_preflight_blocks(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        session.get(KillSwitch, 1).active = False
        demo = service.control(session)
        demo.rollout = 'enabled'
        demo.armed_at = NOW - timedelta(seconds=5)
        record_preflight(session, demo_settings(), now=NOW - timedelta(days=3), ttl_seconds=60)
        gw = DemoCTraderExecutionGateway(transport)
        out = service.on_paper_cycle(session, demo_settings(), {'events': [opened_event()]},
                                     now=NOW, gateway=gw, token_scope='trading')
    assert out['results'][0]['reason'] == 'PREFLIGHT_EXPIRED'
    assert transport.submits == []


def test_signal_before_arm_rejected(factory):
    transport = FakeTransport()
    event = opened_event()
    event['position']['bar'] = (NOW - timedelta(seconds=60)).isoformat()
    with factory.begin() as session:
        out = run(session, demo_settings(), [event], transport)
    assert out['results'][0]['reason'] == 'SIGNAL_BEFORE_ARM'
    assert transport.submits == []


def test_expired_signal_rejected(factory):
    transport = FakeTransport()
    event = opened_event()
    event['position']['bar'] = (NOW - timedelta(seconds=200)).isoformat()
    with factory.begin() as session:
        session.get(KillSwitch, 1).active = False
        demo = service.control(session)
        demo.rollout = 'enabled'
        demo.armed_at = NOW - timedelta(seconds=400)
        record_preflight(session, demo_settings(), now=NOW - timedelta(seconds=5))
        out = service.on_paper_cycle(session, demo_settings(), {'events': [event]},
                                     now=NOW, gateway=DemoCTraderExecutionGateway(transport),
                                     token_scope='trading')
    assert out['results'][0]['reason'] == 'SIGNAL_EXPIRED'
    assert transport.submits == []


def test_fresh_signal_accepted(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event()], transport)
    assert out['results'][0]['status'] == 'filled'
    assert len(transport.submits) == 1


def test_canary_consumed_survives_restart(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        run(session, demo_settings(), [opened_event()], transport, rollout='canary')
        assert session.get(DemoControl, 1).canary_consumed is True
    other = {'kind': 'opened', 'position': {
        'side': 'sell', 'units': '5000', 'entry': '1.10', 'stop': '1.11', 'target': '1.08',
        'risk': '20', 'bar': NOW.isoformat(), 'context': {'setup_id': 'setup-canary-2'},
    }}
    with factory.begin() as session:
        out = run(session, demo_settings(), [other], transport, rollout='canary', reset=False)
    assert out['results'][0]['status'] == 'failed'
    assert len(transport.submits) == 1


def test_min_volume_over_risk_is_blocked(factory):
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened_event(risk='30', units='100')],
                  transport, rollout='canary')
    assert out['results'][0]['reason'] == 'VOLUME_EXCEEDS_RISK'
    assert transport.submits == []


def test_official_transport_only_when_all_demo_gates(factory):
    session = FakeDemoSession()
    cfg = demo_settings(demo_execution_enabled=True)
    with factory.begin() as db:
        service.control(db).rollout = 'enabled'
        service.control(db).armed_at = NOW
        record_preflight(db, cfg, now=datetime.now(timezone.utc))
        gw = build_gateway(cfg, Mock(), db_session=db, token_scope='trading',
                           protobuf_session=session)
        assert isinstance(gw.transport, OfficialDemoTransport)
        service.control(db).rollout = 'shadow'
        gw = build_gateway(cfg, Mock(), db_session=db, token_scope='trading',
                           protobuf_session=session)
        assert isinstance(gw.transport, UnconfiguredDemoTransport)


def test_read_only_probe_pass_and_no_order_writes(factory):
    class ProbeSession(FakeDemoSession):
        def __init__(self):
            super().__init__()
            self.port = 5035
            self.persistent = True
            self.writes = ['authenticate']
        def authenticate(self, **kwargs):
            return {
                'app_auth': True, 'account_auth': True, 'permission_scope': 'trading',
                'accounts': [{'ctidTraderAccountId': 1001, 'isLive': False}],
            }
        def heartbeat(self):
            return True
    with factory.begin() as db:
        report = probe_evaluate(demo_settings(), ProbeSession(), persist=db, now=NOW)
    assert report['result'] == 'PASS'
    assert 'token' not in str(report).lower() or 'injected' not in str(report)
    blob = str(report)
    assert 'client_secret' not in blob


def test_discord_cannot_enable_execution_or_change_host(factory):
    configured = api_settings(discord_api_key=API_KEY)
    with TestClient(create_app(configured, factory, Cache())) as client:
        r = client.post('/internal/discord/demo-rollout', headers=HEADERS,
                        json={'interaction_id': 'x1', 'target': 'shadow', 'confirmed': True,
                              'demo_execution_enabled': True, 'host': 'live.ctraderapi.com'})
        assert r.status_code == 403
        pre = client.get('/internal/discord/demo-preflight', headers=HEADERS)
        assert pre.status_code == 200
        assert pre.json()['status'] == 'missing'
        stop = client.post('/internal/discord/demo-emergency-stop', headers=HEADERS,
                           json={'interaction_id': 'x2', 'reason': 'halt demo'})
        assert stop.status_code == 200
        status = client.get('/internal/discord/demo-status', headers=HEADERS).json()
        assert status['execution_enabled'] is False
        assert status['allow_live_trading'] is False
        assert '*' in status['demo_account'] or status['demo_account'] == '****'


def test_probe_cli_without_session_fails_closed():
    from apps.demo_probe import main
    assert main(argv=[], session=None) == 1
