import json
import secrets

TOKEN_KEY = 'ctrader:oauth'
STATE_PREFIX = 'ctrader:oauth:state:'


def begin_login(redis_client, ttl=600, scope='accounts'):
    if scope not in {'accounts', 'trading'}:
        raise ValueError('Invalid OAuth scope')
    state = secrets.token_urlsafe(24)
    redis_client.setex(STATE_PREFIX + state, ttl, scope)
    return state


def consume_state(redis_client, state):
    if not state:
        return None
    key = STATE_PREFIX + state
    raw = redis_client.get(key)
    if not raw:
        return None
    redis_client.delete(key)
    value = raw.decode() if isinstance(raw, bytes) else raw
    return value


def save_tokens(redis_client, payload, default_ttl=3600, scope=None):
    ttl = int(payload.get('expires_in') or default_ttl)
    stored_scope = scope or payload.get('scope')
    redis_client.setex(TOKEN_KEY, max(ttl, 60), json.dumps({
        'access_token': payload.get('accessToken') or payload.get('access_token'),
        'refresh_token': payload.get('refreshToken') or payload.get('refresh_token'),
        'expires_in': ttl,
        'scope': stored_scope,
    }))


def load_access_token(redis_client):
    data = load_token_record(redis_client)
    return None if data is None else data.get('access_token')


def load_token_record(redis_client):
    raw = redis_client.get(TOKEN_KEY)
    if not raw:
        return None
    return json.loads(raw)


def effective_scope(redis_client):
    data = load_token_record(redis_client)
    if not data:
        return None
    return data.get('scope')
