"""MarketFeed backed by the multi-symbol Redis market bus."""
from datetime import datetime, timezone

from services.ctrader.types import CTraderUnavailable, MarketFeed
from services.market_data import bus


class RedisMarketFeed:
    """Read-only feed for one or more symbols published on market:bus:v1."""

    def __init__(self, cache, *, symbols=('EURUSD',), max_tick_age_seconds=30):
        if cache is None:
            raise CTraderUnavailable('Market cache unavailable')
        self.cache = cache
        self._symbols = tuple(bus._sym(s) for s in symbols)
        self.max_tick_age_seconds = int(max_tick_age_seconds)

    def tick(self, symbol):
        symbol = bus._sym(symbol)
        if symbol not in self._symbols:
            raise CTraderUnavailable(f'Symbol not configured: {symbol}')
        try:
            return bus.read_tick(self.cache, symbol, max_age_seconds=self.max_tick_age_seconds)
        except KeyError as exc:
            raise CTraderUnavailable(f'Market cache not ready: tick:{symbol}') from exc
        except TimeoutError as exc:
            raise CTraderUnavailable('Cached quote stale') from exc

    def ohlc(self, symbol, timeframe, count=200):
        symbol = bus._sym(symbol)
        if symbol not in self._symbols:
            raise CTraderUnavailable(f'Symbol not configured: {symbol}')
        try:
            return bus.read_bars(self.cache, symbol, timeframe, count=count)
        except KeyError as exc:
            raise CTraderUnavailable(f'Market cache not ready: bars:{symbol}:{timeframe}') from exc

    def instrument(self, symbol):
        symbol = bus._sym(symbol)
        if symbol not in self._symbols:
            raise CTraderUnavailable(f'Symbol not configured: {symbol}')
        try:
            return bus.read_instrument(self.cache, symbol)
        except KeyError as exc:
            raise CTraderUnavailable(f'Market cache not ready: instrument:{symbol}') from exc

    def symbols(self):
        return [self.instrument(symbol) for symbol in self._symbols]

    def account(self):
        raise CTraderUnavailable('Broker account polling disabled on market bus')

    def positions(self):
        raise CTraderUnavailable('Broker position polling disabled on market bus')


def feed_status(cache, symbol='EURUSD'):
    symbol = bus._sym(symbol)
    status = bus.read_status(cache, symbol)
    source = cache.get(bus.bus_key(symbol, 'source'))
    if isinstance(source, bytes):
        source = source.decode()
    tick_ok = False
    tick_age = None
    try:
        tick = bus.read_tick(cache, symbol, max_age_seconds=10_000)
        tick_ok = True
        tick_age = round((datetime.now(timezone.utc) - tick.as_of).total_seconds(), 1)
    except Exception:
        pass
    return {
        'symbol': symbol,
        'status': status,
        'source': source,
        'tick_present': tick_ok,
        'tick_age_seconds': tick_age,
        'frames': {
            tf: len(bus.read_bars(cache, symbol, tf, count=500))
            if cache.get(bus.bus_key(symbol, f'bars:{tf}')) else 0
            for tf in bus.FRAMES
        },
    }


# Protocol check for type checkers / docs.
_: type[MarketFeed] = RedisMarketFeed
