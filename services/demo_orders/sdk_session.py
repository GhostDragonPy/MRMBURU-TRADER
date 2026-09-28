"""Official cTrader DEMO protobuf session. Host is not configurable."""
from __future__ import annotations

from collections import deque
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
    ProtoOAAccountAuthReq, ProtoOAApplicationAuthReq, ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq, ProtoOASymbolByIdReq, ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAClientPermissionScope

from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.demo_orders.barrier import TRADING_PAYLOADS, TradingMessageBarrier
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, LIVE_HOST, DemoGuardError
from services.demo_orders.transport import sanitize

log = logging.getLogger('demo_orders.sdk')
PROTOBUF_PORT = 5035
MAX_FRAME = 15_000_000
HEARTBEAT_SECONDS = 10
READ_ONLY_TYPES = {
    ProtoOAApplicationAuthReq().payloadType,
    ProtoOAGetAccountListByAccessTokenReq().payloadType,
    ProtoOAAccountAuthReq().payloadType,
    ProtoOASymbolsListReq().payloadType,
    ProtoOASymbolByIdReq().payloadType,
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

    def connect(self):
        if getattr(self.driver, 'host', DEMO_HOST) != DEMO_HOST:
            raise DemoGuardError('LIVE endpoint rejected')
        self.driver.connect()
        self.healthy = True

    def authenticate(self, **_kwargs):
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
        return {
            'symbol_id': int(meta.symbolId),
            'min_volume': str(meta.minVolume),
            'step_volume': str(meta.stepVolume),
            'max_volume': str(meta.maxVolume),
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
                return self.authenticate()
            except Exception as exc:
                last = exc
                sleeper(delay)
                delay = min(delay * 2, 30)
        raise DemoGuardError('reconnect failed')

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


class SdkDemoSessionFactory:
    def open(self, settings, *, barrier, budget=None, driver=None, redis_client=None):
        from services.ctrader.client import access_token, configured
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
