"""Shared, fail-closed outbound message budget; not a broker compliance guarantee."""
from uuid import uuid4

from services.ctrader.types import CTraderUnavailable

# Redis time and one atomic script prevent worker/API races and midnight bursts.
# Charge attempted writes; never refund a possibly transmitted message.
RESERVE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 86400)
local daily = redis.call('ZCARD', KEYS[1])
local minute = redis.call('ZCOUNT', KEYS[1], '(' .. (now - 60), '+inf')
if daily >= tonumber(ARGV[1]) or minute >= tonumber(ARGV[2]) then
    return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('EXPIRE', KEYS[1], 86401)
return 1
"""


class RequestBudget:
    def __init__(self, redis_client, account_id, daily, minute):
        self.redis = redis_client
        self.key = f'ctrader:outbound:v1:{account_id}'
        self.daily = daily
        self.minute = minute

    def reserve(self):
        try:
            allowed = self.redis.eval(
                RESERVE, 1, self.key, self.daily, self.minute, uuid4().hex)
        except Exception:
            raise CTraderUnavailable('cTrader request budget unavailable; network blocked') from None
        if allowed != 1:
            raise CTraderUnavailable('cTrader request budget exhausted; network blocked')
