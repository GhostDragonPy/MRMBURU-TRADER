"""Infrastructure heartbeat and opt-in local paper simulation.
Redis contains no authoritative risk state and uses no pickle deserialization.
"""
import logging
import signal
from datetime import datetime, timezone
from threading import Event, Thread
from redis import Redis
from sqlalchemy import text
from core.config import get_settings
from core.database import session_factory


def paper_loop(settings, factory, cache, stop):
    from services.ctrader.feed import feed_from_settings
    from services.pipeline.simulator import cycle
    while not stop.is_set():
        try:
            if not settings.paper_account_id:
                raise ValueError('Configure PAPER_ACCOUNT_ID')
            with factory.begin() as session:
                result = cycle(session, feed_from_settings(settings, cache),
                    settings.paper_account_id,
                    allow_unknown_news=settings.paper_allow_unknown_news)
            cache.set('paper:last_success', datetime.now(timezone.utc).isoformat())
            cache.delete('paper:last_error')
            logging.info('Paper cycle: %s', [e['kind'] for e in result['events']])
        except Exception as exc:
            # Error text from providers may contain secrets; record only class.
            cache.set('paper:last_error', type(exc).__name__)
            logging.error('Paper cycle failed: %s; no new fills committed', type(exc).__name__)
        stop.wait(10)


def main():
    settings=get_settings(); factory=session_factory()
    cache=Redis.from_url(settings.redis_url,socket_connect_timeout=3,socket_timeout=3)
    stop=Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    task = None
    if settings.paper_scheduler_enabled:
        task = Thread(target=paper_loop, args=(settings, factory, cache, stop), daemon=True)
        task.start()
    while not stop.is_set():
        try:
            with factory() as s:s.execute(text('SELECT 1'))
            cache.set('worker:heartbeat',datetime.now(timezone.utc).isoformat(),ex=30)
        except Exception:
            logging.error('Worker dependencies unavailable; execution disabled')
        stop.wait(10)
    if task:
        task.join(timeout=5)
    cache.close()

if __name__=='__main__':main()
