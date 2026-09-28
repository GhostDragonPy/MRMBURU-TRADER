"""Personal-assistant features ported from the custom bot. Paper-safe, no secrets."""
import json
from datetime import datetime, timezone, timedelta
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from apps.discord_bot.identity import CAT

REMINDER_KEY = 'discord:reminders'
TOKEN_PREFIX = 'discord:tokens:'
USAGE_PREFIX = 'discord:usage:'


def weather(city: str) -> str:
    place = (city or '').strip()
    if not place or len(place) > 80:
        raise ValueError('Indica una ciudad válida')
    url = f'https://wttr.in/{quote(place)}?format=3'
    request = Request(url, headers={'user-agent': 'mrmburu-discord-bot/paper'})
    try:
        with urlopen(request, timeout=8) as response:
            body = response.read().decode('utf-8', errors='replace').strip()
    except URLError as exc:
        raise RuntimeError('Clima no disponible') from exc
    if not body or 'Unknown location' in body:
        raise ValueError('No encontré esa ubicación')
    return f'{CAT} {body}'


def search(query: str) -> str:
    text = (query or '').strip()
    if not text or len(text) > 200:
        raise ValueError('Escribe una búsqueda corta')
    url = 'https://api.duckduckgo.com/?' + urlencode({
        'q': text, 'format': 'json', 'no_html': 1, 'skip_disambig': 1,
    })
    request = Request(url, headers={'user-agent': 'mrmburu-discord-bot/paper'})
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except (URLError, json.JSONDecodeError) as exc:
        raise RuntimeError('Búsqueda no disponible') from exc
    abstract = (payload.get('AbstractText') or '').strip()
    heading = (payload.get('Heading') or text).strip()
    related = payload.get('RelatedTopics') or []
    hint = ''
    if related and isinstance(related[0], dict):
        hint = (related[0].get('Text') or '').strip()
    summary = abstract or hint or 'Sin resumen instantáneo; prueba un término más concreto.'
    return f'{CAT} **{heading}**\n{summary[:900]}'


def record_usage(redis_client, user_id, command: str):
    redis_client.incr(TOKEN_PREFIX + str(user_id))
    redis_client.incr(USAGE_PREFIX + str(user_id) + ':' + command)


def usage_report(redis_client, user_id) -> dict:
    total = int(redis_client.get(TOKEN_PREFIX + str(user_id)) or 0)
    return {
        'user_id': str(user_id),
        'command_count': total,
        'note': 'Contador local de comandos personales; no es facturación de un modelo.',
        'execution_enabled': False,
    }


def schedule_reminder(redis_client, *, user_id, channel_id, minutes: int, text: str):
    if minutes < 1 or minutes > 24 * 60:
        raise ValueError('Los minutos deben estar entre 1 y 1440')
    note = (text or '').strip()
    if len(note) < 3 or len(note) > 280:
        raise ValueError('El recordatorio debe tener entre 3 y 280 caracteres')
    due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    item = json.dumps({
        'user_id': str(user_id),
        'channel_id': str(channel_id),
        'text': note,
        'due': due.isoformat(),
    })
    redis_client.zadd(REMINDER_KEY, {item: due.timestamp()})
    return {'due': due.isoformat(), 'text': note, 'paper_only': True}


def due_reminders(redis_client, now=None):
    stamp = (now or datetime.now(timezone.utc)).timestamp()
    items = redis_client.zrangebyscore(REMINDER_KEY, 0, stamp) or []
    due = []
    for raw in items:
        redis_client.zrem(REMINDER_KEY, raw)
        payload = json.loads(raw if isinstance(raw, str) else raw.decode())
        due.append(payload)
    return due
