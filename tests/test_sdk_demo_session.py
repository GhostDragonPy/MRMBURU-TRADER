from unittest.mock import Mock
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthRes, ProtoOAApplicationAuthRes, ProtoOAGetAccountListByAccessTokenRes,
    ProtoOANewOrderReq, ProtoOASymbolByIdRes, ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAClientPermissionScope
from services.demo_orders.barrier import NEW_ORDER, TradingMessageBarrier
from services.demo_orders.guards import DEMO_HOST, DemoGuardError
from services.demo_orders.preflight import record_preflight
from services.demo_orders.probe import evaluate as probe_evaluate
from services.demo_orders.sdk_session import SdkDemoSession, SdkDemoSessionFactory
from services.demo_orders.factory import UnconfiguredDemoTransport, build_gateway
from tests.test_demo_orders import NOW, FakeTransport, demo_settings, opened_event, run
from tests.test_official_demo_transport import FakeDemoSession
from datetime import datetime, timezone
from core.models import DemoOrderIntent
from apps.demo_probe import main as probe_main


class FakeDriver:
    def __init__(self, *, is_live=False, scope=ProtoOAClientPermissionScope.SCOPE_TRADE,
                 account=1001, fail_connect=0):
        self.host = DEMO_HOST
        self.port = 5035
        self.writes = []
        self._socket = object()
        self.is_live = is_live
        self.scope = scope
        self.account = account
        self.fail_connect = fail_connect
        self.closed = False

    def connect(self):
        if self.fail_connect > 0:
            self.fail_connect -= 1
            raise OSError('down')
        self._socket = object()
        self.closed = False

    def close(self):
        self._socket = None
        self.closed = True

    def send(self, payload, client_msg_id):
        self.writes.append(type(payload).__name__)

    def request(self, payload, client_msg_id, timeout):
        self.send(payload, client_msg_id)
        name = type(payload).__name__
        if name == 'ProtoOAApplicationAuthReq':
            return ProtoOAApplicationAuthRes()
        if name == 'ProtoOAGetAccountListByAccessTokenReq':
            res = ProtoOAGetAccountListByAccessTokenRes()
            res.permissionScope = self.scope
            row = res.ctidTraderAccount.add()
            row.ctidTraderAccountId = self.account
            row.isLive = self.is_live
            return res
        if name == 'ProtoOAAccountAuthReq':
            return ProtoOAAccountAuthRes()
        if name == 'ProtoOASymbolsListReq':
            res = ProtoOASymbolsListRes()
            light = res.symbol.add()
            light.symbolId = 1
            light.symbolName = 'EURUSD'
            return res
        if name == 'ProtoOASymbolByIdReq':
            res = ProtoOASymbolByIdRes()
            meta = res.symbol.add()
            meta.symbolId = 1
            meta.minVolume = 1000
            meta.stepVolume = 1000
            meta.maxVolume = 1000000
            return res
        raise AssertionError(name)


def sdk_session(driver=None, profile='probe', demo_execution_enabled=False,
                trading_permission='UNVERIFIED', account=1001):
    driver = driver or FakeDriver(account=account)
    barrier = TradingMessageBarrier(
        profile=profile, demo_execution_enabled=demo_execution_enabled,
        trading_permission=trading_permission)
    return SdkDemoSession(
        account_id=account, client_id='id', client_secret='secret', access_token='token',
        barrier=barrier, driver=driver)


def test_sdk_auth_list_account_symbol_heartbeat():
    session = sdk_session()
    listed = session.authenticate()
    assert listed['app_auth'] and listed['account_auth']
    assert listed['trading_permission'] == 'VERIFIED'
    meta = session.symbol('EURUSD', 1001)
    assert meta['min_volume'] == '1000'
    assert session.heartbeat() is True
    assert 'ProtoOANewOrderReq' not in session.writes
    session.close()
    assert session._hb_thread is None
    assert session.driver.closed is True


def test_probe_zero_trading_messages_and_pass_persist(factory):
    session = sdk_session()
    with factory.begin() as db:
        report = probe_evaluate(demo_settings(), session, persist=db, now=datetime.now(timezone.utc))
        session.close()
    assert report['result'] == 'PASS'
    assert 'ProtoOANewOrderReq' not in session.writes
    assert 'secret' not in str(report).lower()


def test_shadow_zero_trading_and_reconnect_skips_old_would_order(factory):
    session = sdk_session(profile='shadow')
    session.authenticate()
    with pytest.raises(DemoGuardError, match='TRADING_MESSAGE_BLOCKED'):
        session.barrier.authorize(NEW_ORDER)
    transport = FakeTransport()
    events = [opened_event()]
    with factory.begin() as db:
        first = run(db, demo_settings(), events, transport, rollout='shadow')
        assert first['results'][0]['status'] == 'shadow'
        assert transport.submits == []
    session.reconnect(sleeper=lambda *_: None)
    with factory.begin() as db:
        again = run(db, demo_settings(), events, transport, rollout='shadow')
        assert again['results'][0]['duplicate'] is True
        assert transport.submits == []
        assert db.get(DemoOrderIntent, first['results'][0]['signal_id']).status == 'shadow'
    session.close()


def test_live_host_account_and_denylist_rejected():
    with pytest.raises(DemoGuardError, match='LIVE account'):
        sdk_session(account=48803059)
    live = sdk_session(driver=FakeDriver(is_live=True))
    with pytest.raises(DemoGuardError, match='LIVE account'):
        live.authenticate()
    driver = FakeDriver()
    driver.host = 'live.ctraderapi.com'
    blocked = SdkDemoSession(
        account_id=1001, client_id='id', client_secret='secret', access_token='token',
        barrier=TradingMessageBarrier(profile='probe'), driver=driver)
    with pytest.raises(DemoGuardError, match='LIVE endpoint'):
        blocked.connect()


def test_unverified_scope_blocks_canary_orders():
    session = sdk_session(driver=FakeDriver(scope=ProtoOAClientPermissionScope.SCOPE_VIEW),
                          profile='canary', demo_execution_enabled=True,
                          trading_permission='UNVERIFIED')
    listed = session.authenticate()
    assert listed['trading_permission'] == 'UNVERIFIED'
    with pytest.raises(DemoGuardError, match='UNVERIFIED'):
        session.barrier.authorize(NEW_ORDER)
    session.close()


def test_sanitized_probe_cli_without_secrets():
    code = probe_main(settings=demo_settings(), session=None)
    assert code == 1


def test_factory_shadow_stays_unconfigured_without_execution(factory):
    cfg = demo_settings(demo_execution_enabled=False)
    with factory.begin() as db:
        from services.demo_orders import service
        service.control(db).rollout = 'shadow'
        gw = build_gateway(cfg, db_session=db, token_scope='trading',
                           protobuf_session=FakeDemoSession())
        assert isinstance(gw.transport, UnconfiguredDemoTransport)


def test_tls_driver_never_uses_live_connect(monkeypatch):
    from services.demo_orders.sdk_session import TlsProtobufDriver
    connect = Mock(side_effect=AssertionError('network'))
    driver = TlsProtobufDriver(connect_fn=connect)
    driver.host = 'live.ctraderapi.com'
    with pytest.raises(DemoGuardError, match='LIVE endpoint'):
        driver.connect()
    connect.assert_not_called()
