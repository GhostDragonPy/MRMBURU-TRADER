from urllib.parse import urlencode
from services.providers.http import ProviderError, request_form
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.ctrader import tokens as token_store

CTRADER_AUTH = 'https://openapi.ctrader.com/apps/auth'
CTRADER_TOKEN = 'https://openapi.ctrader.com/apps/token'


def configured(settings):
    client_id = settings.ctrader_client_id
    secret = settings.ctrader_client_secret
    return bool(client_id and secret and client_id.get_secret_value() and secret.get_secret_value())


def access_token(settings, redis_client=None):
    token = settings.ctrader_access_token
    if token and token.get_secret_value():
        return token.get_secret_value()
    if redis_client is not None:
        return token_store.load_access_token(redis_client)
    return None


def status(settings, redis_client=None):
    ready = configured(settings)
    redirect = settings.ctrader_redirect_uri
    payload = {
        'provider': 'ctrader',
        'configured': ready,
        'authorized': bool(access_token(settings, redis_client)),
        'account_id': settings.ctrader_account_id,
        'scope': token_store.effective_scope(redis_client) if redis_client is not None else None,
        'market_data': 'principal',
        'execution_enabled': False,
        'orders': 'disabled',
        'redirect_uri': redirect,
    }
    if ready and redirect:
        query = urlencode({
            'client_id': settings.ctrader_client_id.get_secret_value(),
            'redirect_uri': redirect,
            'scope': 'trading',
            'product': 'web',
        })
        payload['authorization_url'] = f'{CTRADER_AUTH}?{query}'
        payload['token_url'] = CTRADER_TOKEN
    return payload


def authorization_url(settings, redis_client):
    if not configured(settings):
        raise CTraderAuthRequired('cTrader client id/secret are not set')
    state = token_store.begin_login(redis_client, scope='trading')
    query = urlencode({
        'client_id': settings.ctrader_client_id.get_secret_value(),
        'redirect_uri': settings.ctrader_redirect_uri,
        'scope': 'trading',
        'product': 'web',
        'state': state,
    })
    return f'{CTRADER_AUTH}?{query}'


def authorization_url_accounts(settings, redis_client):
    if not configured(settings):
        raise CTraderAuthRequired('cTrader client id/secret are not set')
    state = token_store.begin_login(redis_client, scope='accounts')
    query = urlencode({
        'client_id': settings.ctrader_client_id.get_secret_value(),
        'redirect_uri': settings.ctrader_redirect_uri,
        'scope': 'accounts',
        'product': 'web',
        'state': state,
    })
    return f'{CTRADER_AUTH}?{query}'


def exchange_code(settings, code: str):
    if not configured(settings):
        raise CTraderAuthRequired('cTrader client id/secret are not set')
    fields = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': settings.ctrader_redirect_uri,
        'client_id': settings.ctrader_client_id.get_secret_value(),
        'client_secret': settings.ctrader_client_secret.get_secret_value(),
    }
    try:
        return request_form(CTRADER_TOKEN, fields, timeout=20)
    except ProviderError as exc:
        raise CTraderUnavailable(str(exc)) from exc


def _session(settings, redis_client):
    from services.ctrader.budget import RequestBudget

    if not settings.ctrader_network_enabled:
        raise CTraderUnavailable('cTrader network disabled by configuration')
    if redis_client is None:
        raise CTraderUnavailable('Shared cTrader request budget requires Redis')

    token = access_token(settings, redis_client)
    if not token:
        raise CTraderAuthRequired('Authorize cTrader first (no access token)')
    if not configured(settings):
        raise CTraderAuthRequired('cTrader client id/secret are not set')
    if not settings.ctrader_account_id:
        raise CTraderAuthRequired('cTrader account id is not set')
    try:
        account_id = int(settings.ctrader_account_id)
    except (TypeError, ValueError) as exc:
        raise CTraderAuthRequired('cTrader account id must be numeric') from exc
    return dict(
        client_id=settings.ctrader_client_id.get_secret_value(),
        client_secret=settings.ctrader_client_secret.get_secret_value(),
        access_token=token,
        account_id=account_id,
        environment=settings.ctrader_environment,
        budget=RequestBudget(redis_client, account_id,
            settings.ctrader_requests_per_24h, settings.ctrader_requests_per_minute),
    )


def open_demo(settings, redis_client=None):
    """Legacy function name: connects to configured read-only source, never submits orders."""
    from services.ctrader.openapi import ReadOnlyOpenApi
    return ReadOnlyOpenApi(**_session(settings, redis_client))


def open_trading(settings, redis_client=None):
    from services.ctrader.openapi import TradingOpenApi
    if not getattr(settings, 'ctrader_broker_orders', False):
        raise CTraderUnavailable('Broker orders disabled')
    return TradingOpenApi(timeout=30, **_session(settings, redis_client))
