from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOAGetAccountListByAccessTokenReq
from services.ctrader.openapi import ReadOnlyOpenApi
from services.ctrader.types import CTraderUnavailable


@pytest.mark.parametrize('is_live',[True,False])
def test_demo_transport_checks_account_environment_before_account_auth(monkeypatch,is_live):
    sock=Mock();connect=Mock(return_value=Mock())
    monkeypatch.setattr('services.ctrader.openapi.socket.create_connection',connect)
    context=Mock();context.wrap_socket.return_value=sock
    monkeypatch.setattr('services.ctrader.openapi.ssl.create_default_context',lambda:context)
    t=ReadOnlyOpenApi(client_id='id',client_secret='secret',access_token='token',account_id=7,budget=Mock(),environment='demo')
    calls=[]
    def request(payload):
        calls.append(type(payload))
        if isinstance(payload,ProtoOAGetAccountListByAccessTokenReq):
            return SimpleNamespace(ctidTraderAccount=[SimpleNamespace(ctidTraderAccountId=7,isLive=is_live)])
        return None
    monkeypatch.setattr(t,'request',request)
    if is_live:
        with pytest.raises(CTraderUnavailable,match='incompatible'):
            t.__enter__()
        assert ProtoOAAccountAuthReq not in calls
        sock.close.assert_called_once()
    else:
        with t:
            assert calls==[ProtoOAApplicationAuthReq,ProtoOAGetAccountListByAccessTokenReq,ProtoOAAccountAuthReq]
    connect.assert_called_once_with(('demo.ctraderapi.com',5035),10)


def test_live_transport_accepts_live_account_and_uses_live_host(monkeypatch):
    sock=Mock();connect=Mock(return_value=Mock())
    monkeypatch.setattr('services.ctrader.openapi.socket.create_connection',connect)
    context=Mock();context.wrap_socket.return_value=sock
    monkeypatch.setattr('services.ctrader.openapi.ssl.create_default_context',lambda:context)
    t=ReadOnlyOpenApi(client_id='id',client_secret='secret',access_token='token',
        account_id=7,budget=Mock(),environment='live')
    calls=[]
    def request(payload):
        calls.append(type(payload))
        if isinstance(payload,ProtoOAGetAccountListByAccessTokenReq):
            return SimpleNamespace(ctidTraderAccount=[SimpleNamespace(ctidTraderAccountId=7,isLive=True)])
        return None
    monkeypatch.setattr(t,'request',request)
    with t:
        assert calls==[ProtoOAApplicationAuthReq,ProtoOAGetAccountListByAccessTokenReq,ProtoOAAccountAuthReq]
    connect.assert_called_once_with(('live.ctraderapi.com',5035),10)


def test_cache_reads_never_call_broker(monkeypatch):
    from services.ctrader.feed import feed_from_settings
    from tests.test_api import settings
    from tests.test_simulator import META
    from services.ctrader.stream import prefix
    from tests.test_esses import fixture
    import json
    s=settings(ctrader_account_id='7',ctrader_access_token='offline-token')
    cache=Mock();key=prefix(s)
    values={key+':instrument':META.model_dump_json(),
        key+':bars:M1':json.dumps([b.model_dump(mode='json') for b in fixture()['M1']])}
    cache.get.side_effect=lambda k:values.get(k)
    broker=Mock(side_effect=AssertionError('Network forbidden'))
    monkeypatch.setattr('services.ctrader.client.open_demo',broker)
    feed=feed_from_settings(s,cache)
    assert feed.instrument('EURUSD')==META
    assert len(feed.ohlc('EURUSD','M1'))==40
    broker.assert_not_called()
