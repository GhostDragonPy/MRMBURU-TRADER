from decimal import Decimal
from types import SimpleNamespace

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsRes, ProtoOANewOrderReq, ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq, ProtoOASubscribeSpotsRes,
    ProtoOASymbolsListReq, ProtoOASymbolsListRes,
)

from services.ctrader import client as auth
from services.ctrader.feed import LiveCTraderFeed
from services.ctrader.openapi import ReadOnlyOpenApi
from services.ctrader.types import CTraderUnavailable


class FakeConnection:
    def __init__(self, responses, event=None):
        self.responses = responses
        self.event = event

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, request):
        return self.responses[type(request)]

    def wait_for(self, response_class, predicate):
        assert isinstance(self.event, response_class)
        assert predicate(self.event)
        return self.event


def _settings():
    return SimpleNamespace(ctrader_account_id='123')


def _symbols():
    response = ProtoOASymbolsListRes(ctidTraderAccountId=123)
    response.symbol.add(symbolId=1, symbolName='EURUSD', enabled=True)
    return response


def test_tick_uses_protobuf_spot_event(monkeypatch):
    event = ProtoOASpotEvent(
        ctidTraderAccountId=123, symbolId=1,
        bid=110000, ask=110020, timestamp=1_700_000_000_000,
    )
    fake = FakeConnection({
        ProtoOASymbolsListReq: _symbols(),
        ProtoOASubscribeSpotsReq: ProtoOASubscribeSpotsRes(ctidTraderAccountId=123),
    }, event=event)
    monkeypatch.setattr(auth, 'open_demo', lambda *_: fake)

    tick = LiveCTraderFeed(_settings()).tick('eurusd')

    assert str(tick.bid) == '1.1'
    assert str(tick.ask) == '1.1002'
    assert tick.symbol == 'EURUSD'


def test_ohlc_decodes_relative_prices(monkeypatch):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq
    bars = ProtoOAGetTrendbarsRes(ctidTraderAccountId=123, period=7,
                                  timestamp=1_700_000_000_000, symbolId=1)
    bars.trendbar.add(low=109900, deltaOpen=50, deltaHigh=150,
                      deltaClose=100, volume=42, utcTimestampInMinutes=28_333_333)
    fake = FakeConnection({ProtoOASymbolsListReq: _symbols(), ProtoOAGetTrendbarsReq: bars})
    monkeypatch.setattr(auth, 'open_demo', lambda *_: fake)

    bar = LiveCTraderFeed(_settings()).ohlc('EURUSD', 'M15', 1)[0]

    assert str(bar.low) == '1.099'
    assert str(bar.open) == '1.0995'
    assert str(bar.high) == '1.1005'
    assert bar.close == Decimal('1.1')
    from datetime import datetime, timezone
    assert bar.closed_at == datetime.fromtimestamp(28_333_333*60+900, tz=timezone.utc)


def test_transport_rejects_order_messages_before_network_use():
    transport = ReadOnlyOpenApi(
        client_id='id', client_secret='secret', access_token='token', account_id=123,
    )
    with pytest.raises(CTraderUnavailable, match='Blocked non-read-only'):
        transport.request(ProtoOANewOrderReq(
            ctidTraderAccountId=123, symbolId=1, orderType=1, tradeSide=1, volume=100,
        ))

def test_missing_quote_timestamp_is_not_synthesized(monkeypatch):
    event = ProtoOASpotEvent(ctidTraderAccountId=123, symbolId=1, bid=110000, ask=110020)
    fake = FakeConnection({ProtoOASymbolsListReq: _symbols(),
        ProtoOASubscribeSpotsReq: ProtoOASubscribeSpotsRes(ctidTraderAccountId=123)}, event)
    monkeypatch.setattr(auth, 'open_demo', lambda *_: fake)
    with pytest.raises(CTraderUnavailable, match='timestamp'):
        LiveCTraderFeed(_settings()).tick('EURUSD')

def test_current_bar_excluded(monkeypatch):
    from datetime import datetime, timezone
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq
    current = int(datetime.now(timezone.utc).timestamp() // 900)*15
    response = ProtoOAGetTrendbarsRes(ctidTraderAccountId=123, period=7,
        timestamp=1, symbolId=1)
    for minute in [current-15,current]:
        response.trendbar.add(low=110000,deltaOpen=0,deltaHigh=10,deltaClose=5,
            volume=1,utcTimestampInMinutes=minute)
    fake=FakeConnection({ProtoOASymbolsListReq:_symbols(),ProtoOAGetTrendbarsReq:response})
    monkeypatch.setattr(auth,'open_demo',lambda *_:fake)
    bars=LiveCTraderFeed(_settings()).ohlc('EURUSD','M15',2)
    assert len(bars)==1
    assert bars[0].closed_at==datetime.fromtimestamp(current*60,tz=timezone.utc)

def test_transport_blocks_close_and_amend():
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq, ProtoOAAmendPositionSLTPReq
    transport=ReadOnlyOpenApi(client_id='id',client_secret='secret',access_token='token',account_id=1)
    for payload in [ProtoOAClosePositionReq(),ProtoOAAmendPositionSLTPReq()]:
        with pytest.raises(CTraderUnavailable,match='Blocked non-read-only'):
            transport.request(payload)

def test_expired_transport_deadline():
    transport=ReadOnlyOpenApi(client_id='id',client_secret='secret',access_token='token',account_id=1)
    transport._deadline=0
    with pytest.raises(CTraderUnavailable,match='deadline'):
        transport._read_exactly(4)
