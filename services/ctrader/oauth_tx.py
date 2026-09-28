"""One-time prop-sim OAuth transactions in Redis. Never stores tokens."""
from __future__ import annotations

from uuid import uuid4
import json
import secrets
from time import time

TX_PREFIX = 'ctrader:oauth:tx:'
ACTIVE_KEY = TX_PREFIX + 'active'
COOKIE_NAME = 'mrmburu_prop_sim_oauth'
COOKIE_PATH = '/research/ctrader'
PROP_SIM_CALLBACK_PATH = '/research/ctrader/prop-sim/callback'
PROP_SIM_START_PATH = '/research/ctrader/prop-sim/start'
PROP_SIM_REDIRECT_URI = 'https://trader.acshop.shop/research/ctrader/prop-sim/callback'


def _as_text(value):
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


def begin(redis_client, *, purpose, expected_account, expected_scope='trading',
          state='', redirect_uri=PROP_SIM_REDIRECT_URI, ttl=600):
    if purpose != 'prop-sim':
        raise ValueError('Only prop-sim transactions are issued here')
    if expected_scope != 'trading':
        raise ValueError('prop-sim OAuth requires trading scope')
    tx_id = secrets.token_urlsafe(24)
    payload = {
        'purpose': purpose,
        'expected_account': str(expected_account),
        'expected_scope': expected_scope,
        'nonce': uuid4().hex,
        'exp': int(time()) + int(ttl),
        'state_issued': bool(state),
        'redirect_uri': redirect_uri,
    }
    redis_client.setex(TX_PREFIX + tx_id, int(ttl), json.dumps(payload, separators=(',', ':')))
    if state:
        redis_client.setex(TX_PREFIX + tx_id + ':state', int(ttl), state)
    redis_client.setex(ACTIVE_KEY, int(ttl), tx_id)
    return {'id': tx_id, **payload, 'ttl': int(ttl)}


def load(redis_client, tx_id):
    if not tx_id:
        return None
    raw = redis_client.get(TX_PREFIX + str(tx_id))
    if not raw:
        return None
    data = json.loads(_as_text(raw))
    if int(data.get('exp') or 0) < int(time()):
        return None
    if data.get('purpose') != 'prop-sim':
        return None
    data['id'] = str(tx_id)
    state = redis_client.get(TX_PREFIX + str(tx_id) + ':state')
    if state:
        data['state'] = _as_text(state)
    return data


def peek_active(redis_client):
    return load(redis_client, _as_text(redis_client.get(ACTIVE_KEY)))


def consume(redis_client, tx_id):
    data = load(redis_client, tx_id)
    if not data:
        return None
    redis_client.delete(TX_PREFIX + str(tx_id))
    redis_client.delete(TX_PREFIX + str(tx_id) + ':state')
    active = _as_text(redis_client.get(ACTIVE_KEY))
    if active == str(tx_id):
        redis_client.delete(ACTIVE_KEY)
    return data


def consume_correlated(redis_client, cookie_tx_id):
    pending = consume(redis_client, cookie_tx_id) if cookie_tx_id else None
    if pending:
        return pending
    return consume(redis_client, _as_text(redis_client.get(ACTIVE_KEY)))
