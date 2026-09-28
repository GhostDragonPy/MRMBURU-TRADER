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
            if getattr(settings, 'paper_strategy', 'sma') == 'esses-v1':
                from services.ctrader.stream import active
                if not active(datetime.now(timezone.utc)):
                    cache.set('paper:session', 'outside_ny_window')
                    stop.wait(10)
                    continue
                cache.set('paper:session', 'ny_window')
            if not settings.paper_account_id:
                raise ValueError('Configure PAPER_ACCOUNT_ID')
            with factory.begin() as session:
                if getattr(settings, 'paper_strategy', 'sma') == 'esses-v1':
                    from services.pipeline.esses import cycle as esses_cycle
                    result = esses_cycle(session, feed_from_settings(settings, cache),
                        settings.paper_account_id, cache=cache,
                        allow_unknown_news=settings.paper_allow_unknown_news)
                else:
                    result = cycle(session, feed_from_settings(settings, cache),
                        settings.paper_account_id,
                        allow_unknown_news=settings.paper_allow_unknown_news)
            cache.set('paper:last_success', datetime.now(timezone.utc).isoformat())
            cache.delete('paper:last_error')
            logging.info('Paper cycle: %s', [e['kind'] for e in result['events']])
        except Exception as exc:
            # Error text from providers may contain secrets; record only class.
            try:
                cache.set('paper:last_error', type(exc).__name__)
            except Exception:
                pass
            logging.error('Paper cycle failed: %s; no new fills committed', type(exc).__name__)
            stop.wait(10)
        stop.wait(1 if getattr(settings, 'paper_strategy', 'sma') == 'esses-v1' else 10)


def main():
    settings=get_settings(); factory=session_factory()
    cache=Redis.from_url(settings.redis_url,socket_connect_timeout=3,socket_timeout=3)
    stop=Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    task = None
    collector = None
    if settings.ctrader_network_enabled and settings.ctrader_cached_feed:
        from services.ctrader.stream import collector_loop
        collector = Thread(target=collector_loop, args=(settings, cache, stop), daemon=True)
        collector.start()
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
    if collector:
        collector.join(timeout=10)
    cache.close()

if __name__=='__main__':main()
