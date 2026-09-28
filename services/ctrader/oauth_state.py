"""Signed, one-time OAuth state. Never embeds tokens."""
from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
import json
from time import time
from uuid import uuid4

from services.ctrader.types import CTraderAuthRequired

NONCE_PREFIX = 'ctrader:oauth:nonce:'
ALLOWED_PURPOSES = frozenset({'market-data', 'prop-sim'})


def _signer(settings):
    return settings.admin_api_key.get_secret_value().encode()


def issue(settings, redis_client, *, purpose, expected_account, scope='trading', ttl=600):
    if purpose not in ALLOWED_PURPOSES:
        raise CTraderAuthRequired('Invalid OAuth purpose')
    nonce = uuid4().hex
    payload = {
        'purpose': purpose,
        'expected_account': str(expected_account or ''),
        'scope': scope,
        'exp': int(time()) + int(ttl),
        'nonce': nonce,
    }
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    sig = hmac_new(_signer(settings), raw, sha256).hexdigest()
    state = urlsafe_b64encode(raw).decode().rstrip('=') + '.' + sig
    redis_client.setex(NONCE_PREFIX + nonce, int(ttl), purpose)
    return state


def consume(settings, redis_client, state):
    if not state or '.' not in state:
        return None
    encoded, sig = state.rsplit('.', 1)
    pad = '=' * (-len(encoded) % 4)
    try:
        raw = urlsafe_b64decode(encoded + pad)
        expected = hmac_new(_signer(settings), raw, sha256).hexdigest()
    except Exception:
        return None
    if not compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if payload.get('purpose') not in ALLOWED_PURPOSES:
        return None
    if int(payload.get('exp') or 0) < int(time()):
        return None
    nonce = payload.get('nonce')
    key = NONCE_PREFIX + str(nonce)
    stored = redis_client.get(key)
    if not stored:
        return None
    redis_client.delete(key)
    return payload
