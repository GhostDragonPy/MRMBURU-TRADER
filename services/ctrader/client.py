from urllib.parse import urlencode
from services.providers.http import ProviderError, request_form, request_json
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.ctrader import tokens as token_store

CTRADER_AUTH = 'https://openapi.ctrader.com/apps/auth'
CTRADER_TOKEN = 'https://openapi.ctrader.com/apps/token'
CTRADER_API = 'https://openapi.ctrader.com/connect'


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
    state = token_store.begin_login(redis_client)
    query = urlencode({
        'client_id': settings.ctrader_client_id.get_secret_value(),
        'redirect_uri': settings.ctrader_redirect_uri,
        'scope': 'trading',
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


def api_get(settings, path: str, params=None, redis_client=None):
    token = access_token(settings, redis_client)
    if not token:
        raise CTraderAuthRequired('Authorize cTrader first (no access token)')
    query = {'oauth_token': token, **(params or {})}
    url = f'{CTRADER_API}{path}?{urlencode(query)}'
    try:
        return request_json(url, timeout=20)
    except ProviderError as exc:
        raise CTraderUnavailable(str(exc)) from exc

