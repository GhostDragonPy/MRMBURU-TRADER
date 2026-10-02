"""Multi-symbol market bus in Redis.

Future MT5 / external bridges publish here. Esses can keep reading the legacy
cTrader cache until we flip PAPER market data to this bus.

Key layout (case-insensitive symbols stored UPPER):
  market:bus:v1:{SYMBOL}:tick
  market:bus:v1:{SYMBOL}:bars:{TF}      TF in M1,M5,M15,H1,H4,D1
  market:bus:v1:{SYMBOL}:instrument
  market:bus:v1:{SYMBOL}:status
  market:bus:v1:{SYMBOL}:source        mt5|external|ctrader
  market:bus:v1:symbols                JSON list of active symbols

Optional compatibility mirror for current Esses EURUSD path:
  ctrader:stream:v1:{env}:{account}:tick|bars:*|instrument|status
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from services.ctrader.types import OhlcBar, SymbolInfo, Tick, spread_bps

BUS_PREFIX = 'market:bus:v1'
FRAMES = ('M1', 'M5', 'M15', 'H1', 'H4', 'D1')
SOURCES = frozenset({'mt5', 'external', 'ctrader'})
DEFAULT_TTL_TICK = 35
DEFAULT_TTL_BARS = 604800
DEFAULT_TTL_STATUS = 120


def _sym(symbol: str) -> str:
    return str(symbol or '').strip().upper()


def bus_key(symbol: str, leaf: str) -> str:
    return f'{BUS_PREFIX}:{_sym(symbol)}:{leaf}'


def symbols_index_key() -> str:
    return f'{BUS_PREFIX}:symbols'


def normalize_source(source: str | None) -> str:
    value = str(source or 'external').strip().lower()
    if value not in SOURCES:
        raise ValueError(f'Unsupported market source: {source}')
    return value


def publish_tick(cache, *, symbol, bid, ask, as_of=None, source='mt5', ttl=DEFAULT_TTL_TICK):
    symbol = _sym(symbol)
    source = normalize_source(source)
    as_of = as_of or datetime.now(timezone.utc)
    if getattr(as_of, 'tzinfo', None) is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    bid_d, ask_d = Decimal(str(bid)), Decimal(str(ask))
    if not (0 < bid_d <= ask_d):
        raise ValueError('Invalid bid/ask')
    tick = Tick(
        symbol=symbol, bid=bid_d, ask=ask_d,
        spread_bps=spread_bps(bid_d, ask_d), as_of=as_of, source=source,
    )
    cache.set(bus_key(symbol, 'tick'), tick.model_dump_json(), ex=int(ttl))
    cache.set(bus_key(symbol, 'source'), source, ex=max(int(ttl), DEFAULT_TTL_STATUS))
    _touch_symbol(cache, symbol)
    return tick


def publish_bars(cache, *, symbol, timeframe, bars, source='mt5', ttl=DEFAULT_TTL_BARS):
    symbol = _sym(symbol)
    timeframe = str(timeframe).upper()
    if timeframe not in FRAMES:
        raise ValueError(f'Unsupported timeframe {timeframe}')
    source = normalize_source(source)
    rows = []
    for bar in bars:
        if isinstance(bar, OhlcBar):
            payload = bar.model_dump(mode='json')
            payload['symbol'] = symbol
            payload['timeframe'] = timeframe
            payload['source'] = source
            rows.append(payload)
        else:
            row = dict(bar)
            row['symbol'] = symbol
            row['timeframe'] = timeframe
            row['source'] = source
            # Validate shape early.
            OhlcBar.model_validate(row)
            rows.append(OhlcBar.model_validate(row).model_dump(mode='json'))
    rows = sorted(rows, key=lambda b: b['closed_at'])[-500:]
    cache.set(bus_key(symbol, f'bars:{timeframe}'), json.dumps(rows), ex=int(ttl))
    cache.set(bus_key(symbol, 'source'), source, ex=DEFAULT_TTL_STATUS)
    _touch_symbol(cache, symbol)
    return len(rows)


def publish_instrument(cache, *, symbol, digits=5, pip_position=4, lot_size='100000',
                       min_volume='1000', max_volume='100000000', step_volume='1000',
                       source='mt5', ttl=DEFAULT_TTL_BARS):
    symbol = _sym(symbol)
    source = normalize_source(source)
    info = SymbolInfo(
        name=symbol, digits=int(digits), pip_position=int(pip_position),
        lot_size=Decimal(str(lot_size)), min_volume=Decimal(str(min_volume)),
        max_volume=Decimal(str(max_volume)), step_volume=Decimal(str(step_volume)),
    )
    cache.set(bus_key(symbol, 'instrument'), info.model_dump_json(), ex=int(ttl))
    cache.set(bus_key(symbol, 'source'), source, ex=DEFAULT_TTL_STATUS)
    _touch_symbol(cache, symbol)
    return info


def publish_status(cache, *, symbol, status, source='mt5', ttl=DEFAULT_TTL_STATUS):
    symbol = _sym(symbol)
    source = normalize_source(source)
    cache.set(bus_key(symbol, 'status'), str(status)[:64], ex=int(ttl))
    cache.set(bus_key(symbol, 'source'), source, ex=int(ttl))
    _touch_symbol(cache, symbol)
    return str(status)[:64]


def _touch_symbol(cache, symbol):
    symbol = _sym(symbol)
    raw = cache.get(symbols_index_key())
    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        rows = []
    if symbol not in rows:
        rows.append(symbol)
        cache.set(symbols_index_key(), json.dumps(sorted(rows)))


def list_symbols(cache):
    raw = cache.get(symbols_index_key())
    if not raw:
        return []
    try:
        return list(json.loads(raw))
    except (TypeError, json.JSONDecodeError):
        return []


def read_tick(cache, symbol, *, max_age_seconds=30):
    raw = cache.get(bus_key(symbol, 'tick'))
    if not raw:
        raise KeyError('tick missing')
    tick = Tick.model_validate_json(raw if isinstance(raw, str) else raw.decode())
    age = (datetime.now(timezone.utc) - tick.as_of).total_seconds()
    if not 0 <= age <= max_age_seconds:
        raise TimeoutError('tick stale')
    return tick


def read_bars(cache, symbol, timeframe, count=200):
    timeframe = str(timeframe).upper()
    raw = cache.get(bus_key(symbol, f'bars:{timeframe}'))
    if not raw:
        raise KeyError('bars missing')
    rows = json.loads(raw if isinstance(raw, str) else raw.decode())
    return [OhlcBar.model_validate(row) for row in rows][-min(int(count), 500):]


def read_instrument(cache, symbol):
    raw = cache.get(bus_key(symbol, 'instrument'))
    if not raw:
        raise KeyError('instrument missing')
    return SymbolInfo.model_validate_json(raw if isinstance(raw, str) else raw.decode())


def read_status(cache, symbol):
    raw = cache.get(bus_key(symbol, 'status'))
    if raw is None:
        return None
    return raw if isinstance(raw, str) else raw.decode()


def mirror_eurusd_to_ctrader_cache(cache, settings):
    """Copy EURUSD bus data into the legacy Esses CachedFeed keys.

    Lets an MT5 bridge feed the current worker without changing Esses yet.
    """
    from services.ctrader.stream import prefix

    symbol = 'EURUSD'
    key = prefix(settings)
    tick_raw = cache.get(bus_key(symbol, 'tick'))
    if tick_raw:
        cache.set(key + ':tick', tick_raw if isinstance(tick_raw, (str, bytes)) else tick_raw, ex=DEFAULT_TTL_TICK)
    for tf in FRAMES:
        bars_raw = cache.get(bus_key(symbol, f'bars:{tf}'))
        if bars_raw:
            cache.set(key + f':bars:{tf}', bars_raw, ex=DEFAULT_TTL_BARS)
    inst_raw = cache.get(bus_key(symbol, 'instrument'))
    if inst_raw:
        cache.set(key + ':instrument', inst_raw, ex=DEFAULT_TTL_BARS)
    status = read_status(cache, symbol) or 'connected'
    cache.set(key + ':status', status, ex=DEFAULT_TTL_STATUS)
    return {'mirrored': True, 'symbol': symbol, 'prefix': key}


def ingest_payload(cache, payload, *, settings=None, mirror_legacy=False):
    """Accept a bridge JSON payload and publish to the bus.

    Expected shape:
    {
      "source": "mt5",
      "symbol": "EURUSD",
      "status": "connected",
      "tick": {"bid": "1.1", "ask": "1.1001", "as_of": "..."},
      "instrument": {"digits": 5, ...},
      "bars": {"M1": [ {...}, ... ], "H1": [...]}
    }
    """
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    source = normalize_source(payload.get('source') or 'mt5')
    symbol = _sym(payload.get('symbol') or 'EURUSD')
    published = {'symbol': symbol, 'source': source, 'bars': {}}
    if payload.get('status'):
        publish_status(cache, symbol=symbol, status=payload['status'], source=source)
        published['status'] = str(payload['status'])
    if payload.get('instrument'):
        inst = dict(payload['instrument'])
        published['instrument'] = publish_instrument(
            cache, symbol=symbol, source=source, **{
                k: inst[k] for k in (
                    'digits', 'pip_position', 'lot_size', 'min_volume',
                    'max_volume', 'step_volume',
                ) if k in inst
            }).model_dump(mode='json')
    if payload.get('tick'):
        tick = dict(payload['tick'])
        as_of = tick.get('as_of')
        if isinstance(as_of, str):
            as_of = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
        published['tick'] = publish_tick(
            cache, symbol=symbol, bid=tick['bid'], ask=tick['ask'],
            as_of=as_of, source=source,
        ).model_dump(mode='json')
    bars = payload.get('bars') or {}
    if not isinstance(bars, dict):
        raise ValueError('bars must be an object of timeframe -> rows')
    for tf, rows in bars.items():
        published['bars'][str(tf).upper()] = publish_bars(
            cache, symbol=symbol, timeframe=tf, bars=rows, source=source)
    if mirror_legacy and symbol == 'EURUSD' and settings is not None:
        published['legacy_mirror'] = mirror_eurusd_to_ctrader_cache(cache, settings)
    return published
