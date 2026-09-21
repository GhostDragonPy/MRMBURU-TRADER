import json
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(RuntimeError):
    pass


def request_json(url, *, method='GET', headers=None, payload=None, timeout=20):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if payload is not None:
        req.add_header('Content-Type', 'application/json')
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='ignore')[:300]
        raise ProviderError(f'{exc.code} {url.split("?")[0]}: {detail}') from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f'Unreachable {url}: {exc.reason}') from exc


def request_form(url, fields, *, timeout=20):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Accept', 'application/json')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='ignore')[:300]
        raise ProviderError(f'{exc.code} {url.split("?")[0]}: {detail}') from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f'Unreachable {url}: {exc.reason}') from exc
