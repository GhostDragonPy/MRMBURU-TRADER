"""Read-only prop-sim probe: python -m apps.prop_sim_probe

Opens a real SDK LIVE socket when no session is injected. Never sends orders.
"""
import json
import sys
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.demo_orders.guards import DemoGuardError
from services.demo_orders.transport import sanitize
from services.prop_sim_orders.probe import evaluate


def main(argv=None, *, settings=None, session=None, persist=None, redis_client=None, driver=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    owned = False
    cfg = settings
    try:
        if cfg is None:
            from core.config import get_settings
            cfg = get_settings()
        if session is None:
            from services.prop_sim_orders.sdk_session import SdkPropSimSessionFactory
            session = SdkPropSimSessionFactory().open_probe(cfg, redis_client, driver=driver)
            owned = True
        report = evaluate(cfg, session, persist=persist)
    except (DemoGuardError, CTraderAuthRequired, CTraderUnavailable) as exc:
        print('FAIL')
        print(json.dumps({'result': 'FAIL', 'reason': sanitize(str(exc))}, indent=2))
        return 1
    finally:
        if owned and session is not None:
            closer = getattr(session, 'close', None)
            if closer:
                closer()
    print(report['result'])
    print(json.dumps(sanitize(report), indent=2))
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
