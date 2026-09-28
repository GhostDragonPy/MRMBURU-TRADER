"""Prop-sim (FTMO simulated funds on LIVE infrastructure) — execution stays disabled."""
from __future__ import annotations

from services.ctrader.guards_accounts import FORBIDDEN_EXECUTION_ACCOUNTS, mask_account
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
try:
    from services.demo_orders.guards import DemoGuardError
except ImportError:
    class DemoGuardError(RuntimeError):
        pass

PROP_SIM_LIVE_HOST = 'live.ctraderapi.com'
SCOPE_TRADE = 'TRADE'
PROP_SIM_CTID = '48803059'
PROP_SIM_TRADER_LOGIN = '17204978'
PROP_SIM_BROKER = 'FTMO'


class PropSimError(RuntimeError):
    pass


def allowed_account_ids(settings):
    target = expected_account(settings)
    raw = getattr(settings, 'prop_sim_allowed_account_ids', '') or ''
    ids = [item.strip() for item in str(raw).split(',') if item.strip()]
    extra = [item for item in ids if item != target]
    if extra:
        raise PropSimError('ACCOUNT_NOT_ALLOWLISTED')
    return [target]


def expected_account(settings):
    target = str(getattr(settings, 'prop_sim_ctrader_account_id', '') or '').strip()
    login = expected_login(settings)
    if not target:
        raise PropSimError('PROP_SIM_CTRADER_ACCOUNT_ID is required')
    if target != PROP_SIM_CTID or login != PROP_SIM_TRADER_LOGIN:
        raise PropSimError('PROP_SIM_TUPLE_MISMATCH')
    return target


def expected_login(settings):
    login = str(getattr(settings, 'prop_sim_trader_login', '') or '').strip()
    if not login:
        raise PropSimError('PROP_SIM_TRADER_LOGIN is required')
    return login


def live_host_for_prop_sim(settings, *, purpose):
    if purpose != 'prop-sim':
        raise PropSimError('LIVE host is not available outside prop-sim')
    expected_account(settings)
    return PROP_SIM_LIVE_HOST


def permission_is_trade(scope_value):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAClientPermissionScope
    if scope_value == ProtoOAClientPermissionScope.SCOPE_TRADE:
        return True
    return str(scope_value).upper() in {'TRADE', 'SCOPE_TRADE', '1'}


def _broker_name(value):
    return str(value or '').strip().upper()


def row_matches_prop_sim_tuple(row, *, permission_scope):
    return (
        str(row.get('ctidTraderAccountId')) == PROP_SIM_CTID
        and str(row.get('traderLogin') or '') == PROP_SIM_TRADER_LOGIN
        and _broker_name(row.get('broker')) == PROP_SIM_BROKER
        and row.get('isLive') is True
        and permission_is_trade(permission_scope)
    )


def validate_listed_accounts(listed, *, expected, allowlist, permission_scope=None, expected_login=None):
    rows = list(listed or [])
    if not rows:
        raise PropSimError('UNBOUND_ACCOUNT')
    other_live = [
        row for row in rows
        if row.get('isLive') is True and str(row.get('ctidTraderAccountId')) != PROP_SIM_CTID
    ]
    if other_live:
        raise PropSimError('OTHER_LIVE_ACCOUNT_BLOCKED')
    match = next((row for row in rows if row_matches_prop_sim_tuple(
        row, permission_scope=permission_scope)), None)
    if match is None:
        raise PropSimError('PROP_SIM_TUPLE_MISMATCH')
    if str(expected) != PROP_SIM_CTID or str(expected_login or PROP_SIM_TRADER_LOGIN) != PROP_SIM_TRADER_LOGIN:
        raise PropSimError('PROP_SIM_TUPLE_MISMATCH')
    if str(match.get('ctidTraderAccountId')) not in set(allowlist):
        raise PropSimError('ACCOUNT_NOT_ALLOWLISTED')
    return match


def complete_oauth(settings, redis_client, payload, state, *, list_accounts):
    from services.ctrader import tokens as token_store
    if state.get('purpose') != 'prop-sim':
        raise PropSimError('OAuth purpose is not prop-sim')
    expected = expected_account(settings)
    login = expected_login(settings)
    if str(state.get('expected_account') or '') != str(expected):
        raise PropSimError('OAUTH_STATE_ACCOUNT_MISMATCH')
    token = payload.get('accessToken') or payload.get('access_token')
    if not token:
        raise CTraderAuthRequired('Token exchange returned no access token')
    listed = list_accounts(token, host=live_host_for_prop_sim(settings, purpose='prop-sim'))
    accounts = listed.get('accounts') or []
    if not accounts:
        raise PropSimError('UNBOUND_ACCOUNT')
    scope_hint = str(listed.get('permission_scope') or listed.get('scope') or '').lower()
    if scope_hint in {'accounts', 'view', 'scope_view'}:
        raise PropSimError('PERMISSION_SCOPE_NOT_TRADE')
    if not permission_is_trade(listed.get('permission_scope')):
        raise PropSimError('PERMISSION_SCOPE_NOT_TRADE')
    match = validate_listed_accounts(
        accounts, expected=expected, allowlist=allowed_account_ids(settings),
        permission_scope=listed.get('permission_scope'), expected_login=login)
    token_store.save_prop_sim_tokens(
        redis_client, payload, scope='trading', account_id=expected, trader_login=login)
    return {
        'purpose': 'prop-sim',
        'account': mask_account(expected),
        'trader_login': mask_account(login),
        'environment': 'LIVE infrastructure',
        'scope': SCOPE_TRADE,
        'execution': 'disabled',
        'host': PROP_SIM_LIVE_HOST,
        'acknowledged_live_environment': bool(getattr(settings, 'prop_sim_acknowledged_live_environment', False)),
        'prop_sim_execution_enabled': False,
    }


def inventory(listed, *, settings):
    rows = []
    for row in listed.get('accounts') or []:
        account = str(row.get('ctidTraderAccountId'))
        rows.append({
            'account': mask_account(account),
            'trader_login': mask_account(row.get('traderLogin')),
            'isLive': bool(row.get('isLive')),
            'permissionScope': 'TRADE' if permission_is_trade(listed.get('permission_scope')) else 'VIEW',
            'broker': row.get('broker') or listed.get('broker') or 'unknown',
            'allowlisted': account == expected_account(settings),
            'forbidden_execution': account in FORBIDDEN_EXECUTION_ACCOUNTS,
        })
    return {
        'purpose': 'prop-sim',
        'execution': 'disabled',
        'host': PROP_SIM_LIVE_HOST,
        'accounts': rows,
    }


def broker_account_list(settings, token, host, redis_client=None):
    """Read-only GetAccountList on the prop-sim LIVE host. Zero trading payloads."""
    if host != PROP_SIM_LIVE_HOST:
        raise PropSimError('LIVE host required for prop-sim inventory')
    if not token:
        raise PropSimError('PROP_SIM_TOKEN_MISSING')
    if not getattr(settings, 'ctrader_network_enabled', False):
        raise CTraderUnavailable('cTrader network disabled')
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq, ProtoOAGetAccountListByAccessTokenReq, ProtoOATraderReq,
    )
    from services.ctrader.openapi import ReadOnlyOpenApi
    from services.ctrader.budget import RequestBudget
    if redis_client is None:
        raise CTraderUnavailable('Redis required for prop-sim inventory')
    target = int(expected_account(settings))
    budget = RequestBudget(
        redis_client, f'prop-sim:{target}', settings.ctrader_requests_per_24h,
        settings.ctrader_requests_per_minute)
    session = ReadOnlyOpenApi(
        client_id=settings.ctrader_client_id.get_secret_value(),
        client_secret=settings.ctrader_client_secret.get_secret_value(),
        access_token=token,
        account_id=target,
        budget=budget,
        environment='live',
        authenticate_account=False,
        timeout=20,
    )
    with session as conn:
        listed = conn.request(ProtoOAGetAccountListByAccessTokenReq(accessToken=token))
        broker = 'unknown'
        trader_login = None
        ids = [int(row.ctidTraderAccountId) for row in listed.ctidTraderAccount]
        if target in ids:
            conn.request(ProtoOAAccountAuthReq(ctidTraderAccountId=target, accessToken=token))
            trader = conn.request(ProtoOATraderReq(ctidTraderAccountId=target)).trader
            broker = trader.brokerName or 'unknown'
            trader_login = int(trader.traderLogin) if trader.HasField('traderLogin') else None
    accounts = []
    for row in listed.ctidTraderAccount:
        login = int(row.traderLogin) if row.HasField('traderLogin') else None
        cid = int(row.ctidTraderAccountId)
        if cid == target and trader_login is not None:
            login = trader_login
        accounts.append({
            'ctidTraderAccountId': cid,
            'traderLogin': login,
            'isLive': bool(row.isLive),
            'broker': broker if cid == target else '',
        })
    return {
        'permission_scope': getattr(listed, 'permissionScope', None),
        'accounts': accounts,
        'broker': broker,
    }


def reject_execution_account(account_id):
    if str(account_id) in FORBIDDEN_EXECUTION_ACCOUNTS:
        raise DemoGuardError('LIVE account rejected')
    return True
