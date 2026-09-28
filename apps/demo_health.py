"""Worker/DEMO healthcheck. Paper default only requires the worker heartbeat."""
import os
from redis import Redis


def main():
    cache = Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=3, socket_timeout=3)
    try:
        if not cache.exists('worker:heartbeat'):
            raise SystemExit(1)
        mode = (os.environ.get('TRADING_MODE') or 'paper').strip()
        demo_on = (os.environ.get('DEMO_EXECUTION_ENABLED') or 'false').strip().lower() == 'true'
        if mode == 'demo-orders' and demo_on and not cache.exists('demo:preflight:ok'):
            raise SystemExit(1)
    finally:
        cache.close()


if __name__ == '__main__':
    main()
