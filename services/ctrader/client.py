from urllib.parse import urlencode

CTRADER_AUTH = 'https://openapi.ctrader.com/apps/auth'
CTRADER_TOKEN = 'https://openapi.ctrader.com/apps/token'


def configured(settings):
    client_id = settings.ctrader_client_id
    secret = settings.ctrader_client_secret
    return bool(client_id and secret and client_id.get_secret_value() and secret.get_secret_value())


def status(settings):
    ready = configured(settings)
    redirect = settings.ctrader_redirect_uri
    payload = {
        'provider': 'ctrader',
        'configured': ready,
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
