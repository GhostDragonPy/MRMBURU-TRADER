from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthRes, ProtoOAApplicationAuthRes, ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenRes, ProtoOANewOrderReq, ProtoOAReconcileRes,
    ProtoOASubscribeSpotsRes, ProtoOASymbolByIdRes, ProtoOASymbolsListRes,
    ProtoOATraderRes, ProtoOAUnsubscribeSpotsRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAClientPermissionScope, ProtoOAExecutionType,
)
from services.ctrader.prop_sim import PROP_SIM_CTID
from services.demo_orders.barrier import NEW_ORDER, TradingMessageBarrier
from services.demo_orders.guards import LIVE_HOST, DemoGuardError
from services.demo_orders.sdk_session import SdkDemoSession, TlsProtobufDriver
from services.prop_sim_orders.factory import UnconfiguredPropSimTransport, build_gateway
from services.prop_sim_orders.probe import evaluate as probe_evaluate
from services.prop_sim_orders.transport import OfficialPropSimTransport, normalize_volume
from tests.test_prop_sim_orders import NOW, FakeTransport, opened_event, prop_sim_settings, run
from tests.test_official_demo_transport import FakeDemoSession
from tests.test_api import Cache
from services.ctrader import tokens as token_store
from apps.prop_sim_probe import main as probe_main


class PropSimFakeDriver:
    def __init__(self, *, is_live=True, scope=ProtoOAClientPermissionScope.SCOPE_TRADE,
                 account=48803059, login=17204978, broker='FTMO', extra_live=None):
        self.host = LIVE_HOST
        self.port = 5035
        self.writes = []
        self._socket = object()
        self.is_live = is_live
        self.scope = scope
        self.account = account
        self.login = login
        self.broker = broker
        self.extra_live = extra_live
        self.closed = False
        self.allow_live = True

    def connect(self):
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
            row.traderLogin = self.login
            if self.extra_live is not None:
                other = res.ctidTraderAccount.add()
                other.ctidTraderAccountId = self.extra_live
                other.isLive = True
                other.traderLogin = 1
            return res
        if name == 'ProtoOAAccountAuthReq':
            return ProtoOAAccountAuthRes()
        if name == 'ProtoOATraderReq':
            res = ProtoOATraderRes()
            res.trader.balance = 10000000
            res.trader.moneyDigits = 2
            res.trader.traderLogin = self.login
            res.trader.brokerName = self.broker
            return res
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


def sdk_session(driver=None, profile='probe', execution_enabled=False,
                trading_permission='UNVERIFIED'):
    driver = driver or PropSimFakeDriver()
    barrier = TradingMessageBarrier(
        profile=profile, demo_execution_enabled=execution_enabled,
        trading_permission=trading_permission)
    barrier.redis_key = 'prop-sim:barrier:new_orders'
    return SdkDemoSession(
        account_id=PROP_SIM_CTID, client_id='id', client_secret='secret', access_token='token',
        barrier=barrier, driver=driver, identity='prop-sim')


def test_prop_sim_auth_list_symbol_heartbeat_zero_orders():
    session = sdk_session()
    listed = session.authenticate()
    assert listed['app_auth'] and listed['account_auth']
    assert listed['trading_permission'] == 'VERIFIED'
    meta = session.symbol('EURUSD', int(PROP_SIM_CTID))
    assert meta['min_volume'] == '1000'
    assert meta['digits'] == 5
    assert meta['pip_position'] == 4
    assert session.heartbeat() is True
    assert 'ProtoOANewOrderReq' not in session.writes
    session.close()
    assert session.driver.closed is True


def test_probe_zero_trading_messages(factory):
    session = sdk_session()
    with factory.begin() as db:
        report = probe_evaluate(prop_sim_settings(), session, persist=db, now=datetime.now(timezone.utc))
        session.close()
    assert report['result'] == 'PASS'
    assert 'ProtoOANewOrderReq' not in session.writes
    assert 'ProtoOAClosePositionReq' not in session.writes
    assert 'ProtoOAAmendPositionSLTPReq' not in session.writes
    assert 'secret' not in str(report).lower()
    assert '48803059' not in str(report)


def test_shadow_zero_trading_and_reconnect_skips_old_would_order(factory):
    session = sdk_session(profile='shadow')
    session.authenticate()
    with pytest.raises(DemoGuardError, match='TRADING_MESSAGE_BLOCKED'):
        session.barrier.authorize(NEW_ORDER)
    transport = FakeTransport()
    events = [opened_event()]
    with factory.begin() as db:
        first = run(db, prop_sim_settings(), events, transport, rollout='shadow')
        assert first['results'][0]['status'] == 'shadow'
        assert transport.submits == []
    session.reconnect(sleeper=lambda *_: None)
    with factory.begin() as db:
        again = run(db, prop_sim_settings(), events, transport, rollout='shadow')
        assert again['results'][0]['duplicate'] is True
        assert transport.submits == []
    session.close()


def test_other_live_account_rejected():
    live = sdk_session(driver=PropSimFakeDriver(extra_live=999))
    with pytest.raises(DemoGuardError, match='OTHER_LIVE_ACCOUNT_BLOCKED'):
        live.authenticate()


def test_wrong_login_rejected():
    session = sdk_session(driver=PropSimFakeDriver(login=1, broker='FTMO'))
    with pytest.raises(DemoGuardError, match='PROP_SIM_TUPLE_MISMATCH'):
        session.authenticate()


def test_demo_host_rejected_for_prop_sim():
    driver = PropSimFakeDriver()
    driver.host = 'demo.ctraderapi.com'
    blocked = SdkDemoSession(
        account_id=PROP_SIM_CTID, client_id='id', client_secret='secret', access_token='token',
        barrier=TradingMessageBarrier(profile='probe'), driver=driver, identity='prop-sim')
    with pytest.raises(DemoGuardError, match='LIVE host required'):
        blocked.connect()


def test_tls_driver_live_only_when_allowed():
    connect = Mock(side_effect=AssertionError('network'))
    driver = TlsProtobufDriver(connect_fn=connect)
    driver.host = LIVE_HOST
    with pytest.raises(DemoGuardError, match='LIVE endpoint'):
        driver.connect()
    connect.assert_not_called()
    allowed = TlsProtobufDriver(connect_fn=connect, host=LIVE_HOST, allow_live=True)
    with pytest.raises(AssertionError):
        allowed.connect()


def test_normalize_volume_never_increases():
    instrument = {'min_volume': '1000', 'step_volume': '1000', 'max_volume': '1000000'}
    with pytest.raises(DemoGuardError, match='volume below minimum'):
        normalize_volume('500', instrument)
    assert normalize_volume('1000', instrument) == 1000


def test_factory_shadow_stays_unconfigured_without_execution(factory):
    cfg = prop_sim_settings(prop_sim_execution_enabled=False)
    with factory.begin() as db:
        from services.prop_sim_orders import service
        service.control(db).rollout = 'shadow'
        gw = build_gateway(cfg, db_session=db, token_scope='trading',
                           protobuf_session=FakeDemoSession(host=LIVE_HOST, environment='live'))
        assert isinstance(gw.transport, UnconfiguredPropSimTransport)


def test_enabled_session_sends_one_new_order():
    session = sdk_session(profile='enabled', execution_enabled=True,
                          trading_permission='VERIFIED')
    session.authenticate()
    session.symbol('EURUSD', int(PROP_SIM_CTID))
    filled = session.new_order(int(PROP_SIM_CTID), {
        'symbol_id': 1, 'side': 'buy', 'volume': 1000,
        'stop_loss': '1.09000', 'take_profit': '1.13000',
        'label': 'MRMBURU', 'client_order_id': 'MRMBURU-sig',
    })
    assert filled['order_id'] == '11'
    assert filled['position_id'] == '22'
    assert filled['sl_confirmed'] is True
    assert session.writes.count('ProtoOANewOrderReq') == 1
    session.close()


def test_probe_cli_without_secrets():
    code = probe_main(settings=prop_sim_settings(), session=None)
    assert code == 1


def test_independent_tokens(factory):
    cache = Cache()
    token_store.save_market_data_tokens(cache, {'access_token': 'md-token', 'expires_in': 60})
    token_store.save_prop_sim_tokens(
        cache, {'access_token': 'ps-token', 'expires_in': 60},
        account_id='48803059', trader_login='17204978')
    assert token_store.load_access_token(cache) == 'md-token'
    assert token_store.load_prop_sim_access_token(cache) == 'ps-token'
    assert token_store.load_access_token(cache) != token_store.load_prop_sim_access_token(cache)
