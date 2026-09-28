"""Official cTrader DEMO protobuf session. Host is not configurable."""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from itertools import count
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4
import logging
import socket
import ssl
import struct

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent, ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq, ProtoOAAmendPositionSLTPReq, ProtoOAApplicationAuthReq,
    ProtoOAClosePositionReq, ProtoOAErrorRes, ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq, ProtoOANewOrderReq, ProtoOAOrderErrorEvent,
    ProtoOAReconcileReq, ProtoOASpotEvent, ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq, ProtoOASymbolsListReq, ProtoOATraderReq,
    ProtoOAUnsubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAClientPermissionScope, ProtoOAExecutionType, ProtoOAOrderType, ProtoOATradeSide,
    ProtoOAPositionStatus,
)

from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.demo_orders.barrier import TRADING_PAYLOADS, TradingMessageBarrier
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, LIVE_HOST, DemoGuardError
from services.demo_orders.transport import sanitize

log = logging.getLogger('demo_orders.sdk')
PROTOBUF_PORT = 5035
MAX_FRAME = 15_000_000
HEARTBEAT_SECONDS = 10
PRICE_SCALE = Decimal('100000')
READ_ONLY_TYPES = {
    ProtoOAApplicationAuthReq().payloadType,
    ProtoOAGetAccountListByAccessTokenReq().payloadType,
    ProtoOAAccountAuthReq().payloadType,
    ProtoOASymbolsListReq().payloadType,
    ProtoOASymbolByIdReq().payloadType,
    ProtoOATraderReq().payloadType,
    ProtoOAReconcileReq().payloadType,
    ProtoOASubscribeSpotsReq().payloadType,
    ProtoOAUnsubscribeSpotsReq().payloadType,
    ProtoHeartbeatEvent().payloadType,
}


def _message_class(payload_type: int):
    from ctrader_open_api.protobuf import Protobuf
    try:
        return type(Protobuf.get(payload_type))
    except (IndexError, KeyError) as exc:
        raise CTraderUnavailable(f'Unknown cTrader response type {payload_type}') from exc


def permission_label(scope_value):
    if scope_value == ProtoOAClientPermissionScope.SCOPE_TRADE:
        return 'VERIFIED'
    if scope_value == ProtoOAClientPermissionScope.SCOPE_VIEW:
        return 'UNVERIFIED'
    return 'UNVERIFIED'


def _money(trader):
    digits = trader.moneyDigits if trader.HasField('moneyDigits') else 2
    return str(Decimal(trader.balance) / (Decimal(10) ** digits))


def _price(value, digits=5):
    return format(Decimal(str(value)), f'.{int(digits)}f')


def _owned_label(label):
    return str(label or '').startswith('MRMBURU')


class TlsProtobufDriver:
    """Length-prefixed protobuf over TLS. Host locked to the DEMO endpoint."""

    def __init__(self, *, timeout=10, connect_fn=socket.create_connection):
        self.host = DEMO_HOST
        self.port = PROTOBUF_PORT
        self.timeout = timeout
        self._connect_fn = connect_fn
        self._socket = None
        self._pending = deque()
        self._deadline = None
        self.writes = []

    def connect(self):
        if self.host != DEMO_HOST or self.port != PROTOBUF_PORT or self.host == LIVE_HOST:
            raise DemoGuardError('LIVE endpoint rejected')
        raw = self._connect_fn((self.host, self.port), self.timeout)
        raw.settimeout(self.timeout)
        self._socket = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def send(self, payload, client_msg_id: str):
        if self._socket is None:
            raise CTraderUnavailable('DEMO session is closed')
        self.writes.append(type(payload).__name__)
        envelope = ProtoMessage(
            payloadType=payload.payloadType,
            payload=payload.SerializeToString(),
            clientMsgId=client_msg_id,
        ).SerializeToString()
        self._socket.sendall(struct.pack('!I', len(envelope)) + envelope)

    def _read_exactly(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            if self._deadline is not None:
                remaining = self._deadline - monotonic()
                if remaining <= 0:
                    raise CTraderUnavailable('cTrader operation deadline exceeded')
                self._socket.settimeout(remaining)
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                raise CTraderUnavailable('cTrader closed the connection')
            chunks.extend(chunk)
        return bytes(chunks)

    def receive(self) -> ProtoMessage:
        if self._pending:
            return self._pending.popleft()
        try:
            size = struct.unpack('!I', self._read_exactly(4))[0]
            if size <= 0 or size > MAX_FRAME:
                raise CTraderUnavailable(f'Invalid cTrader frame size: {size}')
            message = ProtoMessage()
            message.ParseFromString(self._read_exactly(size))
            if message.payloadType == ProtoOAErrorRes().payloadType:
                error = ProtoOAErrorRes()
                error.ParseFromString(message.payload)
                detail = error.description or error.errorCode
                if error.errorCode in {'OA_AUTH_TOKEN_EXPIRED', 'CH_ACCESS_TOKEN_INVALID'}:
                    raise CTraderAuthRequired('cTrader authorization failed')
                raise CTraderUnavailable(f'cTrader rejected the request: {detail}')
            return message
        except (socket.timeout, TimeoutError) as exc:
            raise CTraderUnavailable('cTrader response timed out') from exc
        except DemoGuardError:
            raise
        except (OSError, ValueError) as exc:
            raise CTraderUnavailable('cTrader transport failed') from exc

    def request(self, payload, client_msg_id: str, timeout: float):
        self._deadline = monotonic() + timeout
        self.send(payload, client_msg_id)
        while True:
            envelope = self.receive()
            if envelope.payloadType == ProtoHeartbeatEvent().payloadType:
                continue
            if envelope.clientMsgId != client_msg_id:
                self._pending.append(envelope)
                continue
            expected = payload.payloadType + 1
            if envelope.payloadType != expected:
                raise CTraderUnavailable(f'Unexpected cTrader response type {envelope.payloadType}')
            response = _message_class(expected)()
            response.ParseFromString(envelope.payload)
            return response

    def await_execution(self, payload, client_msg_id: str, timeout: float):
        self._deadline = monotonic() + timeout
        self.send(payload, client_msg_id)
        try:
            while True:
                envelope = self.receive()
                if envelope.payloadType == ProtoHeartbeatEvent().payloadType:
                    continue
                if envelope.clientMsgId and envelope.clientMsgId != client_msg_id:
                    self._pending.append(envelope)
                    continue
                if envelope.payloadType == ProtoOAOrderErrorEvent().payloadType:
                    error = ProtoOAOrderErrorEvent()
                    error.ParseFromString(envelope.payload)
                    raise CTraderUnavailable(error.description or error.errorCode or 'order error')
                if envelope.payloadType == ProtoOAExecutionEvent().payloadType:
                    event = ProtoOAExecutionEvent()
                    event.ParseFromString(envelope.payload)
                    return event
                self._pending.append(envelope)
                raise CTraderUnavailable(f'Unexpected cTrader execution type {envelope.payloadType}')
        except CTraderUnavailable as exc:
            if 'timed out' in str(exc) or 'deadline' in str(exc):
                raise TimeoutError('cTrader execution timeout') from exc
            raise

    def wait_payload(self, payload_type: int, timeout: float, predicate=None):
        self._deadline = monotonic() + timeout
        while True:
            envelope = self.receive()
            if envelope.payloadType == ProtoHeartbeatEvent().payloadType:
                continue
            if envelope.payloadType != payload_type:
                self._pending.append(envelope)
                continue
            message = _message_class(payload_type)()
            message.ParseFromString(envelope.payload)
            if predicate is None or predicate(message):
                return message


class SdkDemoSession:
    """Authenticated DEMO session with heartbeat and a trading-message barrier."""

    def __init__(self, *, account_id, client_id, client_secret, access_token, barrier,
                 budget=None, driver=None, timeout=10):
        if str(account_id) in LIVE_ACCOUNT_IDS:
            raise DemoGuardError('LIVE account rejected')
        self.account_id = int(account_id)
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.barrier = barrier
        self.budget = budget
        self.driver = driver or TlsProtobufDriver(timeout=timeout)
        self.timeout = timeout
        self.host = DEMO_HOST
        self.port = PROTOBUF_PORT
        self.environment = 'demo'
        self.transport = 'sdk-tls'
        self.persistent = True
        self.healthy = False
        self.app_auth = False
        self.account_auth = False
        self.trading_permission = 'UNVERIFIED'
        self.permission_scope = None
        self.accounts = []
        self._ids = count(1)
        self._hb_stop = Event()
        self._hb_thread = None
        self._lock = Lock()
        self.writes = self.driver.writes
        self.reconnects = 0
        self._instrument = None

    def _client_msg_id(self):
        return f'mrmburu-{next(self._ids)}-{uuid4().hex}'

    def _request(self, payload):
        if payload.payloadType in TRADING_PAYLOADS:
            self.barrier.authorize(payload.payloadType)
        elif payload.payloadType not in READ_ONLY_TYPES:
            raise DemoGuardError('Unsupported DEMO payload')
        if self.budget is not None and payload.payloadType != ProtoHeartbeatEvent().payloadType:
            self.budget.reserve()
        with self._lock:
            return self.driver.request(payload, self._client_msg_id(), self.timeout)

    def _execution(self, payload):
        if payload.payloadType in TRADING_PAYLOADS:
            self.barrier.authorize(payload.payloadType)
        elif payload.payloadType not in READ_ONLY_TYPES:
            raise DemoGuardError('Unsupported DEMO payload')
        if self.budget is not None:
            self.budget.reserve()
        with self._lock:
            return self.driver.await_execution(payload, self._client_msg_id(), self.timeout)

    def connect(self):
        if getattr(self.driver, 'host', DEMO_HOST) != DEMO_HOST:
            raise DemoGuardError('LIVE endpoint rejected')
        self.driver.connect()
        self.healthy = True

    def authenticate(self, **_kwargs):
        if self.account_auth and self.healthy:
            return self._auth_payload()
        self.connect()
        self._request(ProtoOAApplicationAuthReq(clientId=self.client_id, clientSecret=self.client_secret))
        self.app_auth = True
        listed = self._request(ProtoOAGetAccountListByAccessTokenReq(accessToken=self.access_token))
        self.permission_scope = getattr(listed, 'permissionScope', None)
        self.trading_permission = permission_label(self.permission_scope)
        self.accounts = [
            {'ctidTraderAccountId': int(row.ctidTraderAccountId), 'isLive': bool(row.isLive)}
            for row in listed.ctidTraderAccount
        ]
        match = next((row for row in self.accounts if row['ctidTraderAccountId'] == self.account_id), None)
        if match is None:
            raise DemoGuardError('DEMO account is not authorized for this token')
        if match['isLive'] is True:
            raise DemoGuardError('LIVE account rejected')
        self._request(ProtoOAAccountAuthReq(
            ctidTraderAccountId=self.account_id, accessToken=self.access_token,
        ))
        self.account_auth = True
        self._start_heartbeat()
        return self._auth_payload()

    def _auth_payload(self):
        return {
            'app_auth': True,
            'account_auth': True,
            'permission_scope': 'trading' if self.trading_permission == 'VERIFIED' else 'accounts',
            'trading_permission': self.trading_permission,
            'permission_metadata': 'ProtoOAGetAccountListByAccessTokenRes.permissionScope',
            'accounts': self.accounts,
            'host': self.host,
        }

    def symbol(self, name, account_id):
        listed = self._request(ProtoOASymbolsListReq(
            ctidTraderAccountId=int(account_id), includeArchivedSymbols=False,
        ))
        light = next((row for row in listed.symbol if row.symbolName == name), None)
        if light is None:
            raise DemoGuardError('EURUSD is not available on this account')
        detail = self._request(ProtoOASymbolByIdReq(
            ctidTraderAccountId=int(account_id), symbolId=[light.symbolId],
        ))
        meta = next((row for row in detail.symbol if row.symbolId == light.symbolId), None)
        if meta is None:
            raise DemoGuardError('EURUSD metadata unavailable')
        self._instrument = {
            'symbol_id': int(meta.symbolId),
            'min_volume': str(meta.minVolume),
            'step_volume': str(meta.stepVolume),
            'max_volume': str(meta.maxVolume),
            'digits': int(meta.digits),
            'pip_position': int(meta.pipPosition),
            'lot_size': str(meta.lotSize) if meta.lotSize else str(meta.minVolume),
        }
        return self._instrument

    def snapshot(self, account_id=None):
        account_id = int(account_id or self.account_id)
        trader_res = self._request(ProtoOATraderReq(ctidTraderAccountId=account_id))
        balance = _money(trader_res.trader)
        rec = self._request(ProtoOAReconcileReq(ctidTraderAccountId=account_id))
        open_positions = sum(
            1 for row in rec.position
            if row.positionStatus == ProtoOAPositionStatus.POSITION_STATUS_OPEN
        )
        instrument = self._instrument or self.symbol('EURUSD', account_id)
        return {
            'balance': balance,
            'equity': balance,
            'open_positions': open_positions,
            'open_orders': len(list(rec.order)),
            'day_start_balance': balance,
            'initial_balance': balance,
            'instrument': instrument,
        }

    def quote(self, symbol_id):
        self._request(ProtoOASubscribeSpotsReq(
            ctidTraderAccountId=self.account_id, symbolId=[int(symbol_id)],
            subscribeToSpotTimestamp=True,
        ))
        with self._lock:
            spot = self.driver.wait_payload(
                ProtoOASpotEvent().payloadType, self.timeout,
                lambda row: row.symbolId == int(symbol_id) and row.HasField('bid') and row.HasField('ask'),
            )
        try:
            self._request(ProtoOAUnsubscribeSpotsReq(
                ctidTraderAccountId=self.account_id, symbolId=[int(symbol_id)],
            ))
        except Exception:
            pass
        return {
            'bid': str(Decimal(spot.bid) / PRICE_SCALE),
            'ask': str(Decimal(spot.ask) / PRICE_SCALE),
        }

    def new_order(self, account_id, request):
        side = str(request['side']).lower()
        payload = ProtoOANewOrderReq(
            ctidTraderAccountId=int(account_id),
            symbolId=int(request['symbol_id']),
            orderType=ProtoOAOrderType.MARKET,
            tradeSide=ProtoOATradeSide.BUY if side == 'buy' else ProtoOATradeSide.SELL,
            volume=int(request['volume']),
            stopLoss=float(request['stop_loss']),
            takeProfit=float(request['take_profit']),
            comment=str(request.get('comment') or 'MRMBURU')[:50],
            label=str(request.get('label') or 'MRMBURU')[:50],
            clientOrderId=str(request['client_order_id'])[:50],
        )
        try:
            event = self._execution(payload)
        except TimeoutError:
            raise
        except CTraderUnavailable as exc:
            raise DemoGuardError(str(exc)) from exc
        return self._from_execution(event, request)

    def amend_sl_tp(self, account_id, position_id, stop_loss, take_profit):
        payload = ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=int(account_id),
            positionId=int(position_id),
            stopLoss=float(stop_loss),
            takeProfit=float(take_profit),
        )
        try:
            event = self._execution(payload)
        except TimeoutError:
            raise
        except CTraderUnavailable as exc:
            raise DemoGuardError(str(exc)) from exc
        parsed = self._from_execution(event, {
            'stop_loss': stop_loss, 'take_profit': take_profit, 'label': 'MRMBURU',
        })
        return {
            'sl_confirmed': parsed['sl_confirmed'],
            'stop_loss': parsed['stop_loss'],
            'take_profit': parsed['take_profit'],
        }

    def close_position(self, account_id, position_id, volume):
        payload = ProtoOAClosePositionReq(
            ctidTraderAccountId=int(account_id),
            positionId=int(position_id),
            volume=int(volume),
        )
        try:
            event = self._execution(payload)
        except TimeoutError:
            raise
        except CTraderUnavailable as exc:
            raise DemoGuardError(str(exc)) from exc
        return {'status': ProtoOAExecutionType.Name(event.executionType), 'closed': str(position_id)}

    def find_by_client_order_id(self, account_id, client_order_id):
        rec = self._request(ProtoOAReconcileReq(ctidTraderAccountId=int(account_id)))
        order = next((row for row in rec.order if row.clientOrderId == client_order_id), None)
        if order is None:
            return None
        position = next((row for row in rec.position if row.positionId == order.positionId), None)
        digits = (self._instrument or {}).get('digits', 5)
        sl = position.stopLoss if position and position.HasField('stopLoss') else (
            order.stopLoss if order.HasField('stopLoss') else '')
        tp = position.takeProfit if position and position.HasField('takeProfit') else (
            order.takeProfit if order.HasField('takeProfit') else '')
        label = ''
        if position and position.HasField('tradeData'):
            label = position.tradeData.label
        elif order.HasField('tradeData'):
            label = order.tradeData.label
        fill = order.executionPrice if order.HasField('executionPrice') else (
            position.price if position else '')
        return {
            'order_id': str(order.orderId),
            'position_id': str(order.positionId or (position.positionId if position else '')),
            'fill_price': _price(fill, digits) if fill != '' else '',
            'stop_loss': _price(sl, digits) if sl != '' else '',
            'take_profit': _price(tp, digits) if tp != '' else '',
            'sl_confirmed': sl != '' and tp != '',
            'tp_confirmed': tp != '',
            'status': 'ORDER_FILLED',
            'label': label,
            'client_order_id': client_order_id,
            'volume': int(order.tradeData.volume) if order.HasField('tradeData') else 0,
        }

    def position(self, account_id, position_id):
        rec = self._request(ProtoOAReconcileReq(ctidTraderAccountId=int(account_id)))
        row = next((item for item in rec.position if str(item.positionId) == str(position_id)), None)
        if row is None:
            return None
        label = row.tradeData.label if row.HasField('tradeData') else ''
        if not _owned_label(label):
            return {'label': label, 'volume': int(row.tradeData.volume) if row.HasField('tradeData') else 0}
        digits = (self._instrument or {}).get('digits', 5)
        return {
            'label': label,
            'volume': int(row.tradeData.volume) if row.HasField('tradeData') else 0,
            'stop_loss': _price(row.stopLoss, digits) if row.HasField('stopLoss') else '',
            'take_profit': _price(row.takeProfit, digits) if row.HasField('takeProfit') else '',
            'position_id': str(row.positionId),
        }

    def _from_execution(self, event, request):
        digits = (self._instrument or {}).get('digits', 5)
        execution = ProtoOAExecutionType.Name(event.executionType)
        order = event.order if event.HasField('order') else None
        position = event.position if event.HasField('position') else None
        deal = event.deal if event.HasField('deal') else None
        fill = ''
        if deal is not None and deal.HasField('executionPrice'):
            fill = _price(deal.executionPrice, digits)
        elif position is not None and position.HasField('price'):
            fill = _price(position.price, digits)
        sl = ''
        tp = ''
        if position is not None and position.HasField('stopLoss'):
            sl = _price(position.stopLoss, digits)
        elif order is not None and order.HasField('stopLoss'):
            sl = _price(order.stopLoss, digits)
        if position is not None and position.HasField('takeProfit'):
            tp = _price(position.takeProfit, digits)
        elif order is not None and order.HasField('takeProfit'):
            tp = _price(order.takeProfit, digits)
        if not sl:
            sl = str(request.get('stop_loss') or '')
        if not tp:
            tp = str(request.get('take_profit') or '')
        sl_ok = bool(position and position.HasField('stopLoss') and position.HasField('takeProfit'))
        label = request.get('label') or 'MRMBURU'
        if position is not None and position.HasField('tradeData') and position.tradeData.label:
            label = position.tradeData.label
        return {
            'order_id': str(order.orderId) if order else str(deal.orderId if deal else ''),
            'position_id': str(position.positionId if position else (order.positionId if order else '')),
            'fill_price': fill,
            'stop_loss': sl,
            'take_profit': tp,
            'sl_confirmed': sl_ok,
            'tp_confirmed': sl_ok,
            'status': execution,
            'label': label,
            'client_order_id': request.get('client_order_id'),
            'volume': request.get('volume'),
        }

    def heartbeat(self):
        with self._lock:
            self.driver.send(ProtoHeartbeatEvent(), self._client_msg_id())
        return True

    def _start_heartbeat(self):
        if self._hb_thread is not None:
            return
        self._hb_stop.clear()

        def loop():
            while not self._hb_stop.wait(HEARTBEAT_SECONDS):
                try:
                    self.heartbeat()
                except Exception:
                    self.healthy = False
                    return

        self._hb_thread = Thread(target=loop, name='demo-sdk-heartbeat', daemon=True)
        self._hb_thread.start()

    def reconnect(self, *, attempts=4, sleeper=sleep):
        self.close()
        delay = 1
        last = None
        for _ in range(attempts):
            try:
                self.reconnects += 1
                self.app_auth = False
                self.account_auth = False
                return self.authenticate()
            except Exception as exc:
                last = exc
                sleeper(delay)
                delay = min(delay * 2, 30)
        raise DemoGuardError('reconnect failed') from last

    def snapshot_status(self):
        return sanitize({
            'host': self.host,
            'port': self.port,
            'healthy': self.healthy,
            'app_auth': self.app_auth,
            'account_auth': self.account_auth,
            'trading_permission': self.trading_permission,
            'permission_metadata': 'ProtoOAGetAccountListByAccessTokenRes.permissionScope',
        })

    def close(self):
        self._hb_stop.set()
        thread = self._hb_thread
        self._hb_thread = None
        if thread is not None:
            thread.join(timeout=1)
        self.driver.close()
        self.healthy = False
        self.account_auth = False


class SdkDemoSessionFactory:
    def open(self, settings, *, barrier, budget=None, driver=None, redis_client=None):
        from services.ctrader.client import access_token, configured
        from services.ctrader import tokens as token_store
        if redis_client is not None and token_store.execution_quarantined(redis_client):
            raise DemoGuardError('OAUTH_TOKEN_QUARANTINED')
        account = (settings.demo_ctrader_account_id or '').strip()
        if not account or account in LIVE_ACCOUNT_IDS:
            raise DemoGuardError('LIVE account rejected')
        if getattr(settings, 'ctrader_environment', 'demo') != 'demo':
            raise DemoGuardError('LIVE endpoint rejected')
        if not configured(settings):
            raise CTraderAuthRequired('cTrader client id/secret are not set')
        token = access_token(settings, redis_client)
        if not token:
            raise CTraderAuthRequired('Authorize cTrader first (no access token)')
        session = SdkDemoSession(
            account_id=account,
            client_id=settings.ctrader_client_id.get_secret_value(),
            client_secret=settings.ctrader_client_secret.get_secret_value(),
            access_token=token,
            barrier=barrier,
            budget=budget,
            driver=driver,
        )
        session.authenticate()
        session.barrier.trading_permission = session.trading_permission
        return session

    def open_probe(self, settings, redis_client=None, *, driver=None, budget=None):
        barrier = TradingMessageBarrier(profile='probe', demo_execution_enabled=False)
        return self.open(settings, barrier=barrier, budget=budget, driver=driver,
                         redis_client=redis_client)
