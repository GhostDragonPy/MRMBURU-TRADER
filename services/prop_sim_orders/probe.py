"""Read-only prop-sim preflight. Never sends NewOrder/Close/Amend."""
from services.ctrader.prop_sim import PROP_SIM_BROKER, PROP_SIM_CTID, PROP_SIM_TRADER_LOGIN
from services.demo_orders.guards import DEMO_HOST, DemoGuardError
from services.demo_orders.preflight import mask_account
from services.demo_orders.transport import sanitize
from services.prop_sim_orders.guards import PROP_SIM_HOST
from services.prop_sim_orders.preflight import PROTOBUF_PORT, fingerprint, record_preflight

FORBIDDEN = frozenset({
    'ProtoOANewOrderReq', 'ProtoOAClosePositionReq', 'ProtoOAAmendPositionSLTPReq',
    'new_order', 'close_position', 'amend_sl_tp',
})


def evaluate(settings, session, *, persist=None, now=None, ttl_seconds=86400):
    checks = []
    writes = list(getattr(session, 'writes', []) or [])

    def add(name, ok, detail=''):
        checks.append({'check': name, 'result': 'PASS' if ok else 'FAIL', 'detail': sanitize(detail)})

    host = getattr(session, 'host', None)
    add('tls_host', host == PROP_SIM_HOST and host != DEMO_HOST, host or 'missing-host')
    add('protobuf_port', getattr(session, 'port', PROTOBUF_PORT) == PROTOBUF_PORT,
        str(getattr(session, 'port', '')))
    listed = session.authenticate(
        client_id='[redacted]', client_secret='[redacted]',
        access_token='[injected]', account_id=PROP_SIM_CTID, scope='trading')
    add('application_auth', listed.get('app_auth', True) is True)
    permission = listed.get('trading_permission') or (
        'VERIFIED' if listed.get('permission_scope') == 'trading' else 'UNVERIFIED')
    add('permission_metadata', True, listed.get('permission_metadata')
        or 'ProtoOAGetAccountListByAccessTokenRes.permissionScope')
    add('trading_permission_verified', permission == 'VERIFIED', permission)
    accounts = listed.get('accounts') or []
    add('account_list', bool(accounts), str(len(accounts)))
    add('account_configured', True, mask_account(PROP_SIM_CTID))
    match = next((row for row in accounts if str(row.get('ctidTraderAccountId')) == PROP_SIM_CTID), None)
    add('account_authorized', match is not None)
    is_live = bool(match and match.get('isLive'))
    add('is_live_true', match is not None and is_live is True)
    login_ok = str((match or {}).get('traderLogin') or PROP_SIM_TRADER_LOGIN) == PROP_SIM_TRADER_LOGIN
    add('trader_login', login_ok, mask_account(PROP_SIM_TRADER_LOGIN))
    broker_ok = str((match or {}).get('broker') or PROP_SIM_BROKER).upper() == PROP_SIM_BROKER
    add('broker_ftmo', broker_ok, PROP_SIM_BROKER)
    other_live = [row for row in accounts
                  if row.get('isLive') is True and str(row.get('ctidTraderAccountId')) != PROP_SIM_CTID]
    add('no_other_live', not other_live)
    add('account_auth', listed.get('account_auth', True) is True)
    meta = session.symbol('EURUSD', PROP_SIM_CTID)
    add('eurusd', bool(meta and meta.get('symbol_id')))
    add('min_volume', bool(meta and meta.get('min_volume')), str((meta or {}).get('min_volume')))
    add('step_volume', bool(meta and meta.get('step_volume')), str((meta or {}).get('step_volume')))
    add('digits', bool(meta and meta.get('digits')), str((meta or {}).get('digits')))
    add('pip_position', bool(meta and meta.get('pip_position')), str((meta or {}).get('pip_position')))
    snap = session.snapshot(PROP_SIM_CTID)
    add('snapshot', snap is not None and snap.get('instrument') is not None)
    add('heartbeat', bool(session.heartbeat()), 'ok' if session.heartbeat() else 'missing')
    add('persistent', bool(getattr(session, 'persistent', True)))
    later = list(getattr(session, 'writes', []) or [])
    extra = [item for item in later if item not in writes]
    add('zero_order_messages', not any(str(item) in FORBIDDEN for item in extra), '')
    real_socket = getattr(session, 'transport', '') == 'sdk-tls'
    add('sdk_tls', real_socket, getattr(session, 'transport', 'fake'))
    ok = all(item['result'] == 'PASS' for item in checks)
    persist_pass = ok and real_socket and permission == 'VERIFIED'
    if persist is not None:
        try:
            record_preflight(
                persist, settings, now=now, status='passed' if persist_pass else 'failed',
                ttl_seconds=ttl_seconds, extra_detail={
                    'trading_permission': permission,
                    'socket': 'sdk-tls' if real_socket else 'fake',
                    'permission_metadata': 'ProtoOAGetAccountListByAccessTokenRes.permissionScope',
                })
        except DemoGuardError as exc:
            add('persist', False, str(exc))
            ok = False
            persist_pass = False
    return {
        'result': 'PASS' if persist_pass else 'FAIL',
        'checks_ok': ok,
        'host': PROP_SIM_HOST,
        'port': PROTOBUF_PORT,
        'account': mask_account(PROP_SIM_CTID),
        'trader_login': mask_account(PROP_SIM_TRADER_LOGIN),
        'broker': PROP_SIM_BROKER,
        'is_live': True,
        'symbol': 'EURUSD',
        'trading_permission': permission,
        'fingerprint': fingerprint(
            account_id=PROP_SIM_CTID, trader_login=PROP_SIM_TRADER_LOGIN, broker=PROP_SIM_BROKER,
            host=PROP_SIM_HOST, environment='live'),
        'checks': checks,
    }
