"""One read-only demo collector; consumers read Redis, never poll the broker."""
import json
import logging
import select
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq, ProtoOASpotEvent, ProtoOASubscribeLiveTrendbarRes,
    ProtoOASubscribeSpotsReq, ProtoOASubscribeLiveTrendbarReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
from services.ctrader.client import open_demo
from services.ctrader.feed import LiveCTraderFeed, PERIODS
from services.ctrader.types import CTraderUnavailable, Tick, OhlcBar, SymbolInfo, spread_bps

FRAMES = ('M1', 'M5', 'M15', 'H1', 'H4', 'D1')


def prefix(settings):
    return f'ctrader:stream:v1:{settings.ctrader_environment}:{settings.ctrader_account_id}'


def active(now):
    local = now.astimezone(ZoneInfo('America/New_York'))
    return local.weekday() < 5 and 555 <= local.hour*60+local.minute < 670


def fx_open(now):
    """Spot FX session: Sunday 17:00 NY through Friday 17:00 NY."""
    local = now.astimezone(ZoneInfo('America/New_York'))
    weekday, minutes = local.weekday(), local.hour * 60 + local.minute
    if weekday == 5:
        return False
    if weekday == 4 and minutes >= 17 * 60:
        return False
    if weekday == 6 and minutes < 17 * 60:
        return False
    return True


class CachedFeed:
    def __init__(self, settings, cache):
        self.cache, self.prefix = cache, prefix(settings)

    def _read(self, key):
        if self.cache is None:
            raise CTraderUnavailable('Market cache unavailable')
        raw = self.cache.get(self.prefix+':'+key)
        if not raw:
            raise CTraderUnavailable('Market cache not ready: '+key)
        return json.loads(raw)

    def tick(self, symbol):
        if symbol != 'EURUSD':
            raise CTraderUnavailable('Only EURUSD is collected')
        tick = Tick.model_validate(self._read('tick'))
        if not 0 <= (datetime.now(timezone.utc)-tick.as_of).total_seconds() <= 30:
            raise CTraderUnavailable('Cached quote stale')
        return tick

    def ohlc(self, symbol, timeframe, count=200):
        if symbol != 'EURUSD' or timeframe not in FRAMES:
            raise CTraderUnavailable('Unsupported cached market')
        return [OhlcBar.model_validate(b) for b in self._read('bars:'+timeframe)][-min(count, 500):]

    def instrument(self, symbol):
        if symbol != 'EURUSD':
            raise CTraderUnavailable('Unsupported instrument')
        return SymbolInfo.model_validate(self._read('instrument'))

    def symbols(self):
        return [self.instrument('EURUSD')]

    def account(self):
        raise CTraderUnavailable('Broker account polling disabled; use paper status')

    def positions(self):
        raise CTraderUnavailable('Broker position polling disabled; use paper status')


class ConnectedFeed(LiveCTraderFeed):
    def __init__(self, settings, cache, connection):
        super().__init__(settings, redis_client=cache)
        self.connection = connection
        self.symbols_by_name = self._symbol_map(connection, self.account_id)

    @contextmanager
    def _connection(self):
        yield self.connection

    def _find_symbol(self, connection, name):
        if name.upper() not in self.symbols_by_name:
            raise CTraderUnavailable('Exact EURUSD symbol unavailable')
        return self.symbols_by_name[name.upper()]

    def ohlc(self, symbol, timeframe, count=100):
        period_name = timeframe.upper()
        if period_name not in PERIODS:
            raise CTraderUnavailable(f'Unsupported timeframe {timeframe}')
        count = max(1, min(int(count), 200))
        now = datetime.now(timezone.utc)
        info = self._find_symbol(self.connection, symbol)
        response = self.connection.request(ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=self.account_id, symbolId=info.symbolId,
            period=ProtoOATrendbarPeriod.Value(period_name),
            fromTimestamp=int((now - timedelta(minutes=PERIODS[period_name] * count * 2)).timestamp() * 1000),
            toTimestamp=int(now.timestamp() * 1000), count=count,
        ))
        bars = []
        for row in response.trendbar[-count:]:
            bars.append(decode_bar(row, period_name))
        return [b for b in sorted(bars, key=lambda bar: bar.closed_at) if b.closed_at <= now]


def decode_bar(row, tf):
    low = D(row.low)/100000
    return OhlcBar(symbol='EURUSD', timeframe=tf, low=low,
        high=low+D(row.deltaHigh)/100000, open=low+D(row.deltaOpen)/100000,
        close=low+D(row.deltaClose)/100000, volume=D(row.volume),
        closed_at=datetime.fromtimestamp(row.utcTimestampInMinutes*60, timezone.utc)+timedelta(minutes=PERIODS[tf]))


def store_event(event, cache, key, books, quotes, now):
    """Partial bid/ask updates retain separately timestamped sides."""
    if not event.HasField('timestamp'):
        return
    ts = datetime.fromtimestamp(event.timestamp/1000, timezone.utc)
    if not 0 <= (now-ts).total_seconds() <= 30:
        return
    for side in ('bid', 'ask'):
        if event.HasField(side) and (side not in quotes or ts >= quotes[side][1]):
            quotes[side] = (D(getattr(event, side))/100000, ts)
    # Bars are processed before publishing the quote that can trigger evaluation.
    for row in event.trendbar:
        tf = ProtoOATrendbarPeriod.Name(row.period)
        if tf not in FRAMES:
            continue
        b = decode_bar(row, tf)
        if b.closed_at > ts:
            continue
        by_time = {x.closed_at:x for x in books[tf]}
        by_time[b.closed_at] = b
        books[tf] = sorted(by_time.values(), key=lambda x:x.closed_at)[-200:]
        cache.set(key+':bars:'+tf, json.dumps([x.model_dump(mode='json') for x in books[tf]]), ex=604800)
    if all(s in quotes and 0 <= (now-quotes[s][1]).total_seconds() <= 30 for s in ('bid','ask')):
        bid, ask = quotes['bid'][0], quotes['ask'][0]
        if 0 < bid <= ask:
            tick = Tick(symbol='EURUSD', bid=bid, ask=ask, spread_bps=spread_bps(bid,ask), as_of=ts)
            cache.set(key+':tick', tick.model_dump_json(), ex=35)


def _cached_bars(cache, key, tf):
    raw = cache.get(key + ':bars:' + tf)
    if not raw:
        return []
    return [OhlcBar.model_validate(row) for row in json.loads(raw)]


def bars_stale(books, now=None, *, max_age_seconds=180):
    """True when the live M1 book is missing or older than Esses will accept."""
    now = now or datetime.now(timezone.utc)
    rows = books.get('M1') or []
    if not rows:
        return True
    last = rows[-1].closed_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() > max_age_seconds


def reconnect_delay(exc=None, *, failures=0):
    """Back off hard on budget/rate limits; escalate repeated stale reconnects.

    A tight stale→bootstrap loop can burn the entire daily outbound budget
    before the NY window opens; never retry those cases every few seconds.
    """
    msg = str(exc).lower() if exc is not None else ''
    failures = max(0, int(failures))
    if 'budget exhausted' in msg or 'budget unavailable' in msg:
        return 900
    if 'rate limited' in msg:
        return 180
    if 'stale' in msg:
        return min(600, 60 * (2 ** min(failures, 3)))
    return min(300, 30 * (2 ** min(failures, 3)))


def collect_session(settings, cache, stop, lock):
    if settings.ctrader_environment not in ('demo', 'live'):
        raise CTraderUnavailable('Esses collector requires a verified read-only source account')
    key = prefix(settings)
    cache.delete(key+':tick')
    with open_demo(settings, cache) as connection:
        feed = ConnectedFeed(settings, cache, connection)
        light = feed._find_symbol(connection, 'EURUSD')
        meta = feed.instrument('EURUSD')
        cache.set(key+':instrument', meta.model_dump_json(), ex=604800)
        books = {tf: [] for tf in FRAMES}
        for tf in FRAMES:
            try:
                books[tf] = feed.ohlc('EURUSD', tf, 40)
                cache.set(key+':bars:'+tf, json.dumps([b.model_dump(mode='json') for b in books[tf]]), ex=604800)
            except CTraderUnavailable:
                books[tf] = _cached_bars(cache, key, tf)
                if not books[tf]:
                    raise
            if stop.wait(0.8):
                return
        connection.request(ProtoOASubscribeSpotsReq(ctidTraderAccountId=feed.account_id,
            symbolId=[light.symbolId], subscribeToSpotTimestamp=True))
        for tf in FRAMES:
            connection._write(ProtoOASubscribeLiveTrendbarReq(ctidTraderAccountId=feed.account_id,
                symbolId=light.symbolId, period=ProtoOATrendbarPeriod.Value(tf)), 'tb-'+tf+'-'+uuid4().hex[:8])
            connection.wait_for(ProtoOASubscribeLiveTrendbarRes)
            if stop.wait(0.4):
                return
        cache.set(key+':status', 'connected', ex=30)
        quotes = {}
        beat = 0
        # After subscribe, give live trendbars time to arrive before treating
        # bootstrap/cache age as a hard reconnect (avoids budget burn loops).
        live_since = monotonic()
        stale_grace_seconds = 180
        while not stop.is_set() and fx_open(datetime.now(timezone.utc)):
            now = datetime.now(timezone.utc)
            if bars_stale(books, now):
                if monotonic() - live_since < stale_grace_seconds:
                    cache.set(key+':status', 'waiting_live_bars', ex=60)
                else:
                    cache.set(key+':status', 'stale_bars', ex=60)
                    raise CTraderUnavailable('Cached M1 bars went stale; reconnecting collector')
            if monotonic()-beat >= 9:
                # Renew ownership BEFORE any outbound write, including heartbeat.
                lock.extend(300, replace_ttl=True)
                connection._write(ProtoHeartbeatEvent(), 'collector-heartbeat')
                if not bars_stale(books, now):
                    cache.set(key+':status', 'connected', ex=30)
                beat = monotonic()
            if connection._pending:
                envelope = connection._pending.popleft()
            elif connection._socket.pending() or select.select([connection._socket], [], [], 0.5)[0]:
                connection._deadline = monotonic()+8
                envelope = connection.receive()
            else:
                continue
            if envelope.payloadType == ProtoOASpotEvent().payloadType:
                event = ProtoOASpotEvent()
                event.ParseFromString(envelope.payload)
                if event.symbolId == light.symbolId and event.ctidTraderAccountId == feed.account_id:
                    store_event(event, cache, key, books, quotes, datetime.now(timezone.utc))


def collector_loop(settings, cache, stop):
    key = prefix(settings)
    failures = 0
    while not stop.is_set():
        if not settings.ctrader_network_enabled or not fx_open(datetime.now(timezone.utc)):
            stop.wait(10)
            continue
        lock = cache.lock(key+':owner', timeout=300, blocking=False)
        acquired = False
        delay = 30
        try:
            acquired = lock.acquire()
            if not acquired:
                stop.wait(10)
                continue
            collect_session(settings, cache, stop, lock)
        except Exception as exc:
            failures += 1
            logging.error('Collector stopped: %s', exc)
            label = 'error:' + type(exc).__name__
            try:
                msg = str(exc).lower()
                if 'budget exhausted' in msg or 'budget unavailable' in msg:
                    label = 'error:budget_exhausted'
                elif 'stale' in msg:
                    label = 'stale_bars'
                elif 'rate limited' in msg:
                    label = 'error:rate_limited'
                cache.set(key+':status', label, ex=300)
                cache.delete(key+':tick')
            except Exception:
                pass
            delay = reconnect_delay(exc, failures=failures)
            try:
                from services.ops.alerts import notify
                if 'budget' in label:
                    notify(settings, cache, kind='collector_budget',
                           message='Collector: presupuesto cTrader agotado. Backoff 15 min.')
                elif label == 'stale_bars' and failures >= 3:
                    notify(settings, cache, kind='collector_stale',
                           message=f'Collector: M1 stale repetido (fallos={failures}).')
            except Exception:
                pass
        else:
            # Session ended cleanly (FX closed or stop). Recheck soon when FX reopens.
            failures = 0
            delay = 30
        finally:
            if acquired:
                try:
                    lock.release()
                except Exception:
                    pass
        stop.wait(delay)
