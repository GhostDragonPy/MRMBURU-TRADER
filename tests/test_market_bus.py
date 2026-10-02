from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.ctrader.stream import CachedFeed, prefix
from services.ctrader.types import CTraderUnavailable
from services.market_data import bus
from services.market_data.redis_feed import RedisMarketFeed, feed_status
from tests.test_api import ADMIN, RESEARCH, settings

NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Cache:
    def __init__(self):
        self.store = {}

    def ping(self):
        return True

    def close(self):
        pass

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, px=None, nx=False, xx=False):
        self.store[key] = value
        return True

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _bar(closed_at, close='1.08500'):
    return {
        'open': '1.08490', 'high': '1.08520', 'low': '1.08480', 'close': close,
        'volume': '10', 'closed_at': closed_at.isoformat().replace('+00:00', 'Z'),
    }


def test_publish_and_read_tick_and_bars():
    cache = Cache()
    tick = bus.publish_tick(
        cache, symbol='eurusd', bid='1.08500', ask='1.08512', as_of=NOW, source='mt5')
    assert tick.symbol == 'EURUSD'
    assert tick.source == 'mt5'
    assert tick.spread_bps > 0
    assert bus.list_symbols(cache) == ['EURUSD']
    assert bus.read_tick(cache, 'EURUSD', max_age_seconds=10_000).bid == Decimal('1.08500')

    n = bus.publish_bars(
        cache, symbol='EURUSD', timeframe='M1',
        bars=[_bar(NOW - timedelta(minutes=2)), _bar(NOW - timedelta(minutes=1))],
        source='mt5')
    assert n == 2
    rows = bus.read_bars(cache, 'EURUSD', 'M1')
    assert len(rows) == 2
    assert rows[-1].source == 'mt5'
    assert rows[-1].symbol == 'EURUSD'


def test_reject_bad_source_and_stale_tick():
    cache = Cache()
    with pytest.raises(ValueError):
        bus.publish_tick(cache, symbol='EURUSD', bid='1.1', ask='1.2', source='binance')
    bus.publish_tick(
        cache, symbol='EURUSD', bid='1.1', ask='1.1001',
        as_of=NOW - timedelta(seconds=120), source='external')
    with pytest.raises(TimeoutError):
        bus.read_tick(cache, 'EURUSD', max_age_seconds=30)


def test_redis_market_feed_and_legacy_mirror(factory):
    cache = Cache()
    cfg = settings(ctrader_account_id='17204978', ctrader_environment='demo')
    bus.ingest_payload(cache, {
        'source': 'mt5',
        'symbol': 'EURUSD',
        'status': 'connected',
        'tick': {'bid': '1.08500', 'ask': '1.08512', 'as_of': NOW.isoformat()},
        'instrument': {
            'digits': 5, 'pip_position': 4, 'lot_size': '100000',
            'min_volume': '1000', 'max_volume': '100000000', 'step_volume': '1000',
        },
        'bars': {'M1': [_bar(NOW - timedelta(minutes=1))]},
    }, settings=cfg, mirror_legacy=True)

    feed = RedisMarketFeed(cache, symbols=('EURUSD',), max_tick_age_seconds=10_000)
    assert feed.tick('EURUSD').source == 'mt5'
    assert feed.ohlc('EURUSD', 'M1')
    assert feed.instrument('EURUSD').digits == 5
    assert [row.name for row in feed.symbols()] == ['EURUSD']
    with pytest.raises(CTraderUnavailable):
        feed.account()

    legacy = CachedFeed(cfg, cache)
    assert legacy.tick('EURUSD').bid == Decimal('1.08500')
    assert legacy.ohlc('EURUSD', 'M1')
    assert legacy.instrument('EURUSD').name == 'EURUSD'
    assert cache.get(prefix(cfg) + ':status') in (b'connected', 'connected')

    status = feed_status(cache, 'EURUSD')
    assert status['source'] == 'mt5'
    assert status['tick_present'] is True
    assert status['frames']['M1'] == 1


def test_market_bus_api(factory):
    cache = Cache()
    cfg = settings(ctrader_account_id='17204978', ctrader_environment='demo')
    app = create_app(cfg, factory, cache)
    with TestClient(app) as client:
        denied = client.post('/research/market-bus/ingest', json={
            'source': 'mt5', 'symbol': 'EURUSD',
            'tick': {'bid': '1.08500', 'ask': '1.08512', 'as_of': NOW.isoformat()},
        })
        assert denied.status_code in (401, 403, 422)

        research_denied = client.post(
            '/research/market-bus/ingest',
            headers={'X-API-Key': RESEARCH},
            json={
                'source': 'mt5', 'symbol': 'EURUSD',
                'tick': {'bid': '1.08500', 'ask': '1.08512', 'as_of': NOW.isoformat()},
            })
        assert research_denied.status_code == 401

        ok = client.post(
            '/research/market-bus/ingest',
            headers={'X-API-Key': ADMIN},
            json={
                'source': 'mt5',
                'symbol': 'gbpusd',
                'status': 'connected',
                'mirror_legacy': False,
                'tick': {'bid': '1.25000', 'ask': '1.25020', 'as_of': NOW.isoformat()},
                'instrument': {'digits': 5, 'pip_position': 4},
                'bars': {'H1': [_bar(NOW - timedelta(hours=1), close='1.25000')]},
            })
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body['ok'] is True
        assert body['published']['symbol'] == 'GBPUSD'
        assert body['published']['bars']['H1'] == 1

        eurusd = client.post(
            '/research/market-bus/ingest',
            headers={'X-API-Key': ADMIN},
            json={
                'source': 'mt5',
                'symbol': 'EURUSD',
                'status': 'connected',
                'mirror_legacy': True,
                'tick': {'bid': '1.08500', 'ask': '1.08512', 'as_of': NOW.isoformat()},
                'instrument': {'digits': 5},
                'bars': {'M1': [_bar(NOW - timedelta(minutes=1))]},
            })
        assert eurusd.status_code == 200
        assert eurusd.json()['published']['legacy_mirror']['mirrored'] is True

        status = client.get(
            '/research/market-bus/status',
            headers={'X-API-Key': RESEARCH},
            params={'symbol': 'EURUSD'})
        assert status.status_code == 200
        assert status.json()['source'] == 'mt5'
        assert status.json()['tick_present'] is True

        symbols = client.get('/research/market-bus/symbols', headers={'X-API-Key': RESEARCH})
        assert symbols.status_code == 200
        assert set(symbols.json()['symbols']) >= {'EURUSD', 'GBPUSD'}

        providers = client.get('/research/providers', headers={'X-API-Key': RESEARCH})
        assert providers.status_code == 200
        assert providers.json()['market_bus']['contract'] == 'market:bus:v1'
        assert 'mt5' in providers.json()['market_bus']['sources']
