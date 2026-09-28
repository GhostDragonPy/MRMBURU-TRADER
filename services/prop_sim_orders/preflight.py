"""Persisted prop-sim preflight on LIVE infrastructure. Never stores tokens."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from core.models import PropSimPreflight
from services.ctrader.prop_sim import PROP_SIM_BROKER, PROP_SIM_CTID, PROP_SIM_TRADER_LOGIN
from services.demo_orders.guards import DemoGuardError
from services.demo_orders.preflight import mask_account
from services.prop_sim_orders.guards import PROP_SIM_HOST, require_prop_sim_identity

PROTOBUF_PORT = 5035
PREFLIGHT_ID = 1


def fingerprint(*, account_id, trader_login, broker, host, environment, symbol='EURUSD',
                scope='trading'):
    raw = '|'.join([
        str(account_id), str(trader_login), str(broker), str(host), str(environment),
        str(symbol), str(scope),
    ]).encode()
    return sha256(raw).hexdigest()


def expected_fingerprint(settings):
    return fingerprint(
        account_id=PROP_SIM_CTID,
        trader_login=PROP_SIM_TRADER_LOGIN,
        broker=PROP_SIM_BROKER,
        host=PROP_SIM_HOST,
        environment='live',
        symbol='EURUSD',
        scope='trading',
    )


def record_preflight(session, settings, *, now=None, status='passed', ttl_seconds=86400,
                     extra_detail=None):
    now = now or datetime.now(timezone.utc)
    require_prop_sim_identity(
        account_id=PROP_SIM_CTID, trader_login=PROP_SIM_TRADER_LOGIN, broker=PROP_SIM_BROKER,
        is_live=True, host=PROP_SIM_HOST, environment='live', scope='trading',
    )
    detail = {
        'port': PROTOBUF_PORT, 'environment': 'live', 'broker': PROP_SIM_BROKER,
        'trader_login': mask_account(PROP_SIM_TRADER_LOGIN),
    }
    if extra_detail:
        detail.update(extra_detail)
    row = session.get(PropSimPreflight, PREFLIGHT_ID)
    payload = dict(
        account_id=PROP_SIM_CTID, trader_login=PROP_SIM_TRADER_LOGIN, broker=PROP_SIM_BROKER,
        is_live=True, host=PROP_SIM_HOST, symbol='EURUSD', status=status,
        fingerprint=expected_fingerprint(settings), checked_at=now,
        expires_at=now + timedelta(seconds=int(ttl_seconds)), detail=detail,
    )
    if row is None:
        session.add(PropSimPreflight(id=PREFLIGHT_ID, **payload))
    else:
        for key, value in payload.items():
            setattr(row, key, value)
    return payload


def current_preflight(session):
    return session.get(PropSimPreflight, PREFLIGHT_ID)


def require_preflight(session, settings, *, now=None):
    now = now or datetime.now(timezone.utc)
    row = current_preflight(session)
    if row is None:
        raise DemoGuardError('PREFLIGHT_MISSING')
    if row.status != 'passed':
        raise DemoGuardError('PREFLIGHT_FAILED')
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise DemoGuardError('PREFLIGHT_EXPIRED')
    if row.is_live is not True or row.host != PROP_SIM_HOST:
        raise DemoGuardError('LIVE host required for prop-sim')
    if str(row.account_id) != PROP_SIM_CTID or str(row.trader_login) != PROP_SIM_TRADER_LOGIN:
        raise DemoGuardError('PREFLIGHT_ACCOUNT_MISMATCH')
    if str(row.broker).upper() != PROP_SIM_BROKER:
        raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
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
        return {'status': 'missing', 'account': mask_account(PROP_SIM_CTID)}
    return {
        'status': row.status,
        'account': mask_account(row.account_id),
        'trader_login': mask_account(row.trader_login),
        'broker': row.broker,
        'is_live': True,
        'host': row.host,
        'symbol': row.symbol,
        'checked_at': row.checked_at.isoformat() if row.checked_at else None,
        'expires_at': row.expires_at.isoformat() if row.expires_at else None,
        'fingerprint': row.fingerprint,
        'trading_permission': (row.detail or {}).get('trading_permission'),
        'socket': (row.detail or {}).get('socket'),
    }
