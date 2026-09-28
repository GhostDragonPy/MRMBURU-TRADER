"""Synchronous read-only cTrader transport; broker order messages are forbidden."""
from __future__ import annotations

import socket
import ssl
import struct
from contextlib import AbstractContextManager
from collections import deque
from itertools import count
from uuid import uuid4
from time import monotonic

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent, ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq, ProtoOAApplicationAuthReq, ProtoOAAssetListReq,
    ProtoOAErrorRes, ProtoOAGetPositionUnrealizedPnLReq, ProtoOAGetTrendbarsReq,
    ProtoOAReconcileReq, ProtoOASubscribeSpotsReq, ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq, ProtoOATraderReq, ProtoOASubscribeLiveTrendbarReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable

# Legacy constant name retained; matches the VPS account's read-only Live source.
DEMO_HOST = "live.ctraderapi.com"
PROTOBUF_PORT = 5035
MAX_FRAME = 15_000_000
READ_ONLY_REQUEST_TYPES = {
    klass().payloadType for klass in (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOAAssetListReq,
        ProtoOAGetPositionUnrealizedPnLReq, ProtoOAGetTrendbarsReq,
        ProtoOAReconcileReq, ProtoOASubscribeSpotsReq, ProtoOASymbolByIdReq,
        ProtoOASymbolsListReq, ProtoOATraderReq, ProtoOASubscribeLiveTrendbarReq,
        ProtoOAGetAccountListByAccessTokenReq,
    )
}
ALLOWED_WRITES = READ_ONLY_REQUEST_TYPES | {ProtoHeartbeatEvent().payloadType}


class ReadOnlyOpenApi(AbstractContextManager):
    """Authenticated connection limited to an explicit read-only allowlist."""

    def __init__(self, *, client_id: str, client_secret: str, access_token: str,
                 account_id: int, timeout: float = 10, budget=None, environment='demo',
                 authenticate_account: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self.timeout = timeout
        self._socket = None
        self._ids = count(1)
        self._pending = deque()
        self._deadline = None
        self._budget = budget
        self._first_write_reserved = False
        self.allowed_writes = set(ALLOWED_WRITES)
        self.authenticate_account = bool(authenticate_account)
        if environment not in ('demo', 'live'):
            raise ValueError('Invalid broker environment')
        self.environment = environment
        self.host = 'demo.ctraderapi.com' if environment == 'demo' else DEMO_HOST
        self._heartbeat_at = monotonic()

    def __enter__(self):
        try:
            # Refuse even a connection attempt when the shared budget is spent.
            if self._budget is None:
                raise CTraderUnavailable('cTrader transport requires a request budget')
            self._budget.reserve()
            self._first_write_reserved = True
            raw = socket.create_connection((self.host, PROTOBUF_PORT), self.timeout)
            raw.settimeout(self.timeout)
            self._socket = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
            self._heartbeat_at = monotonic()
            self.request(ProtoOAApplicationAuthReq(
                clientId=self.client_id, clientSecret=self.client_secret,
            ))
            accounts = self.request(ProtoOAGetAccountListByAccessTokenReq(accessToken=self.access_token))
            if not self.authenticate_account:
                return self
            match = next((a for a in accounts.ctidTraderAccount if a.ctidTraderAccountId == self.account_id), None)
            if match is None or match.isLive != (self.environment == 'live'):
                raise CTraderUnavailable('Account absent or incompatible with broker environment')
            self.request(ProtoOAAccountAuthReq(
                ctidTraderAccountId=self.account_id, accessToken=self.access_token,
            ))
            return self
        except (CTraderUnavailable, CTraderAuthRequired):
            self.close()
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.close()
            raise CTraderUnavailable(f"cTrader read-only connection failed: {exc}") from exc

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _write(self, payload, client_msg_id: str):
        if payload.payloadType not in self.allowed_writes:
            raise CTraderUnavailable('Blocked non-read-only cTrader request')
        # Bootstrap can span several requests; keep it alive too, using the same
        # guarded writer. Collector separately schedules idle heartbeats.
        if payload.payloadType != ProtoHeartbeatEvent().payloadType and monotonic()-self._heartbeat_at >= 9:
            self._write(ProtoHeartbeatEvent(), 'bootstrap-heartbeat')
        if self._budget is None:
            raise CTraderUnavailable('cTrader transport requires a request budget')
        if self._first_write_reserved:
            self._first_write_reserved = False
        else:
            self._budget.reserve()
        envelope = ProtoMessage(
            payloadType=payload.payloadType,
            payload=payload.SerializeToString(),
            clientMsgId=client_msg_id,
        ).SerializeToString()
        self._socket.sendall(struct.pack("!I", len(envelope)) + envelope)
        if payload.payloadType == ProtoHeartbeatEvent().payloadType:
            self._heartbeat_at = monotonic()

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
                raise CTraderUnavailable("cTrader closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def receive(self) -> ProtoMessage:
        try:
            size = struct.unpack("!I", self._read_exactly(4))[0]
            if size <= 0 or size > MAX_FRAME:
                raise CTraderUnavailable(f"Invalid cTrader frame size: {size}")
            message = ProtoMessage()
            message.ParseFromString(self._read_exactly(size))
            if message.payloadType == ProtoHeartbeatEvent().payloadType:
                return message
            if message.payloadType == ProtoOAErrorRes().payloadType:
                error = ProtoOAErrorRes()
                error.ParseFromString(message.payload)
                detail = error.description or error.errorCode
                if error.errorCode in {"OA_AUTH_TOKEN_EXPIRED", "CH_ACCESS_TOKEN_INVALID"}:
                    raise CTraderAuthRequired(f"cTrader authorization failed: {detail}")
                raise CTraderUnavailable(f"cTrader rejected the request: {detail}")
            return message
        except (socket.timeout, TimeoutError) as exc:
            raise CTraderUnavailable("cTrader response timed out") from exc
        except (OSError, ValueError) as exc:
            raise CTraderUnavailable(f"cTrader transport failed: {exc}") from exc

    def request(self, payload):
        if payload.payloadType not in READ_ONLY_REQUEST_TYPES:
            raise CTraderUnavailable('Blocked non-read-only cTrader request')
        self._deadline = monotonic() + self.timeout
        client_msg_id = f"mrmburu-{next(self._ids)}-{uuid4().hex}"
        self._write(payload, client_msg_id)
        while True:
            envelope = self.receive()
            if envelope.clientMsgId != client_msg_id:
                self._pending.append(envelope)
                continue
            response_type = payload.payloadType + 1
            if envelope.payloadType != response_type:
                raise CTraderUnavailable(f"Unexpected cTrader response type {envelope.payloadType}")
            response_class = _message_class(response_type)
            response = response_class()
            response.ParseFromString(envelope.payload)
            return response

    def wait_for(self, response_class, predicate=lambda _: True):
        self._deadline = monotonic() + self.timeout
        while True:
            if monotonic() >= self._deadline:
                raise CTraderUnavailable('cTrader event deadline exceeded')
            envelope = self._pending.popleft() if self._pending else self.receive()
            if envelope.payloadType != response_class().payloadType:
                continue
            response = response_class()
            response.ParseFromString(envelope.payload)
            if predicate(response):
                return response


class TradingOpenApi(ReadOnlyOpenApi):
    """Same auth as read-only, plus a single market NewOrder path."""

    def __init__(self, **kwargs):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAClosePositionReq, ProtoOANewOrderReq,
        )
        super().__init__(**kwargs)
        self.allowed_writes.add(ProtoOANewOrderReq().payloadType)
        self.allowed_writes.add(ProtoOAClosePositionReq().payloadType)

    def _await_terminal(self):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType as XT
        terminal = {
            XT.ORDER_FILLED, XT.ORDER_REJECTED, XT.ORDER_CANCELLED,
            XT.ORDER_EXPIRED, XT.ORDER_PARTIAL_FILL,
        }
        while True:
            event = self.wait_for(ProtoOAExecutionEvent)
            if event.executionType in terminal:
                return event

    def send_market(self, *, symbol_id: int, side: str, volume: int):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType, ProtoOATradeSide
        if side not in ('buy', 'sell'):
            raise CTraderUnavailable('Invalid trade side')
        if volume <= 0:
            raise CTraderUnavailable('Invalid volume')
        request = ProtoOANewOrderReq(
            ctidTraderAccountId=self.account_id,
            symbolId=symbol_id,
            orderType=ProtoOAOrderType.MARKET,
            tradeSide=ProtoOATradeSide.BUY if side == 'buy' else ProtoOATradeSide.SELL,
            volume=volume,
        )
        self._deadline = monotonic() + self.timeout
        client_msg_id = f"mrmburu-{next(self._ids)}-{uuid4().hex}"
        self._write(request, client_msg_id)
        return self._await_terminal()

    def close_position(self, *, position_id: int, volume: int):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq
        request = ProtoOAClosePositionReq(
            ctidTraderAccountId=self.account_id,
            positionId=int(position_id),
            volume=int(volume),
        )
        self._deadline = monotonic() + self.timeout
        client_msg_id = f"mrmburu-{next(self._ids)}-{uuid4().hex}"
        self._write(request, client_msg_id)
        return self._await_terminal()


def _message_class(payload_type: int):
    from ctrader_open_api.protobuf import Protobuf
    try:
        return type(Protobuf.get(payload_type))
    except (IndexError, KeyError) as exc:
        raise CTraderUnavailable(f"Unknown cTrader response type {payload_type}") from exc
