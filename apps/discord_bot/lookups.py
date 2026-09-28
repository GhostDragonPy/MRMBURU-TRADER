"""HTTPS lookups to a fixed allowlist of hosts. No redirects, no user URLs."""
import http.client
import ssl

ALLOWED_HOSTS = frozenset({'wttr.in', 'api.duckduckgo.com'})


def https_get(host: str, path: str, *, timeout=8, max_bytes=1024) -> bytes:
    if host not in ALLOWED_HOSTS:
        raise RuntimeError('Host not allowed')
    if not path.startswith('/') or '://' in path or '\r' in path or '\n' in path:
        raise RuntimeError('Path not allowed')
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=context)
    try:
        connection.request('GET', path, headers={'User-Agent': 'mrmburu-discord-bot/paper'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('Lookup failed')
        body = response.read(max_bytes + 1)
    finally:
        connection.close()
    if len(body) > max_bytes:
        raise RuntimeError('Response too large')
    return body
