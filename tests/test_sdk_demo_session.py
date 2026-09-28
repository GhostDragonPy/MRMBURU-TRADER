from collections import deque
from unittest.mock import Mock
import pytest
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthRes, ProtoOAApplicationAuthRes, ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenRes, ProtoOANewOrderReq, ProtoOAReconcileRes,
    ProtoOASpotEvent, ProtoOASubscribeSpotsReq, ProtoOASubscribeSpotsRes,
    ProtoOASymbolByIdRes, ProtoOASymbolsListRes, ProtoOATraderRes,
    ProtoOAUnsubscribeSpotsRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAClientPermissionScope, ProtoOAExecutionType, ProtoOAPositionStatus,
)
from services.demo_orders.barrier import NEW_ORDER, TradingMessageBarrier
from services.demo_orders.guards import DEMO_HOST, DemoGuardError
from services.demo_orders.preflight import record_preflight
from services.demo_orders.probe import evaluate as probe_evaluate
from services.demo_orders.sdk_session import SdkDemoSession, SdkDemoSessionFactory, TlsProtobufDriver
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
            meta.digits = 5
            meta.pipPosition = 4
            meta.lotSize = 10000000
            return res
        if name == 'ProtoOATraderReq':
            res = ProtoOATraderRes()
            res.trader.balance = 10000000
            res.trader.moneyDigits = 2
            return res
        if name == 'ProtoOAReconcileReq':
            return ProtoOAReconcileRes()
        if name == 'ProtoOASubscribeSpotsReq':
            return ProtoOASubscribeSpotsRes()
        if name == 'ProtoOAUnsubscribeSpotsReq':
            return ProtoOAUnsubscribeSpotsRes()
        raise AssertionError(name)

    def await_execution(self, payload, client_msg_id, timeout):
        self.send(payload, client_msg_id)
        event = ProtoOAExecutionEvent()
        event.executionType = ProtoOAExecutionType.ORDER_FILLED
        event.order.orderId = 11
        event.order.positionId = 22
        event.order.clientOrderId = getattr(payload, 'clientOrderId', '')
        if getattr(payload, 'stopLoss', None):
            event.order.stopLoss = payload.stopLoss
            event.position.stopLoss = payload.stopLoss
        if getattr(payload, 'takeProfit', None):
            event.order.takeProfit = payload.takeProfit
            event.position.takeProfit = payload.takeProfit
        event.position.positionId = 22
        event.position.price = 1.10010
        event.position.tradeData.label = getattr(payload, 'label', 'MRMBURU') or 'MRMBURU'
        event.position.tradeData.volume = int(getattr(payload, 'volume', 1000) or 1000)
        event.deal.orderId = 11
        event.deal.positionId = 22
        event.deal.executionPrice = 1.10010
        return event


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


def test_probe_blocks_new_order():
    session = sdk_session(profile='probe')
    session.authenticate()
    with pytest.raises(DemoGuardError, match='TRADING_MESSAGE_BLOCKED'):
        session.new_order(1001, {
            'symbol_id': 1, 'side': 'buy', 'volume': 1000,
            'stop_loss': '1.09', 'take_profit': '1.13',
            'label': 'MRMBURU', 'client_order_id': 'MRMBURU-x',
        })
    assert session.writes.count('ProtoOANewOrderReq') == 0
    session.close()


def test_enabled_session_sends_one_new_order():
    session = sdk_session(profile='enabled', demo_execution_enabled=True,
                          trading_permission='VERIFIED')
    session.authenticate()
    session.symbol('EURUSD', 1001)
    filled = session.new_order(1001, {
        'symbol_id': 1, 'side': 'buy', 'volume': 1000,
        'stop_loss': '1.09000', 'take_profit': '1.13000',
        'label': 'MRMBURU', 'client_order_id': 'MRMBURU-sig',
    })
    assert filled['order_id'] == '11'
    assert filled['position_id'] == '22'
    assert filled['sl_confirmed'] is True
    assert session.writes.count('ProtoOANewOrderReq') == 1
    session.close()


def test_canary_allows_one_new_order_then_close():
    session = sdk_session(profile='canary', demo_execution_enabled=True,
                          trading_permission='VERIFIED')
    session.authenticate()
    session.new_order(1001, {
        'symbol_id': 1, 'side': 'buy', 'volume': 1000,
        'stop_loss': '1.09', 'take_profit': '1.13',
        'label': 'MRMBURU-CANARY', 'client_order_id': 'MRMBURU-CANARY-DIAG',
    })
    with pytest.raises(DemoGuardError, match='CANARY_ORDER_CAP'):
        session.new_order(1001, {
            'symbol_id': 1, 'side': 'buy', 'volume': 1000,
            'stop_loss': '1.09', 'take_profit': '1.13',
            'label': 'MRMBURU-CANARY', 'client_order_id': 'MRMBURU-CANARY-2',
        })
    closed = session.close_position(1001, 22, 1000)
    assert closed['closed'] == '22'
    session.close()


def test_worker_passes_persistent_session(factory):
    cfg = demo_settings()
    with factory.begin() as db:
        from services.demo_orders import service
        service.control(db).rollout = 'enabled'
        from services.demo_orders.preflight import record_preflight
        record_preflight(db, cfg, now=datetime.now(timezone.utc), extra_detail={
            'trading_permission': 'VERIFIED', 'socket': 'sdk-tls'})
        sess = FakeDemoSession()
        sess.account_auth = True
        gw = build_gateway(cfg, Mock(), db_session=db, token_scope='trading',
                           protobuf_session=sess, rollout='enabled')
        from services.demo_orders.transport import OfficialDemoTransport
        assert isinstance(gw.transport, OfficialDemoTransport)
        assert gw.transport.session is sess


def _envelope(payload, client_msg_id='x'):
    return ProtoMessage(
        payloadType=payload.payloadType,
        payload=payload.SerializeToString(),
        clientMsgId=client_msg_id,
    )


def _spot_envelope(symbol_id=1, client_msg_id=''):
    spot = ProtoOASpotEvent(
        ctidTraderAccountId=1001, symbolId=symbol_id, bid=110000, ask=110010,
    )
    return _envelope(spot, client_msg_id)


def _install_framed_receive(driver, frames):
    calls = {'n': 0}
    queued = deque(frames)

    def fake_receive(*, socket_only=False):
        calls['n'] += 1
        if calls['n'] > 40:
            raise AssertionError('busy-loop recycling pending frames')
        if not socket_only and driver._pending:
            return driver._pending.popleft()
        if queued:
            return queued.popleft()
        raise TimeoutError('no more frames')

    driver.receive = fake_receive
    driver.send = lambda *a, **k: None
    return calls


def test_request_does_not_spin_when_spot_arrives_before_ack():
    driver = TlsProtobufDriver(timeout=2)
    req = ProtoOASubscribeSpotsReq()
    ack = ProtoOASubscribeSpotsRes(ctidTraderAccountId=1001)
    expected_id = 'mrmburu-subscribe'
    calls = _install_framed_receive(driver, [
        _spot_envelope(),
        _envelope(ack, expected_id),
    ])
    result = driver.request(req, expected_id, 2.0)
    assert isinstance(result, ProtoOASubscribeSpotsRes)
    assert len(driver._pending) == 1
    assert driver._pending[0].payloadType == ProtoOASpotEvent().payloadType
    assert calls['n'] == 2


def test_wait_payload_does_not_spin_on_unmatched_pending():
    driver = TlsProtobufDriver(timeout=2)
    unmatched = _envelope(ProtoOAReconcileRes(ctidTraderAccountId=1001), 'stale')
    driver._pending.append(unmatched)
    calls = _install_framed_receive(driver, [_spot_envelope()])
    found = driver.wait_payload(
        ProtoOASpotEvent().payloadType, 2.0,
        lambda row: row.symbolId == 1 and row.HasField('bid') and row.HasField('ask'),
    )
    assert found.symbolId == 1
    assert found.bid == 110000
    assert list(driver._pending) == [unmatched]
    assert calls['n'] == 1


def test_wait_payload_consumes_matching_pending_without_socket():
    driver = TlsProtobufDriver(timeout=2)
    driver._pending.append(_spot_envelope())
    calls = _install_framed_receive(driver, [])
    found = driver.wait_payload(ProtoOASpotEvent().payloadType, 2.0)
    assert found.symbolId == 1
    assert calls['n'] == 0
    assert not driver._pending
