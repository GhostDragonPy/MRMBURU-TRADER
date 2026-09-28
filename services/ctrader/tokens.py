"""Separated cTrader OAuth token profiles. Never logs token values."""
from __future__ import annotations

from datetime import datetime, timezone
import json

MARKET_DATA_KEY = 'ctrader:oauth'
PROP_SIM_KEY = 'ctrader:prop-sim:oauth'
BACKUP_PREFIX = 'ctrader:oauth:backup:'
QUARANTINE_FLAG = 'ctrader:oauth:execution_quarantined'

# Backward-compatible alias used by market-data helpers.
TOKEN_KEY = MARKET_DATA_KEY


def _ttl(payload, default_ttl):
    return max(int(payload.get('expires_in') or default_ttl), 60)


def _record(payload, *, scope, profile, execution_usable=False):
    return {
        'access_token': payload.get('accessToken') or payload.get('access_token'),
        'refresh_token': payload.get('refreshToken') or payload.get('refresh_token'),
        'expires_in': int(payload.get('expires_in') or 3600),
        'scope': scope,
        'profile': profile,
        'execution_usable': bool(execution_usable),
        'account_id': str(payload.get('account_id') or ''),
    }


def save_market_data_tokens(redis_client, payload, default_ttl=3600, scope='accounts'):
    redis_client.setex(MARKET_DATA_KEY, _ttl(payload, default_ttl), json.dumps(
        _record(payload, scope=scope, profile='market-data', execution_usable=False)))


def save_prop_sim_tokens(redis_client, payload, default_ttl=3600, scope='trading', account_id='',
                         trader_login=''):
    stored = _record(payload, scope=scope, profile='prop-sim', execution_usable=False)
    stored['account_id'] = str(account_id)
    stored['trader_login'] = str(trader_login)
    redis_client.setex(PROP_SIM_KEY, _ttl(payload, default_ttl), json.dumps(stored))


def save_tokens(redis_client, payload, default_ttl=3600, scope=None):
    """Market-data profile only. Never writes the prop-sim key."""
    save_market_data_tokens(redis_client, payload, default_ttl=default_ttl,
                            scope=scope or payload.get('scope') or 'accounts')


def load_token_record(redis_client, *, profile='market-data'):
    key = PROP_SIM_KEY if profile == 'prop-sim' else MARKET_DATA_KEY
    raw = redis_client.get(key)
    if not raw:
        return None
    return json.loads(raw)


def load_access_token(redis_client):
    data = load_token_record(redis_client, profile='market-data')
    return None if data is None else data.get('access_token')


def load_prop_sim_access_token(redis_client):
    data = load_token_record(redis_client, profile='prop-sim')
    return None if data is None else data.get('access_token')


def effective_scope(redis_client):
    data = load_token_record(redis_client, profile='market-data')
    if not data:
        return None
    return data.get('scope')


def backup_and_quarantine_shared(redis_client):
    """Copy the shared market-data token, then mark it unusable for execution."""
    raw = redis_client.get(MARKET_DATA_KEY)
    if not raw:
        return {'backed_up': False, 'quarantined': False}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    redis_client.setex(BACKUP_PREFIX + stamp, 30 * 24 * 3600, raw)
    data = json.loads(raw)
    data['execution_usable'] = False
    data['quarantined'] = True
    data['profile'] = 'market-data'
    ttl = max(int(data.get('expires_in') or 3600), 60)
    redis_client.setex(MARKET_DATA_KEY, ttl, json.dumps(data))
    redis_client.setex(QUARANTINE_FLAG, ttl, '1')
    return {'backed_up': True, 'quarantined': True, 'backup_key': BACKUP_PREFIX + stamp}


def execution_quarantined(redis_client):
    return bool(redis_client.get(QUARANTINE_FLAG))


def profiles_status(redis_client):
    market = load_token_record(redis_client, profile='market-data')
    prop_sim = load_token_record(redis_client, profile='prop-sim')
    return {
        'market_data': {
            'present': market is not None,
            'profile': 'market-data',
            'scope': None if not market else market.get('scope'),
            'execution_usable': False,
            'quarantined': bool(market and market.get('quarantined')),
            'account_id': None,
        },
        'prop_sim': {
            'present': prop_sim is not None,
            'profile': 'prop-sim',
            'scope': None if not prop_sim else prop_sim.get('scope'),
            'execution_usable': False,
            'account_id': None if not prop_sim else _mask(prop_sim.get('account_id')),
            'trader_login': None if not prop_sim else _mask(prop_sim.get('trader_login')),
        },
        'execution_quarantined': execution_quarantined(redis_client),
    }


def _mask(account_id):
    text = str(account_id or '')
    if len(text) <= 4:
        return '****'
    return ('*' * (len(text) - 4)) + text[-4:]
