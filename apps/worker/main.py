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


def maintain_broker_socket(settings, factory, cache, demo_sdk):
    mode = getattr(settings, 'trading_mode', 'paper')
    if mode not in ('demo-orders', 'prop-sim'):
        return
    import json as json_lib
    with factory.begin() as session:
        if mode == 'demo-orders':
            from services.demo_orders.factory import open_shadow_session
            from services.demo_orders.service import control
            key = 'demo:socket'
            ok_key = 'demo:preflight:ok'
        else:
            from services.prop_sim_orders.factory import open_shadow_session
            from services.prop_sim_orders.service import control
            key = 'prop-sim:socket'
            ok_key = 'prop-sim:preflight:ok'
        demo = control(session)
        if demo.rollout not in ('shadow', 'canary', 'enabled'):
            return
        sess = demo_sdk.get('session')
        if sess is None or not getattr(sess, 'healthy', False):
            try:
                sess = open_shadow_session(settings, cache, demo=demo)
                demo_sdk['session'] = sess
            except Exception as exc:
                logging.error('%s socket unavailable: %s', mode, type(exc).__name__)
                return
        if sess is not None:
            cache.set(key, json_lib.dumps(sess.snapshot_status()))
            if sess.trading_permission == 'VERIFIED':
                cache.set(ok_key, '1', ex=3600)


def paper_loop(settings, factory, cache, stop, demo_sdk=None):
    from services.ctrader.feed import feed_from_settings
    from services.pipeline.simulator import cycle
    demo_sdk = demo_sdk if demo_sdk is not None else {'session': None}
    while not stop.is_set():
        try:
            maintain_broker_socket(settings, factory, cache, demo_sdk)
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
                    if settings.trading_mode == 'demo-orders':
                        from services.ctrader import tokens as token_store
                        from services.demo_orders.factory import build_gateway, open_shadow_session
                        from services.demo_orders.service import control, on_paper_cycle
                        import json as json_lib
                        scope = token_store.effective_scope(cache) or 'accounts'
                        demo = control(session)
                        sess = demo_sdk.get('session')
                        if demo.rollout in ('shadow', 'canary', 'enabled'):
                            sess = demo_sdk.get('session')
                            if sess is None or not getattr(sess, 'healthy', False):
                                try:
                                    sess = open_shadow_session(settings, cache, demo=demo)
                                    demo_sdk['session'] = sess
                                except Exception as exc:
                                    logging.error('DEMO socket unavailable: %s', type(exc).__name__)
                                    sess = None
                            if sess is not None:
                                cache.set('demo:socket', json_lib.dumps(sess.snapshot_status()))
                                if sess.trading_permission == 'VERIFIED':
                                    cache.set('demo:preflight:ok', '1', ex=3600)
                        gw = build_gateway(settings, cache, db_session=session, token_scope=scope,
                            protobuf_session=sess, rollout=demo.rollout)
                        on_paper_cycle(session, settings, result, now=datetime.now(timezone.utc),
                            gateway=gw, token_scope=scope, redis_client=cache)
                    elif getattr(settings, 'trading_mode', 'paper') == 'prop-sim':
                        from services.ctrader import tokens as token_store
                        from services.prop_sim_orders.factory import build_gateway, open_shadow_session
                        from services.prop_sim_orders.service import control, on_paper_cycle
                        import json as json_lib
                        record = token_store.load_token_record(cache, profile='prop-sim') if cache else None
                        scope = (record or {}).get('scope') or 'accounts'
                        demo = control(session)
                        sess = demo_sdk.get('session')
                        if demo.rollout in ('shadow', 'canary', 'enabled'):
                            sess = demo_sdk.get('session')
                            if sess is None or not getattr(sess, 'healthy', False):
                                try:
                                    sess = open_shadow_session(settings, cache, demo=demo)
                                    demo_sdk['session'] = sess
                                except Exception as exc:
                                    logging.error('PROP SIM socket unavailable: %s', type(exc).__name__)
                                    sess = None
                            if sess is not None:
                                cache.set('prop-sim:socket', json_lib.dumps(sess.snapshot_status()))
                                if sess.trading_permission == 'VERIFIED':
                                    cache.set('prop-sim:preflight:ok', '1', ex=3600)
                        gw = build_gateway(settings, cache, db_session=session, token_scope=scope,
                            protobuf_session=sess, rollout=demo.rollout)
                        on_paper_cycle(session, settings, result, now=datetime.now(timezone.utc),
                            gateway=gw, token_scope=scope, redis_client=cache)
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
    demo_sdk = {'session': None}
    if settings.ctrader_network_enabled and settings.ctrader_cached_feed:
        from services.ctrader.stream import collector_loop
        collector = Thread(target=collector_loop, args=(settings, cache, stop), daemon=True)
        collector.start()
    if settings.paper_scheduler_enabled:
        task = Thread(target=paper_loop, args=(settings, factory, cache, stop, demo_sdk), daemon=True)
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
    closer = getattr(demo_sdk.get('session'), 'close', None)
    if closer:
        closer()
    cache.close()

if __name__=='__main__':main()
