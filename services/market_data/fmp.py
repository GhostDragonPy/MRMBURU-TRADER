from datetime import datetime, timezone
from urllib.parse import quote as urlquote
from services.providers.http import ProviderError, request_json

FMP_BASE = 'https://financialmodelingprep.com/stable'


class FmpUnavailable(RuntimeError):
    pass


def configured(settings):
    key = settings.fmp_api_key
    return bool(key and key.get_secret_value())


def _key(settings):
    if not configured(settings):
        raise FmpUnavailable('FMP_API_KEY is not set')
    return settings.fmp_api_key.get_secret_value()


def quote(settings, symbol):
    symbol = symbol.strip().upper()
    url = f'{FMP_BASE}/quote?symbol={urlquote(symbol)}&apikey={_key(settings)}'
    try:
        rows = request_json(url, timeout=15)
    except ProviderError as exc:
        raise FmpUnavailable(str(exc)) from exc
    if not isinstance(rows, list) or not rows:
        raise FmpUnavailable(f'No FMP quote for {symbol}')
    row = rows[0]
    return {
        'provider': 'fmp',
        'symbol': row.get('symbol', symbol),
        'price': row.get('price'),
        'change': row.get('change'),
        'changes_percentage': row.get('changePercentage') or row.get('changesPercentage'),
        'timestamp': row.get('timestamp'),
        'as_of': datetime.now(timezone.utc).isoformat(),
    }


def calendar(settings, limit=20):
    url = f'{FMP_BASE}/economic-calendar?apikey={_key(settings)}'
    try:
        rows = request_json(url, timeout=20)
    except ProviderError as exc:
        raise FmpUnavailable(str(exc)) from exc
    if not isinstance(rows, list):
        raise FmpUnavailable('Unexpected FMP calendar payload')
    events = []
    for row in rows[:limit]:
        events.append({
            'event': row.get('event'),
            'country': row.get('country'),
            'date': row.get('date'),
            'impact': row.get('impact'),
            'actual': row.get('actual'),
            'estimate': row.get('estimate'),
            'previous': row.get('previous'),
        })
    return {'provider': 'fmp', 'count': len(events), 'events': events}
