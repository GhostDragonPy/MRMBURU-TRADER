"""Infrastructure worker: heartbeat only. Trading jobs arrive in later phases.
Redis contains no authoritative risk state and uses no pickle deserialization.
"""
import logging
import signal
from datetime import datetime, timezone
from threading import Event
from redis import Redis
from sqlalchemy import text
from core.config import get_settings
from core.database import session_factory


def main():
    settings=get_settings(); factory=session_factory()
    cache=Redis.from_url(settings.redis_url,socket_connect_timeout=3,socket_timeout=3)
    stop=Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    while not stop.is_set():
        try:
            with factory() as s:s.execute(text('SELECT 1'))
            cache.set('worker:heartbeat',datetime.now(timezone.utc).isoformat(),ex=30)
        except Exception:
            logging.error('Worker dependencies unavailable; execution disabled')
        stop.wait(10)
    cache.close()

if __name__=='__main__':main()
