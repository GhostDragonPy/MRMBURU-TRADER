from datetime import datetime, timezone
from urllib.parse import quote as urlquote
from services.providers.http import ProviderError, request_json

FRED_BASE = 'https://api.stlouisfed.org/fred'

# Common trading aliases → FRED series (daily FX / macro).
SERIES = {
    'EURUSD': 'DEXUSEU',
    'GBPUSD': 'DEXUSUK',
    'USDJPY': 'DEXJPUS',
    'USDCAD': 'DEXCAUS',
    'AUDUSD': 'DEXUSAL',
    'NZDUSD': 'DEXUSNZ',
    'USDCHF': 'DEXSZUS',
    'DXY': 'DTWEXBGS',
    'VIX': 'VIXCLS',
    'FEDFUNDS': 'FEDFUNDS',
    'CPI': 'CPIAUCSL',
    'UNRATE': 'UNRATE',
    'DGS10': 'DGS10',
}


class FredUnavailable(RuntimeError):
    pass


def configured(settings):
    key = settings.fred_api_key
    return bool(key and key.get_secret_value())


def _key(settings):
    if not configured(settings):
        raise FredUnavailable('FRED_API_KEY is not set')
    return settings.fred_api_key.get_secret_value()


def resolve_series(symbol):
    raw = (symbol or '').strip().upper()
    if not raw:
        raise FredUnavailable('Symbol or FRED series id required')
    return SERIES.get(raw, raw)


def quote(settings, symbol):
    series_id = resolve_series(symbol)
    url = (
        f'{FRED_BASE}/series/observations?series_id={urlquote(series_id)}'
        f'&api_key={_key(settings)}&file_type=json&sort_order=desc&limit=5'
    )
    try:
        data = request_json(url, timeout=20)
    except ProviderError as exc:
        raise FredUnavailable(str(exc)) from exc
    rows = [row for row in data.get('observations', []) if row.get('value') not in (None, '.', '')]
    if not rows:
        raise FredUnavailable(f'No FRED observations for {series_id}')
    latest = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    price = float(latest['value'])
    change = None
    if previous:
        change = price - float(previous['value'])
    return {
        'provider': 'fred',
        'symbol': symbol.strip().upper(),
        'series_id': series_id,
        'price': price,
        'change': change,
        'observation_date': latest.get('date'),
        'as_of': datetime.now(timezone.utc).isoformat(),
    }


def calendar(settings, limit=20):
    url = f'{FRED_BASE}/releases/dates?api_key={_key(settings)}&file_type=json&limit={int(limit)}&sort_order=desc'
    try:
        data = request_json(url, timeout=20)
    except ProviderError as exc:
        raise FredUnavailable(str(exc)) from exc
    events = []
    for row in data.get('release_dates', [])[:limit]:
        events.append({
            'release_id': row.get('release_id'),
            'release_name': row.get('release_name'),
            'date': row.get('date'),
        })
    return {'provider': 'fred', 'count': len(events), 'events': events}
