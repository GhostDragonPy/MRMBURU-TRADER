"""Read-only DEMO probe: python -m apps.demo_probe

Does not send orders. Requires an injected session in tests.
Network sockets stay off unless CTRADER_NETWORK_ENABLED=true and this module
is run as a human operator command; tests never take that path.
"""
import json
import sys
from services.demo_orders.guards import DemoGuardError
from services.demo_orders.probe import evaluate
from services.demo_orders.transport import sanitize


def main(argv=None, *, settings=None, session=None, persist=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if session is None:
        print('FAIL')
        print(json.dumps({'result': 'FAIL', 'reason': 'probe_session_required_until_activation'}, indent=2))
        return 1
    from core.config import get_settings
    cfg = settings or get_settings()
    try:
        report = evaluate(cfg, session, persist=persist)
    except DemoGuardError as exc:
        print('FAIL')
        print(json.dumps({'result': 'FAIL', 'reason': str(exc)}, indent=2))
        return 1
    print(report['result'])
    print(json.dumps(sanitize(report), indent=2))
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
