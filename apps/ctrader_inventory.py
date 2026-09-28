"""Sanitized read-only cTrader account inventory. Never prints tokens.

python -m apps.ctrader_inventory
python -m apps.ctrader_inventory --quarantine-shared
"""
import json
import sys
from services.ctrader import tokens as token_store
from services.demo_orders.transport import sanitize


def main(argv=None, *, settings=None, redis_client=None, listed=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if settings is None:
        from core.config import get_settings
        settings = get_settings()
    if redis_client is None:
        from redis import Redis
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    if '--quarantine-shared' in argv:
        result = token_store.backup_and_quarantine_shared(redis_client)
        print(json.dumps(sanitize(result), indent=2))
        return 0
    profiles = token_store.profiles_status(redis_client)
    report = {
        'profiles': profiles,
        'market_data_account': None,
        'execution': 'disabled',
        'prop_sim_execution_enabled': False,
        'accounts': [],
    }
    from services.ctrader.guards_accounts import mask_account
    if settings.ctrader_account_id:
        report['market_data_account'] = mask_account(settings.ctrader_account_id)
    if listed is not None:
        from services.ctrader.prop_sim import inventory
        report.update(inventory(listed, settings=settings))
    blob = json.dumps(sanitize(report), indent=2)
    lowered = blob.lower()
    if any(part in lowered for part in ('access_token', 'refresh_token', 'client_secret')):
        print(json.dumps({'result': 'FAIL', 'reason': 'refusing to print secrets'}))
        return 1
    print(blob)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
