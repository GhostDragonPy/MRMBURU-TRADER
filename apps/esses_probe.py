"""One explicit read-only source preflight, charged to the shared budget."""
from redis import Redis
from core.config import get_settings
from services.ctrader.client import open_demo
from services.ctrader.stream import ConnectedFeed


def main():
    settings=get_settings()
    if settings.ctrader_environment not in ('demo', 'live'):
        raise ValueError('Demo or live market-data source required')
    with Redis.from_url(settings.redis_url,socket_connect_timeout=3,socket_timeout=3) as cache:
        try:
            with open_demo(settings,cache) as connection:
                meta=ConnectedFeed(settings,cache,connection).instrument('EURUSD')
                if not all((meta.min_volume,meta.step_volume,meta.max_volume,meta.lot_size)) or meta.pip_position != 4:
                    raise ValueError('Unsupported instrument')
            print('PASS: source account verified, authentication and EURUSD metadata available; broker orders disabled.')
        except Exception as exc:
            # Never print token-containing provider exception text in installation logs.
            raise SystemExit('Source preflight failed: '+type(exc).__name__)


if __name__=='__main__':
    main()
