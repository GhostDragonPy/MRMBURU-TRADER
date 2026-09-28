"""Prop-sim (FTMO simulated funds on LIVE infrastructure) — execution stays disabled."""
from __future__ import annotations

from services.ctrader.guards_accounts import FORBIDDEN_EXECUTION_ACCOUNTS, mask_account
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.demo_orders.guards import DemoGuardError

PROP_SIM_LIVE_HOST = 'live.ctraderapi.com'
SCOPE_TRADE = 'TRADE'


class PropSimError(RuntimeError):
    pass


def allowed_account_ids(settings):
    raw = getattr(settings, 'prop_sim_allowed_account_ids', '') or ''
    ids = [item.strip() for item in str(raw).split(',') if item.strip()]
    forbidden = [item for item in ids if item in FORBIDDEN_EXECUTION_ACCOUNTS]
    if forbidden:
        raise PropSimError('FORBIDDEN_EXECUTION_ACCOUNT')
    return ids


def expected_account(settings):
    target = str(getattr(settings, 'prop_sim_ctrader_account_id', '') or '').strip()
    allow = allowed_account_ids(settings)
    if target and target not in allow:
        raise PropSimError('PROP_SIM_ACCOUNT_NOT_ALLOWLISTED')
    if not target and len(allow) == 1:
        target = allow[0]
    if not target:
        raise PropSimError('PROP_SIM_CTRADER_ACCOUNT_ID is required')
    if target in FORBIDDEN_EXECUTION_ACCOUNTS:
        raise PropSimError('FORBIDDEN_EXECUTION_ACCOUNT')
    return target


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


def validate_listed_accounts(listed, *, expected, allowlist):
    rows = list(listed or [])
    ids = {str(row.get('ctidTraderAccountId')) for row in rows}
    if expected in FORBIDDEN_EXECUTION_ACCOUNTS:
        raise PropSimError('FORBIDDEN_EXECUTION_ACCOUNT')
    if ids == FORBIDDEN_EXECUTION_ACCOUNTS or ids == {'48803059'}:
        raise PropSimError('TOKEN_ONLY_HAS_FORBIDDEN_ACCOUNT')
    extra = ids - set(allowlist) - FORBIDDEN_EXECUTION_ACCOUNTS
    # Forbidden IDs may appear in the token list; they must never be selected.
    if str(expected) not in ids:
        raise PropSimError('EXPECTED_ACCOUNT_MISSING')
    match = next(row for row in rows if str(row.get('ctidTraderAccountId')) == str(expected))
    if match.get('isLive') is not True:
        raise PropSimError('EXPECTED_ACCOUNT_NOT_LIVE_INFRA')
    disallowed_selected = [item for item in ids if item not in set(allowlist) and item not in FORBIDDEN_EXECUTION_ACCOUNTS]
    if disallowed_selected:
        raise PropSimError('ACCOUNT_NOT_ALLOWLISTED')
    return match


def complete_oauth(settings, redis_client, payload, state, *, list_accounts):
    from services.ctrader import tokens as token_store
    if state.get('purpose') != 'prop-sim':
        raise PropSimError('OAuth purpose is not prop-sim')
    expected = expected_account(settings)
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
    validate_listed_accounts(
        accounts, expected=expected, allowlist=allowed_account_ids(settings))
    token_store.save_prop_sim_tokens(
        redis_client, payload, scope='trading', account_id=expected)
    return {
        'purpose': 'prop-sim',
        'account': mask_account(expected),
        'environment': 'LIVE infrastructure',
        'scope': SCOPE_TRADE,
        'execution': 'disabled',
        'host': PROP_SIM_LIVE_HOST,
        'acknowledged_live_environment': bool(getattr(settings, 'prop_sim_acknowledged_live_environment', False)),
        'prop_sim_execution_enabled': False,
    }


def inventory(listed, *, settings):
    allow = set(allowed_account_ids(settings))
    rows = []
    for row in listed.get('accounts') or []:
        account = str(row.get('ctidTraderAccountId'))
        rows.append({
            'account': mask_account(account),
            'isLive': bool(row.get('isLive')),
            'permissionScope': 'TRADE' if permission_is_trade(listed.get('permission_scope')) else 'VIEW',
            'broker': row.get('broker') or listed.get('broker') or 'unknown',
            'allowlisted': account in allow,
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
        ProtoOAGetAccountListByAccessTokenReq, ProtoOATraderReq,
    )
    from services.ctrader.openapi import ReadOnlyOpenApi
    from services.ctrader.budget import RequestBudget
    if redis_client is None:
        raise CTraderUnavailable('Redis required for prop-sim inventory')
    target = int(expected_account(settings))
    budget = RequestBudget(
        redis_client, target, settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute)
    session = ReadOnlyOpenApi(
        client_id=settings.ctrader_client_id.get_secret_value(),
        client_secret=settings.ctrader_client_secret.get_secret_value(),
        access_token=token,
        account_id=target,
        budget=budget,
        environment='live',
    )
    with session:
        listed = session.request(ProtoOAGetAccountListByAccessTokenReq(accessToken=token))
        trader = session.request(ProtoOATraderReq(ctidTraderAccountId=target)).trader
    accounts = [
        {
            'ctidTraderAccountId': int(row.ctidTraderAccountId),
            'isLive': bool(row.isLive),
            'broker': getattr(trader, 'brokerName', '') if int(row.ctidTraderAccountId) == target else '',
        }
        for row in listed.ctidTraderAccount
    ]
    return {
        'permission_scope': getattr(listed, 'permissionScope', None),
        'accounts': accounts,
        'broker': getattr(trader, 'brokerName', '') or 'unknown',
    }


def reject_execution_account(account_id):
    if str(account_id) in FORBIDDEN_EXECUTION_ACCOUNTS:
        raise DemoGuardError('LIVE account rejected')
    return True
