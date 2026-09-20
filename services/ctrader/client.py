from urllib.parse import urlencode
from services.providers.http import ProviderError, request_json
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable

CTRADER_AUTH = 'https://openapi.ctrader.com/apps/auth'
CTRADER_TOKEN = 'https://openapi.ctrader.com/apps/token'
CTRADER_API = 'https://openapi.ctrader.com/connect'


def configured(settings):
    client_id = settings.ctrader_client_id
    secret = settings.ctrader_client_secret
    return bool(client_id and secret and client_id.get_secret_value() and secret.get_secret_value())


def access_token(settings):
    token = settings.ctrader_access_token
    return token.get_secret_value() if token and token.get_secret_value() else None


def status(settings):
    ready = configured(settings)
    redirect = settings.ctrader_redirect_uri
    payload = {
        'provider': 'ctrader',
        'configured': ready,
        'authorized': bool(access_token(settings)),
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


def exchange_code(settings, code: str):
    if not configured(settings):
        raise CTraderAuthRequired('cTrader client id/secret are not set')
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': settings.ctrader_redirect_uri,
        'client_id': settings.ctrader_client_id.get_secret_value(),
        'client_secret': settings.ctrader_client_secret.get_secret_value(),
    }
    try:
        return request_json(CTRADER_TOKEN, method='POST', payload=payload, timeout=20)
    except ProviderError as exc:
        raise CTraderUnavailable(str(exc)) from exc


def api_get(settings, path: str, params=None):
    token = access_token(settings)
    if not token:
        raise CTraderAuthRequired('Authorize cTrader first (no access token)')
    query = {'oauth_token': token, **(params or {})}
    url = f'{CTRADER_API}{path}?{urlencode(query)}'
    try:
        return request_json(url, timeout=20)
    except ProviderError as exc:
        raise CTraderUnavailable(str(exc)) from exc
