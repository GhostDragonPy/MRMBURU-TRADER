from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq

from services.ctrader.budget import RequestBudget
from services.ctrader.client import open_demo
from services.ctrader.openapi import ReadOnlyOpenApi
from services.ctrader.types import CTraderUnavailable


def transport(budget=None):
    return ReadOnlyOpenApi(client_id='id', client_secret='secret',
        access_token='token', account_id=1, budget=budget)


def test_disabled_configuration_blocks_before_credentials_or_socket():
    with pytest.raises(CTraderUnavailable, match='disabled'):
        open_demo(SimpleNamespace(ctrader_network_enabled=False))


@pytest.mark.parametrize('result', [0, None, '1'])
def test_denied_or_invalid_budget_blocks_write(result):
    cache = Mock()
    cache.eval.return_value = result
    t = transport(RequestBudget(cache, 1, 500, 20))
    t._socket = Mock()
    with pytest.raises(CTraderUnavailable):
        t._write(ProtoOAApplicationAuthReq(clientId='id', clientSecret='secret'), 'a')
    t._socket.sendall.assert_not_called()


def test_redis_outage_blocks_connection_attempt(monkeypatch):
    cache = Mock()
    cache.eval.side_effect = RuntimeError('private backend details')
    connect = Mock()
    monkeypatch.setattr('services.ctrader.openapi.socket.create_connection', connect)
    with pytest.raises(CTraderUnavailable, match='budget unavailable') as exc:
        transport(RequestBudget(cache, 1, 500, 20)).__enter__()
    assert 'private' not in str(exc.value)
    connect.assert_not_called()


def test_heartbeats_do_not_consume_budget():
    budget = Mock()
    t = transport(budget)
    t._socket = Mock()
    t._write(ProtoOAApplicationAuthReq(clientId='id', clientSecret='secret'), 'a')
    t._write(ProtoHeartbeatEvent(), 'h')
    t._write(ProtoHeartbeatEvent(), 'h2')
    assert budget.reserve.call_count == 1
    assert t._socket.sendall.call_count == 3


def test_preconnection_reservation_is_consumed_once():
    budget = Mock()
    t = transport(budget)
    t._socket = Mock()
    t._first_write_reserved = True
    t._write(ProtoHeartbeatEvent(), 'h1')
    t._write(ProtoOAApplicationAuthReq(clientId='id', clientSecret='secret'), 'a')
    t._write(ProtoOAApplicationAuthReq(clientId='id', clientSecret='secret'), 'b')
    assert budget.reserve.call_count == 1


def test_no_budget_cannot_open_connection(monkeypatch):
    connect = Mock()
    monkeypatch.setattr('services.ctrader.openapi.socket.create_connection', connect)
    with pytest.raises(CTraderUnavailable, match='requires a request budget'):
        transport().__enter__()
    connect.assert_not_called()
