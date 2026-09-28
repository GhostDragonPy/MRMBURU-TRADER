"""Check the bot's fresh gateway-ready heartbeat in Redis."""
import os
from redis import Redis


def main():
    cache = Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=3, socket_timeout=3)
    try:
        if not cache.exists('discord:bot:healthy'):
            raise SystemExit(1)
    finally:
        cache.close()


if __name__ == '__main__':
    main()
