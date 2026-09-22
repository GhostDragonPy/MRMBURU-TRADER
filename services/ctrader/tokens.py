import json
import secrets

TOKEN_KEY = 'ctrader:oauth'
STATE_PREFIX = 'ctrader:oauth:state:'


def begin_login(redis_client, ttl=600):
    state = secrets.token_urlsafe(24)
    redis_client.setex(STATE_PREFIX + state, ttl, '1')
    return state


def consume_state(redis_client, state):
    if not state:
        return False
    return bool(redis_client.delete(STATE_PREFIX + state))


def save_tokens(redis_client, payload, default_ttl=3600):
    ttl = int(payload.get('expires_in') or default_ttl)
    redis_client.setex(TOKEN_KEY, max(ttl, 60), json.dumps({
        'access_token': payload.get('accessToken') or payload.get('access_token'),
        'refresh_token': payload.get('refreshToken') or payload.get('refresh_token'),
        'expires_in': ttl,
    }))


def load_access_token(redis_client):
    raw = redis_client.get(TOKEN_KEY)
    if not raw:
        return None
    data = json.loads(raw)
    return data.get('access_token')
