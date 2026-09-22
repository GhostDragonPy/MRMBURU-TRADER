"""Synchronous, read-only cTrader demo Open API transport."""
from __future__ import annotations

import socket
import ssl
import struct
from contextlib import AbstractContextManager
from collections import deque
from itertools import count
from uuid import uuid4

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent, ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq, ProtoOAApplicationAuthReq, ProtoOAAssetListReq,
    ProtoOAErrorRes, ProtoOAGetPositionUnrealizedPnLReq, ProtoOAGetTrendbarsReq,
    ProtoOAReconcileReq, ProtoOASubscribeSpotsReq, ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq, ProtoOATraderReq,
)
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable

DEMO_HOST = "demo.ctraderapi.com"
PROTOBUF_PORT = 5035
MAX_FRAME = 15_000_000
READ_ONLY_REQUEST_TYPES = {
    klass().payloadType for klass in (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOAAssetListReq,
        ProtoOAGetPositionUnrealizedPnLReq, ProtoOAGetTrendbarsReq,
        ProtoOAReconcileReq, ProtoOASubscribeSpotsReq, ProtoOASymbolByIdReq,
        ProtoOASymbolsListReq, ProtoOATraderReq,
    )
}


class ReadOnlyOpenApi(AbstractContextManager):
    """Authenticated demo connection limited to explicitly supplied reads."""

    def __init__(self, *, client_id: str, client_secret: str, access_token: str,
                 account_id: int, timeout: float = 10):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self.timeout = timeout
        self._socket = None
        self._ids = count(1)
        self._pending = deque()

    def __enter__(self):
        try:
            raw = socket.create_connection((DEMO_HOST, PROTOBUF_PORT), self.timeout)
            raw.settimeout(self.timeout)
            self._socket = ssl.create_default_context().wrap_socket(raw, server_hostname=DEMO_HOST)
            self.request(ProtoOAApplicationAuthReq(
                clientId=self.client_id, clientSecret=self.client_secret,
            ))
            self.request(ProtoOAAccountAuthReq(
                ctidTraderAccountId=self.account_id, accessToken=self.access_token,
            ))
            return self
        except CTraderUnavailable:
            self.close()
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.close()
            raise CTraderUnavailable(f"cTrader demo connection failed: {exc}") from exc

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _write(self, payload, client_msg_id: str):
        envelope = ProtoMessage(
            payloadType=payload.payloadType,
            payload=payload.SerializeToString(),
            clientMsgId=client_msg_id,
        ).SerializeToString()
        self._socket.sendall(struct.pack("!I", len(envelope)) + envelope)

    def _read_exactly(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
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
                self._write(ProtoHeartbeatEvent(), f"heartbeat-{next(self._ids)}")
                return self.receive()
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
        while True:
            envelope = self._pending.popleft() if self._pending else self.receive()
            if envelope.payloadType != response_class().payloadType:
                continue
            response = response_class()
            response.ParseFromString(envelope.payload)
            if predicate(response):
                return response


def _message_class(payload_type: int):
    from ctrader_open_api.protobuf import Protobuf
    try:
        return type(Protobuf.get(payload_type))
    except (IndexError, KeyError) as exc:
        raise CTraderUnavailable(f"Unknown cTrader response type {payload_type}") from exc
