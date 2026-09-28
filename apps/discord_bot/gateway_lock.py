"""Prevent two processes from connecting with the same Discord bot token."""
import hashlib
import json
import os
import secrets
import socket

LOCK_KEY = 'discord:gateway:lock'
DEFAULT_TTL = 90
RELEASE_SCRIPT = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('DEL', KEYS[1]) else return 0 end"
)
REFRESH_SCRIPT = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
)


class GatewayLockError(RuntimeError):
    pass


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class GatewayLock:
    def __init__(self, token: str, ttl=DEFAULT_TTL):
        self.token = token
        self.ttl = ttl
        self.owner = secrets.token_urlsafe(16)
        self.payload = json.dumps({
            'fp': fingerprint(token),
            'owner': self.owner,
            'host': socket.gethostname(),
            'pid': os.getpid(),
        }, separators=(',', ':'))

    def acquire(self, redis_client):
        try:
            acquired = redis_client.set(LOCK_KEY, self.payload, nx=True, ex=self.ttl)
        except Exception as exc:
            raise GatewayLockError('Redis unavailable; refusing to start Discord gateway') from exc
        if acquired:
            return True
        raise GatewayLockError(
            'Another process already holds DISCORD_BOT_TOKEN; stop nanobot or the other gateway first'
        )

    def refresh(self, redis_client):
        try:
            renewed = redis_client.eval(REFRESH_SCRIPT, 1, LOCK_KEY, self.payload, str(self.ttl))
        except Exception as exc:
            raise GatewayLockError('Redis unavailable; cannot renew Discord gateway lock') from exc
        if not renewed:
            raise GatewayLockError('Discord gateway lock was lost or stolen')
        return True

    def release(self, redis_client):
        try:
            redis_client.eval(RELEASE_SCRIPT, 1, LOCK_KEY, self.payload)
        except Exception:
            return False
        return True
