"""Personal-assistant features. Paper-safe: no shell, no secrets, no broker access."""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo
from apps.discord_bot.identity import CAT
from apps.discord_bot.lookups import https_get
from apps.discord_bot.security import MAX_CITY, MAX_QUERY, MAX_REMINDER, bounded

REMINDER_KEY = 'discord:reminders'
USAGE_TOTAL = 'discord:usage:total:'
USAGE_COMMAND = 'discord:usage:command:'
ZONE = ZoneInfo('America/Asuncion')
CITY_PATTERN = re.compile(r"^[A-Za-zÀ-ÿ0-9 .,'-]{1,64}$")
URLISH = re.compile(r'(?i)(https?://|file:|ftp:|\\\\|localhost|\d{1,3}(?:\.\d{1,3}){3}|::1|/etc/|\.\./)')
COMMAND_NAMES = frozenset({
    'ayuda', 'identidad', 'clima', 'tokens', 'recordatorio', 'buscar',
    'status', 'positions', 'history', 'daily_report', 'pause', 'resume',
    'paper_order', 'paper_close', 'broker_order',
})


def weather(city: str) -> str:
    place = bounded(city, minimum=2, maximum=MAX_CITY, field='ciudad')
    if not CITY_PATTERN.fullmatch(place) or URLISH.search(place):
        raise ValueError('Indica una ciudad válida')
    path = f'/{quote(place, safe="")}?format=3'
    try:
        body = https_get('wttr.in', path, timeout=8, max_bytes=256).decode('utf-8', 'replace').strip()
    except Exception as exc:
        raise RuntimeError('Clima no disponible') from exc
    if not body or 'Unknown location' in body:
        raise ValueError('No encontré esa ubicación')
    return f'{CAT} {body}'


def search(query: str) -> str:
    text = bounded(query, minimum=2, maximum=MAX_QUERY, field='consulta')
    if URLISH.search(text) or any(ch in text for ch in '<>`"\'\\\n\r\t'):
        raise ValueError('La consulta no puede ser una URL ni contener caracteres especiales')
    path = '/?' + urlencode({'q': text, 'format': 'json', 'no_html': 1, 'skip_disambig': 1})
    try:
        raw = https_get('api.duckduckgo.com', path, timeout=8, max_bytes=4096)
        payload = json.loads(raw.decode('utf-8', 'replace'))
    except Exception as exc:
        raise RuntimeError('Búsqueda no disponible') from exc
    if not isinstance(payload, dict):
        raise RuntimeError('Búsqueda no disponible')
    abstract = str(payload.get('AbstractText') or '').strip()
    heading = str(payload.get('Heading') or text).strip()[:80]
    related = payload.get('RelatedTopics') or []
    hint = ''
    if related and isinstance(related[0], dict):
        hint = str(related[0].get('Text') or '').strip()
    summary = (abstract or hint or 'Sin resumen instantáneo; prueba un término más concreto.')[:400]
    if URLISH.search(heading):
        heading = text
    return f'{CAT} **{heading}**\n{summary}'


def record_usage(redis_client, user_id, command: str):
    if command not in COMMAND_NAMES:
        return
    redis_client.incr(USAGE_TOTAL + str(int(user_id)))
    redis_client.incr(USAGE_COMMAND + str(int(user_id)) + ':' + command)


def usage_report(redis_client, user_id) -> dict:
    total = int(redis_client.get(USAGE_TOTAL + str(int(user_id))) or 0)
    by_command = {}
    labels = {'tokens': 'usage'}
    for name in sorted(COMMAND_NAMES):
        count = int(redis_client.get(USAGE_COMMAND + str(int(user_id)) + ':' + name) or 0)
        if count:
            by_command[labels.get(name, name)] = count
    return {
        'usage_count': total,
        'by_command': by_command,
        'window': 'process-local counters',
        'execution_enabled': False,
    }


def schedule_reminder(redis_client, *, user_id, channel_id, minutes: int, text: str):
    if not isinstance(minutes, int) or minutes < 1 or minutes > 24 * 60:
        raise ValueError('Los minutos deben estar entre 1 y 1440')
    note = bounded(text, minimum=3, maximum=MAX_REMINDER, field='recordatorio')
    now = datetime.now(ZONE)
    due = now + timedelta(minutes=minutes)
    if due <= now:
        raise ValueError('La fecha del recordatorio debe ser futura')
    item = json.dumps({
        'user_id': str(int(user_id)),
        'channel_id': str(int(channel_id)),
        'text': note,
        'due': due.isoformat(),
        'timezone': 'America/Asuncion',
    }, separators=(',', ':'))
    redis_client.zadd(REMINDER_KEY, {item: due.timestamp()})
    return {
        'due': due.isoformat(),
        'timezone': 'America/Asuncion',
        'text': note,
        'paper_only': True,
        'execution_enabled': False,
    }


def due_reminders(redis_client, now=None):
    stamp = (now or datetime.now(ZONE)).timestamp()
    items = redis_client.zrangebyscore(REMINDER_KEY, 0, stamp) or []
    due = []
    for raw in items:
        redis_client.zrem(REMINDER_KEY, raw)
        payload = json.loads(raw if isinstance(raw, str) else raw.decode())
        due.append(payload)
    return due
