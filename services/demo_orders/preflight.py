"""Persisted DEMO preflight. Never stores tokens or secrets."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from services.demo_orders.guards import DEMO_HOST, LIVE_ACCOUNT_IDS, DemoGuardError
from core.models import DemoPreflight

PROTOBUF_PORT = 5035
PREFLIGHT_ID = 1


def fingerprint(*, account_id, host, environment, symbol='EURUSD', scope='trading'):
    raw = '|'.join([str(account_id), str(host), str(environment), str(symbol), str(scope)]).encode()
    return sha256(raw).hexdigest()


def mask_account(account_id):
    text = str(account_id or '')
    if len(text) <= 4:
        return '****'
    return ('*' * (len(text) - 4)) + text[-4:]


def expected_fingerprint(settings):
    return fingerprint(
        account_id=settings.demo_ctrader_account_id,
        host=DEMO_HOST,
        environment='demo',
        symbol='EURUSD',
        scope='trading',
    )


def record_preflight(session, settings, *, now=None, is_live=False, host=DEMO_HOST,
                     symbol='EURUSD', status='passed', ttl_seconds=86400, extra_detail=None):
    now = now or datetime.now(timezone.utc)
    account = (settings.demo_ctrader_account_id or '').strip()
    if not account:
        raise DemoGuardError('DEMO_CTRADER_ACCOUNT_ID is required')
    if account in LIVE_ACCOUNT_IDS or is_live is True:
        raise DemoGuardError('LIVE account rejected')
    if host != DEMO_HOST:
        raise DemoGuardError('LIVE endpoint rejected')
    detail = {'port': PROTOBUF_PORT, 'environment': 'demo'}
    if extra_detail:
        detail.update(extra_detail)
    row = session.get(DemoPreflight, PREFLIGHT_ID)
    payload = dict(
        account_id=account, is_live=False, host=DEMO_HOST, symbol=symbol,
        status=status, fingerprint=expected_fingerprint(settings),
        checked_at=now, expires_at=now + timedelta(seconds=int(ttl_seconds)),
        detail=detail,
    )
    if row is None:
        session.add(DemoPreflight(id=PREFLIGHT_ID, **payload))
    else:
        for key, value in payload.items():
            setattr(row, key, value)
    return payload


def current_preflight(session):
    return session.get(DemoPreflight, PREFLIGHT_ID)


def require_preflight(session, settings, *, now=None):
    now = now or datetime.now(timezone.utc)
    row = current_preflight(session)
    if row is None:
        raise DemoGuardError('PREFLIGHT_MISSING')
    if row.status != 'passed':
        raise DemoGuardError('PREFLIGHT_FAILED')
    if row.expires_at.tzinfo is None:
        expires = row.expires_at.replace(tzinfo=timezone.utc)
    else:
        expires = row.expires_at
    if expires <= now:
        raise DemoGuardError('PREFLIGHT_EXPIRED')
    if row.is_live is True or row.host != DEMO_HOST:
        raise DemoGuardError('LIVE endpoint rejected')
    if str(row.account_id) != str(settings.demo_ctrader_account_id):
        raise DemoGuardError('PREFLIGHT_ACCOUNT_MISMATCH')
    if row.fingerprint != expected_fingerprint(settings):
        raise DemoGuardError('PREFLIGHT_FINGERPRINT_MISMATCH')
    return row


def require_verified_preflight(session, settings, *, now=None):
    row = require_preflight(session, settings, now=now)
    detail = row.detail or {}
    if detail.get('trading_permission') != 'VERIFIED':
        raise DemoGuardError('TRADING_PERMISSION_UNVERIFIED')
    if detail.get('socket') != 'sdk-tls':
        raise DemoGuardError('PREFLIGHT_SOCKET_UNVERIFIED')
    return row


def public_preflight(session, settings):
    row = current_preflight(session)
    if row is None:
        return {'status': 'missing', 'account': mask_account(settings.demo_ctrader_account_id)}
    return {
        'status': row.status,
        'account': mask_account(row.account_id),
        'is_live': False,
        'host': row.host,
        'symbol': row.symbol,
        'checked_at': row.checked_at.isoformat() if row.checked_at else None,
        'expires_at': row.expires_at.isoformat() if row.expires_at else None,
        'fingerprint': row.fingerprint,
        'trading_permission': (row.detail or {}).get('trading_permission'),
        'socket': (row.detail or {}).get('socket'),
    }
