"""Deployment check: Redis Lua/concurrency and read-only config, no broker calls."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from redis import Redis
from core.config import get_settings
from services.ctrader.budget import RequestBudget
from services.ctrader.types import CTraderUnavailable


def main():
    settings=get_settings()
    assert settings.execution_enabled is False
    assert settings.paper_strategy=='esses-v1'
    assert settings.ctrader_cached_feed
    account='selftest-'+uuid4().hex
    cache=Redis.from_url(settings.redis_url,socket_timeout=3,socket_connect_timeout=3)
    budget=RequestBudget(cache,account,7,7)
    def attempt(_):
        try:
            budget.reserve()
            return 1
        except CTraderUnavailable:
            return 0
    try:
        cache.ping()
        with ThreadPoolExecutor(max_workers=10) as pool:
            assert sum(pool.map(attempt,range(30)))==7, 'Shared budget failed'
        assert cache.zcard(budget.key)==7
        print('PASS: Redis real, 30 concurrent attempts, exactly 7 reservations. No broker requests.')
    finally:
        cache.delete(budget.key)
        cache.close()


if __name__=='__main__':
    main()
