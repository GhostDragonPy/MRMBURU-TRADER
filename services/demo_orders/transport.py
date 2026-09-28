"""Official cTrader DEMO protobuf transport. Not wired by factory.build_gateway."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from threading import Lock
from time import monotonic
from collections import deque
from itertools import count
from uuid import uuid4
import logging
import socket
import ssl
import struct

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent, ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq, ProtoOAAmendPositionSLTPReq, ProtoOAApplicationAuthReq,
    ProtoOAClosePositionReq, ProtoOAErrorRes, ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq, ProtoOANewOrderReq, ProtoOAReconcileReq,
    ProtoOASymbolByIdReq, ProtoOASymbolsListReq, ProtoOATraderReq,
    ProtoOAUnsubscribeLiveTrendbarReq, ProtoOAUnsubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAClientPermissionScope, ProtoOAExecutionType, ProtoOAOrderType, ProtoOATradeSide,
)

from services.ctrader.budget import RequestBudget
from services.ctrader.types import CTraderUnavailable
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, LIVE_HOST, DemoGuardError, reject_live_identity

LABEL = 'MRMBURU'
PROTOBUF_PORT = 5035
MAX_FRAME = 15_000_000
D = Decimal
log = logging.getLogger('demo_orders.transport')

SECRET_FRAGMENTS = ('token', 'secret', 'password', 'authorization')


def client_order_id(signal_id: str) -> str:
    return ('MRMBURU-' + str(signal_id))[:50]


def normalize_volume(units, instrument):
    step = D(str(instrument['step_volume']))
    minimum = D(str(instrument['min_volume']))
    maximum = D(str(instrument['max_volume']))
    qty = (D(str(units)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if qty < minimum:
        qty = minimum
    if qty > maximum:
        qty = maximum
    return int(qty)


def sanitize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SECRET_FRAGMENTS):
                continue
            out[key] = sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    text = str(value)
    if any(part in text.lower() for part in ('access_token', 'client_secret', 'bearer ')):
        return '[redacted]'
    return value


def _account_id(settings):
    account = (getattr(settings, 'demo_ctrader_account_id', None) or '').strip()
    if not account:
        raise DemoGuardError('DEMO_CTRADER_ACCOUNT_ID is required')
    if account in LIVE_ACCOUNT_IDS or str(getattr(settings, 'ctrader_account_id', '') or '') in LIVE_ACCOUNT_IDS:
        raise DemoGuardError('LIVE account rejected')
    return int(account)


class OfficialDemoTransport:
    """SDK protobuf session bound to demo.ctraderapi.com with trading-scope OAuth."""

    def __init__(self, settings, *, session, token_scope='trading', budget=None):
        self.settings = settings
        self.session = session
        self.scope = token_scope
        self.budget = budget
        self.account_id = _account_id(settings)
        self.host = getattr(session, 'host', DEMO_HOST)
        self.environment = getattr(session, 'environment', 'demo')
        self.is_live = False
        self._ready = False
        self._locks = {}
        self._lock_table = Lock()
        self._results = {}
        self._instrument = None
        self._sent_new_orders = 0
        reject_live_identity(
            account_id=self.account_id, is_live=False, host=self.host,
            environment=self.environment, scope=self.scope,
        )
        if self.host == LIVE_HOST or 'live' in str(self.host).lower():
            raise DemoGuardError('LIVE endpoint rejected')
        if getattr(settings, 'ctrader_environment', 'demo') != 'demo':
            raise DemoGuardError('LIVE endpoint rejected')
        if getattr(settings, 'allow_live_trading', False) is not False:
            raise DemoGuardError('LIVE trading flag rejected')
        if getattr(settings, 'execution_enabled', False) is not False:
            raise DemoGuardError('EXECUTION_ENABLED must remain false')

    def _gate_lock(self, key):
        with self._lock_table:
            lock = self._locks.get(key)
            if lock is None:
                lock = Lock()
                self._locks[key] = lock
            return lock

    def connect(self):
        if self.budget is None:
            raise DemoGuardError('RequestBudget is required')
        listed = self.session.authenticate(
            client_id=self.settings.ctrader_client_id.get_secret_value()
            if getattr(self.settings, 'ctrader_client_id', None) else 'unused',
            client_secret=self.settings.ctrader_client_secret.get_secret_value()
            if getattr(self.settings, 'ctrader_client_secret', None) else 'unused',
            access_token='[injected]',
            account_id=self.account_id,
            scope=self.scope,
        )
        accounts = listed.get('accounts') or []
        if listed.get('permission_scope') != 'trading':
            raise DemoGuardError('Token scope accounts rejected')
        match = next((row for row in accounts if int(row['ctidTraderAccountId']) == self.account_id), None)
        if match is None:
            raise DemoGuardError('DEMO account is not authorized for this token')
        if match.get('isLive') is True:
            raise DemoGuardError('LIVE account rejected')
        if str(match['ctidTraderAccountId']) in LIVE_ACCOUNT_IDS:
            raise DemoGuardError('LIVE account rejected')
        self.is_live = False
        self._ready = True
        log.info('demo transport authenticated account=%s host=%s', self.account_id, self.host)
        return listed

    def _ensure(self):
        if self.budget is None:
            raise DemoGuardError('RequestBudget is required')
        if getattr(self.session, 'account_auth', False):
            self._ready = True
            return
        if not self._ready:
            self.connect()

    def snapshot(self):
        self._ensure()
        snap = self.session.snapshot(self.account_id)
        instrument = snap.get('instrument') or self._load_instrument()
        self._instrument = instrument
        open_positions = snap.get('open_positions', 0)
        return {
            'balance': str(snap.get('balance', '0')),
            'equity': str(snap.get('equity', '0')),
            'open_positions': int(open_positions),
            'open_orders': int(snap.get('open_orders', 0)),
            'day_start_balance': str(snap.get('day_start_balance', snap.get('balance', '0'))),
            'initial_balance': str(snap.get('initial_balance', snap.get('balance', '0'))),
            'instrument': instrument,
        }

    def _load_instrument(self):
        meta = self.session.symbol('EURUSD', self.account_id)
        self._instrument = {
            'symbol_id': int(meta['symbol_id']),
            'min_volume': str(meta['min_volume']),
            'max_volume': str(meta['max_volume']),
            'step_volume': str(meta['step_volume']),
            'lot_size': str(meta.get('lot_size') or meta['min_volume']),
        }
        return self._instrument

    def submit_market(self, order):
        self._ensure()
        if order.get('symbol') != 'EURUSD':
            raise DemoGuardError('Only EURUSD is allowed')
        if not order.get('stop_loss') or not order.get('take_profit'):
            raise DemoGuardError('SL/TP required')
        cid = client_order_id(order['signal_id'])
        with self._gate_lock(cid):
            if cid in self._results:
                return self._results[cid]
            recovered = self.session.find_by_client_order_id(self.account_id, cid)
            if recovered:
                result = self._confirmed(recovered, order, cid)
                self._results[cid] = result
                return result
            instrument = self._instrument or self._load_instrument()
            volume = normalize_volume(order['volume'], instrument)
            label = str(order.get('label') or LABEL)
            if not label.startswith(LABEL):
                raise DemoGuardError('Refusing unlabeled DEMO order')
            request = {
                'symbol_id': instrument['symbol_id'],
                'side': order['side'],
                'volume': volume,
                'stop_loss': str(order['stop_loss']),
                'take_profit': str(order['take_profit']),
                'label': label[:50],
                'client_order_id': str(order.get('client_order_id') or cid)[:50],
                'comment': label[:50],
            }
            try:
                self._sent_new_orders += 1
                raw = self.session.new_order(self.account_id, request)
            except TimeoutError:
                recovered = self.session.find_by_client_order_id(self.account_id, cid)
                if recovered:
                    result = self._confirmed(recovered, order, cid)
                    self._results[cid] = result
                    return result
                raise
            result = self._confirmed(raw, order, cid)
            self._results[cid] = result
            log.info('demo new order correlated id=%s status=%s', cid, result.get('status'))
            return result

    def _confirmed(self, raw, order, cid):
        payload = sanitize({
            'order_id': str(raw.get('order_id') or ''),
            'position_id': str(raw.get('position_id') or ''),
            'fill_price': str(raw.get('fill_price') or ''),
            'stop_loss': str(raw.get('stop_loss') or order['stop_loss']),
            'take_profit': str(raw.get('take_profit') or order['take_profit']),
            'status': raw.get('status') or 'ORDER_FILLED',
            'client_order_id': cid,
            'label': raw.get('label') or LABEL,
        })
        sl_ok = bool(raw.get('sl_confirmed')) and bool(payload['stop_loss'])
        tp_ok = bool(raw.get('tp_confirmed', True)) and bool(payload['take_profit'])
        if payload['position_id'] and not sl_ok:
            amended = self.session.amend_sl_tp(
                self.account_id, payload['position_id'], payload['stop_loss'], payload['take_profit'])
            sl_ok = bool(amended.get('sl_confirmed'))
            payload['stop_loss'] = str(amended.get('stop_loss') or payload['stop_loss'])
            payload['take_profit'] = str(amended.get('take_profit') or payload['take_profit'])
        payload['sl_confirmed'] = sl_ok and tp_ok
        payload['tp_confirmed'] = tp_ok
        return payload

    def reconcile(self, signal_id):
        self._ensure()
        cid = client_order_id(signal_id)
        found = self.session.find_by_client_order_id(self.account_id, cid)
        if not found:
            return None
        return sanitize({
            'order_id': str(found.get('order_id') or ''),
            'position_id': str(found.get('position_id') or ''),
            'fill_price': str(found.get('fill_price') or ''),
            'stop_loss': str(found.get('stop_loss') or ''),
            'take_profit': str(found.get('take_profit') or ''),
            'sl_confirmed': bool(found.get('sl_confirmed')),
            'status': found.get('status') or 'ORDER_FILLED',
            'client_order_id': cid,
        })

    def close_position(self, position_id):
        self._ensure()
        owned = self.session.position(self.account_id, position_id)
        if owned is None or not str(owned.get('label') or '').startswith(LABEL):
            raise DemoGuardError('Refusing to manage a foreign position')
        closed = self.session.close_position(self.account_id, position_id, owned.get('volume'))
        return sanitize({'closed': str(position_id), 'status': closed.get('status')})

    def close(self):
        self.session.close()
        self._ready = False


class DemoProtobufSession:
    """Persistent TLS protobuf client forced onto the DEMO host."""

    def __init__(self, *, client_id, client_secret, access_token, account_id, timeout=10, budget=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = int(account_id)
        if str(self.account_id) in LIVE_ACCOUNT_IDS:
            raise DemoGuardError('LIVE account rejected')
        self.timeout = timeout
        self.host = DEMO_HOST
        self.environment = 'demo'
        self._budget = budget
        self._socket = None
        self._ids = count(1)
        self._pending = deque()
        self._deadline = None
        self._first_write_reserved = False
        self._heartbeat_at = monotonic()
        self._subscriptions = []
        self.allowed_writes = {
            klass().payloadType for klass in (
                ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq,
                ProtoOAGetAccountListByAccessTokenReq, ProtoOATraderReq,
                ProtoOASymbolsListReq, ProtoOASymbolByIdReq, ProtoOAReconcileReq,
                ProtoOANewOrderReq, ProtoOAClosePositionReq, ProtoOAAmendPositionSLTPReq,
                ProtoOAUnsubscribeSpotsReq, ProtoOAUnsubscribeLiveTrendbarReq,
                ProtoHeartbeatEvent,
            )
        }

    def authenticate(self, **_kwargs):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def snapshot(self, account_id):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def symbol(self, name, account_id):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def new_order(self, account_id, request):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def find_by_client_order_id(self, account_id, client_order_id):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def amend_sl_tp(self, account_id, position_id, stop_loss, take_profit):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def position(self, account_id, position_id):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def close_position(self, account_id, position_id, volume):
        raise DemoGuardError('Real DEMO sockets are not activated')

    def close(self):
        for unsub in list(self._subscriptions):
            try:
                self._write(unsub, f'mrmburu-unsub-{next(self._ids)}')
            except Exception:
                pass
        self._subscriptions.clear()
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _write(self, payload, client_msg_id: str):
        if payload.payloadType not in self.allowed_writes:
            raise CTraderUnavailable('Blocked unsupported cTrader request')
        if LIVE_HOST in (getattr(self, 'host', ''),):
            raise DemoGuardError('LIVE endpoint rejected')
        if self._budget is None:
            raise CTraderUnavailable('cTrader transport requires a request budget')
        if payload.payloadType != ProtoHeartbeatEvent().payloadType and monotonic() - self._heartbeat_at >= 9:
            self._write(ProtoHeartbeatEvent(), 'demo-heartbeat')
        if self._first_write_reserved:
            self._first_write_reserved = False
        else:
            self._budget.reserve()
        if self._socket is None:
            raise CTraderUnavailable('DEMO session is closed')
        envelope = ProtoMessage(
            payloadType=payload.payloadType,
            payload=payload.SerializeToString(),
            clientMsgId=client_msg_id,
        ).SerializeToString()
        self._socket.sendall(struct.pack('!I', len(envelope)) + envelope)
        if payload.payloadType == ProtoHeartbeatEvent().payloadType:
            self._heartbeat_at = monotonic()


def assert_demo_host(host):
    if host != DEMO_HOST or host == LIVE_HOST:
        raise DemoGuardError('LIVE endpoint rejected')
    return host
