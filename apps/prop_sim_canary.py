"""Authorized one-shot FTMO Free Trial canary: python -m apps.prop_sim_canary

Places exactly one EURUSD MARKET order with label MRMBURU-CANARY, then closes it.
Never prints secrets. Never targets any LIVE account except the authorized tuple.
"""
import json
import sys
from services.demo_orders.guards import DemoGuardError
from services.demo_orders.transport import sanitize


def _mask_id(value):
    text = str(value or '')
    if len(text) <= 4:
        return '****'
    return text[:2] + '***' + text[-2:]


def main(argv=None, *, settings=None, session=None, factory=None, redis_client=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    owned = False
    try:
        if settings is None:
            from core.config import get_settings
            settings = get_settings()
        if factory is None:
            from core.database import session_factory
            factory = session_factory()
        if redis_client is None:
            from redis import Redis
            redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        from services.prop_sim_orders.factory import build_gateway, open_shadow_session
        from services.prop_sim_orders.service import control, place_diagnostic_canary
        with factory.begin() as db:
            demo = control(db)
            if session is None:
                session = open_shadow_session(settings, redis_client, demo=demo)
                owned = True
            gw = build_gateway(settings, redis_client, db_session=db, token_scope='trading',
                               protobuf_session=session, rollout=demo.rollout)
            result = place_diagnostic_canary(db, settings, gw)
        report = sanitize({
            'result': 'PASS',
            'order_id': _mask_id(result.get('order_id')),
            'position_id': _mask_id(result.get('position_id')),
            'sl_confirmed': result.get('sl_confirmed'),
            'tp_confirmed': result.get('tp_confirmed'),
            'closed': result.get('closed'),
            'kind': result.get('kind'),
            'residual_positions': result.get('residual_positions'),
            'residual_orders': result.get('residual_orders'),
        })
        print('PASS')
        print(json.dumps(report, indent=2))
        return 0
    except DemoGuardError as exc:
        print('FAIL')
        print(json.dumps(sanitize({'result': 'FAIL', 'reason': str(exc)}), indent=2))
        return 1
    finally:
        if owned and session is not None:
            closer = getattr(session, 'close', None)
            if closer:
                closer()


if __name__ == '__main__':
    raise SystemExit(main())
