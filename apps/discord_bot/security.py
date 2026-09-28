"""Fail-closed Discord authorization. Commands run only for acfz in #tradehouse."""
import re

class AuthorizationError(PermissionError): pass

REQUIRED_CHANNEL = 'tradehouse'
REQUIRED_OPERATOR = 'acfz'
MAX_REASON = 512
MAX_CITY = 64
MAX_QUERY = 120
MAX_REMINDER = 280
MAX_POSITION_ID = 36

_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def allowed(*, guild_id, user_id, role_ids, expected_guild_id, admin_role_id, allowed_user_ids,
            channel_name='', operator_name='',
            expected_channel=REQUIRED_CHANNEL, expected_operator=REQUIRED_OPERATOR):
    users = {int(value.strip()) for value in allowed_user_ids.split(',') if value.strip()}
    if guild_id != expected_guild_id:
        raise AuthorizationError('Server not authorized')
    if (channel_name or '').casefold() != expected_channel.casefold():
        raise AuthorizationError('Channel not authorized')
    if (operator_name or '').casefold() != expected_operator.casefold():
        raise AuthorizationError('User not authorized')
    if user_id not in users:
        raise AuthorizationError('User not authorized')
    if admin_role_id not in set(role_ids):
        raise AuthorizationError('Administrator role required')
    return True


def rate_limit(redis_client, *, user_id, limit):
    try:
        key = f'discord:rate:{user_id}'
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, 60)
        if count > limit:
            raise RuntimeError('Rate limit exceeded')
        return count
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError('Rate limit unavailable') from exc


def bounded(value, *, minimum=1, maximum=128, field='texto'):
    text = (value or '').strip()
    if _CONTROL.search(text):
        raise ValueError(f'{field} contiene caracteres no permitidos')
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(f'{field} debe tener entre {minimum} y {maximum} caracteres')
    return text
