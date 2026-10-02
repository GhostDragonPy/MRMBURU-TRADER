"""Deduped Discord channel alerts for collector / preflight / AI health."""
import json
import logging
from datetime import datetime, timezone

from services.providers.http import ProviderError, request_json

ALERT_PREFIX = 'ops:alert:v1:'
ALERT_QUEUE = 'discord:ops_alerts'
HEALTH_SIGNATURE_KEY = 'ops:alert:v1:health:signature'
HEALTH_DIGEST_KIND = 'trader_health'
DEFAULT_COOLDOWN_SECONDS = 21600  # 6h: chronic infra issues should not ping hourly
DISCORD_USER_AGENT = 'MRMBURU-Trader-Ops (https://trader.acshop.shop, 1.0)'


def alerts_enabled(settings):
    token = getattr(settings, 'discord_bot_token', None)
    channel = getattr(settings, 'discord_channel_id', None)
    return bool(
        getattr(settings, 'discord_bot_enabled', False)
        and token and token.get_secret_value()
        and channel
    )


def _as_text(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def _mark_cooldown(cache, key, seconds):
    try:
        cache.set(key, datetime.now(timezone.utc).isoformat(), ex=int(seconds))
    except Exception:
        pass


def _enqueue(cache, kind, message, channel_id):
    """Fallback path: discord-bot gateway already bypasses Cloudflare."""
    try:
        cache.rpush(ALERT_QUEUE, json.dumps({
            'kind': kind,
            'message': message,
            'channel_id': str(int(channel_id)),
            'at': datetime.now(timezone.utc).isoformat(),
        }))
        cache.ltrim(ALERT_QUEUE, -50, -1)
        return True
    except Exception:
        return False


def due_alerts(cache, *, limit=10):
    rows = []
    for _ in range(int(limit)):
        raw = cache.lpop(ALERT_QUEUE)
        if not raw:
            break
        try:
            rows.append(json.loads(_as_text(raw)))
        except (TypeError, json.JSONDecodeError):
            continue
    return rows


def notify(settings, cache, *, kind, message, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS):
    """Send at most one Discord message per kind inside the cooldown window.

    Tries Discord REST first; on Cloudflare/network failure enqueues for the bot.
    Returns True when delivered or queued. Never raises to callers.
    """
    if not kind or not message or cache is None or not alerts_enabled(settings):
        return False
    key = ALERT_PREFIX + str(kind)
    try:
        if cache.get(key):
            return False
    except Exception:
        return False
    body = f"🐈 **GhostDragon ops** · `{kind}`\n{message}".strip()[:1800]
    try:
        request_json(
            f'https://discord.com/api/v10/channels/{int(settings.discord_channel_id)}/messages',
            method='POST',
            headers={
                'Authorization': f"Bot {settings.discord_bot_token.get_secret_value()}",
                'Content-Type': 'application/json',
                'User-Agent': DISCORD_USER_AGENT,
            },
            payload={'content': body},
            timeout=10,
        )
        _mark_cooldown(cache, key, cooldown_seconds)
        return True
    except ProviderError as exc:
        logging.error('Ops Discord REST failed: %s; queueing for bot', type(exc).__name__)
        queued = _enqueue(cache, kind, message, settings.discord_channel_id)
        _mark_cooldown(cache, key, int(cooldown_seconds) if queued else 3600)
        return queued
    except Exception:
        logging.error('Ops Discord alert failed', exc_info=False)
        queued = _enqueue(cache, kind, message, settings.discord_channel_id)
        _mark_cooldown(cache, key, 3600)
        return queued


def collect_health_issues(settings, cache):
    """Return stable (code, detail) tuples for current infra problems."""
    if cache is None:
        return []
    from services.ctrader.stream import prefix

    issues = []
    key = prefix(settings)
    try:
        status = _as_text(cache.get(key + ':status'))
        last_err = _as_text(cache.get('paper:last_error'))
        prop_ok = _as_text(cache.get('prop-sim:preflight:ok'))
        budget = cache.zcard(f'ctrader:outbound:v1:{settings.ctrader_account_id}') if settings.ctrader_account_id else 0
        if isinstance(budget, bytes):
            budget = int(budget)
    except Exception:
        return issues

    budget_full = isinstance(budget, int) and budget >= getattr(settings, 'ctrader_requests_per_24h', 1000)
    if 'budget' in status.lower() or budget_full:
        issues.append(('collector_budget', f'status={status or "missing"} budget={budget}'))
    elif status.startswith('error:') or status in ('stale_bars', 'waiting_live_bars'):
        issues.append(('collector_status', f'status={status}'))

    # Paper error is usually a symptom of collector/cache — skip duplicate noise.
    if last_err and last_err not in ('None', '') and not any(c.startswith('collector_') for c, _ in issues):
        issues.append(('paper_error', last_err))

    if getattr(settings, 'trading_mode', '') == 'prop-sim' and prop_ok != '1':
        issues.append(('prop_sim_preflight', 'prop-sim:preflight:ok missing'))

    return issues


def _health_codes(issues):
    """Codes only — ignore volatile detail text so flaps do not re-alert."""
    return tuple(sorted({code for code, _ in issues}))


def watch_worker_health(settings, cache):
    """At most one digest every 6h while unhealthy. No spam on status flaps."""
    if cache is None or not alerts_enabled(settings):
        return []
    issues = collect_health_issues(settings, cache)
    if not issues:
        try:
            cache.delete(HEALTH_SIGNATURE_KEY)
        except Exception:
            pass
        return []

    digest_key = ALERT_PREFIX + HEALTH_DIGEST_KIND
    try:
        if cache.get(digest_key):
            return []
    except Exception:
        pass

    signature = '|'.join(_health_codes(issues))
    lines = ['Estado del trader (paper/prop-sim):']
    for code, detail in issues:
        lines.append(f'- `{code}`: {detail}')
    message = '\n'.join(lines)
    if not notify(settings, cache, kind=HEALTH_DIGEST_KIND, message=message,
                  cooldown_seconds=DEFAULT_COOLDOWN_SECONDS):
        return []
    try:
        cache.set(HEALTH_SIGNATURE_KEY, signature)
    except Exception:
        pass
    return [HEALTH_DIGEST_KIND]
