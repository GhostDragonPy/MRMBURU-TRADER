class AuthorizationError(PermissionError): pass

def allowed(*, guild_id, user_id, role_ids, expected_guild_id, admin_role_id, allowed_user_ids):
    users = {int(value.strip()) for value in allowed_user_ids.split(',') if value.strip()}
    if guild_id != expected_guild_id: raise AuthorizationError('Server not authorized')
    if user_id not in users: raise AuthorizationError('User not authorized')
    if admin_role_id not in set(role_ids): raise AuthorizationError('Administrator role required')
    return True

def rate_limit(redis_client, *, user_id, limit):
    key = f'discord:rate:{user_id}'
    count = redis_client.incr(key)
    if count == 1: redis_client.expire(key, 60)
    if count > limit: raise RuntimeError('Rate limit exceeded')
    return count
