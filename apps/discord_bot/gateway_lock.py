"""Prevent two processes from connecting with the same Discord bot token."""
import hashlib
import json
import os
import socket

LOCK_KEY = 'discord:gateway:lock'


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def owner_payload(token: str) -> str:
    return json.dumps({
        'fp': fingerprint(token),
        'host': socket.gethostname(),
        'pid': os.getpid(),
    })


def acquire(redis_client, token: str, ttl=90):
    payload = owner_payload(token)
    if redis_client.set(LOCK_KEY, payload, nx=True, ex=ttl):
        return True
    raw = redis_client.get(LOCK_KEY)
    if raw is None:
        return bool(redis_client.set(LOCK_KEY, payload, nx=True, ex=ttl))
    data = json.loads(raw if isinstance(raw, str) else raw.decode())
    if data.get('fp') == fingerprint(token) and int(data.get('pid') or 0) != os.getpid():
        raise RuntimeError(
            'Another process already holds DISCORD_BOT_TOKEN; stop nanobot or the other gateway first'
        )
    if data.get('fp') != fingerprint(token):
        raise RuntimeError('Discord gateway lock is held by a different token fingerprint')
    redis_client.set(LOCK_KEY, payload, ex=ttl)
    return True


def refresh(redis_client, token: str, ttl=90):
    redis_client.set(LOCK_KEY, owner_payload(token), xx=True, ex=ttl)


def release(redis_client, token: str):
    raw = redis_client.get(LOCK_KEY)
    if not raw:
        return
    data = json.loads(raw if isinstance(raw, str) else raw.decode())
    if data.get('fp') == fingerprint(token) and int(data.get('pid') or 0) == os.getpid():
        redis_client.delete(LOCK_KEY)
