"""One-time prop-sim OAuth transactions in Redis. Never stores tokens."""
from __future__ import annotations

from uuid import uuid4
import json
import secrets
from time import time

TX_PREFIX = 'ctrader:oauth:tx:'
COOKIE_NAME = 'mrmburu_prop_sim_oauth'
PROP_SIM_CALLBACK_PATH = '/research/ctrader/prop-sim/callback'
PROP_SIM_START_PATH = '/research/ctrader/prop-sim/start'
PROP_SIM_REDIRECT_URI = 'https://trader.acshop.shop/research/ctrader/prop-sim/callback'


def begin(redis_client, *, purpose, expected_account, expected_scope='trading',
          state='', ttl=600):
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
    }
    redis_client.setex(TX_PREFIX + tx_id, int(ttl), json.dumps(payload, separators=(',', ':')))
    # HMAC state is kept in a sibling key so the cookie never carries it.
    if state:
        redis_client.setex(TX_PREFIX + tx_id + ':state', int(ttl), state)
    return {'id': tx_id, **payload, 'ttl': int(ttl)}


def load(redis_client, tx_id):
    if not tx_id:
        return None
    raw = redis_client.get(TX_PREFIX + str(tx_id))
    if not raw:
        return None
    data = json.loads(raw)
    if int(data.get('exp') or 0) < int(time()):
        return None
    if data.get('purpose') != 'prop-sim':
        return None
    data['id'] = str(tx_id)
    state = redis_client.get(TX_PREFIX + str(tx_id) + ':state')
    if state:
        data['state'] = state.decode() if isinstance(state, bytes) else state
    return data


def consume(redis_client, tx_id):
    data = load(redis_client, tx_id)
    if not data:
        return None
    redis_client.delete(TX_PREFIX + str(tx_id))
    redis_client.delete(TX_PREFIX + str(tx_id) + ':state')
    return data
