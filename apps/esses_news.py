"""Load a manually reviewed EUR/USD high-impact calendar for one NY date."""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from redis import Redis
from core.config import get_settings


def validate(data):
    day = datetime.strptime(data['date'], '%Y-%m-%d').date()
    if data.get('reviewed') is not True or not isinstance(data.get('events'), list):
        raise ValueError('Expected reviewed=true and events list')
    for value in data['events']:
        t = datetime.fromisoformat(value)
        if t.tzinfo is None or t.astimezone(ZoneInfo('America/New_York')).date() != day:
            raise ValueError('Each event must have a timezone and match the NY date')
    return data


def main():
    data = validate(json.load(sys.stdin))
    with Redis.from_url(get_settings().redis_url) as cache:
        cache.set('esses:news:'+data['date'], json.dumps(data), ex=172800)
    print('Reviewed calendar saved for '+data['date'])


if __name__ == '__main__':
    main()
