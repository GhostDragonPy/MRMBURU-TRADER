import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
from services.ctrader.stream import store_event, FRAMES, CachedFeed
from services.ctrader.budget import RequestBudget
from services.ctrader.types import CTraderUnavailable
from tests.test_esses import NOW, fixture


def test_collector_bootstraps_all_esses_frames():
    import inspect
    from services.ctrader.stream import collect_session, FRAMES
    assert FRAMES == ('M1', 'M5', 'M15', 'H1', 'H4', 'D1')
    source = inspect.getsource(collect_session)
    assert 'for tf in FRAMES' in source
    assert '_cached_bars' in source
    assert 'stop.wait(0.8)' in source
    cache=Mock(); quotes={}; books={tf:[] for tf in FRAMES}
    event=ProtoOASpotEvent(ctidTraderAccountId=1,symbolId=1,bid=110000,timestamp=int(NOW.timestamp()*1000))
    store_event(event,cache,'x',books,quotes,NOW)
    cache.set.assert_not_called()
    event=ProtoOASpotEvent(ctidTraderAccountId=1,symbolId=1,ask=110020,timestamp=int(NOW.timestamp()*1000))
    store_event(event,cache,'x',books,quotes,NOW)
    assert cache.set.call_count==1
    later=NOW+timedelta(seconds=40)
    event=ProtoOASpotEvent(ctidTraderAccountId=1,symbolId=1,bid=110010,timestamp=int(later.timestamp()*1000))
    store_event(event,cache,'x',books,quotes,later)
    assert cache.set.call_count==1


def test_lua_budget_shared_concurrent_and_fail_closed():
    fakeredis=pytest.importorskip('fakeredis')
    server=fakeredis.FakeServer()
    def attempt(_):
        client=fakeredis.FakeRedis(server=server)
        try:
            RequestBudget(client,123,7,7).reserve()
            return 1
        except CTraderUnavailable:
            return 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(attempt,range(30)))==7
    client=fakeredis.FakeRedis(server=server)
    assert client.zcard('ctrader:outbound:v1:123')==7
    RequestBudget(client,456,7,7).reserve()


def test_lua_rolling_window_expires_old_reservations():
    fakeredis=pytest.importorskip('fakeredis')
    client=fakeredis.FakeRedis()
    now=client.time()[0]
    client.zadd('ctrader:outbound:v1:1',{'old':now-86401,'minute-old':now-61})
    RequestBudget(client,1,2,1).reserve()
    assert client.zcard('ctrader:outbound:v1:1')==2
    with pytest.raises(CTraderUnavailable):
        RequestBudget(client,1,2,1).reserve()


def test_replay_rejects_future_context():
    from apps.esses_replay import replay
    from tests.test_simulator import META
    from services.ctrader.types import Tick
    frames=fixture()
    frames['M1'][-1]=frames['M1'][-1].model_copy(update={'closed_at':NOW+timedelta(seconds=10)})
    record={'tick':Tick(symbol='EURUSD',bid='1.112',ask='1.11202',as_of=NOW,spread_bps='0.2').model_dump(mode='json'),
        'frames':{tf:[b.model_dump(mode='json') for b in rows] for tf,rows in frames.items()},
        'instrument':META.model_dump(mode='json')}
    with pytest.raises(ValueError,match='Future candle'):
        list(replay([json.dumps(record)]))
