"""Read-only DEMO preflight. Never sends NewOrder/Close/Amend."""
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, LIVE_HOST, DemoGuardError
from services.demo_orders.preflight import PROTOBUF_PORT, fingerprint, mask_account, record_preflight
from services.demo_orders.transport import sanitize

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
    add('tls_host', host == DEMO_HOST and host != LIVE_HOST, host or 'missing-host')
    add('protobuf_port', getattr(session, 'port', PROTOBUF_PORT) == PROTOBUF_PORT, str(getattr(session, 'port', '')))
    listed = session.authenticate(
        client_id='[redacted]', client_secret='[redacted]',
        access_token='[injected]', account_id=settings.demo_ctrader_account_id,
        scope='trading')
    add('application_auth', listed.get('app_auth', True) is True)
    permission = listed.get('trading_permission') or (
        'VERIFIED' if listed.get('permission_scope') == 'trading' else 'UNVERIFIED')
    add('permission_metadata', True, listed.get('permission_metadata')
        or 'ProtoOAGetAccountListByAccessTokenRes.permissionScope')
    add('trading_permission_verified', permission == 'VERIFIED', permission)
    accounts = listed.get('accounts') or []
    add('account_list', bool(accounts), str(len(accounts)))
    account = str(settings.demo_ctrader_account_id or '')
    add('account_configured', bool(account), mask_account(account))
    add('denylist', account not in LIVE_ACCOUNT_IDS, mask_account(account))
    match = next((row for row in accounts if str(row.get('ctidTraderAccountId')) == account), None)
    add('account_authorized', match is not None)
    is_live = bool(match and match.get('isLive'))
    add('is_live_false', match is not None and is_live is False)
    add('account_auth', listed.get('account_auth', True) is True)
    meta = session.symbol('EURUSD', account)
    add('eurusd', bool(meta and meta.get('symbol_id')))
    add('min_volume', bool(meta and meta.get('min_volume')), str((meta or {}).get('min_volume')))
    add('step_volume', bool(meta and meta.get('step_volume')), str((meta or {}).get('step_volume')))
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
                persist, settings, now=now, is_live=False, host=DEMO_HOST,
                status='passed' if persist_pass else 'failed', ttl_seconds=ttl_seconds,
                extra_detail={
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
        'host': DEMO_HOST,
        'port': PROTOBUF_PORT,
        'account': mask_account(account),
        'is_live': False,
        'symbol': 'EURUSD',
        'trading_permission': permission,
        'fingerprint': fingerprint(account_id=account, host=DEMO_HOST, environment='demo'),
        'checks': checks,
    }
