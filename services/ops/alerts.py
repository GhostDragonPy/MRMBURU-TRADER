"""Deduped Discord channel alerts for collector / preflight / AI health."""
import logging
from datetime import datetime, timezone

from services.providers.http import ProviderError, request_json

ALERT_PREFIX = 'ops:alert:v1:'
DEFAULT_COOLDOWN_SECONDS = 1800


def alerts_enabled(settings):
    token = getattr(settings, 'discord_bot_token', None)
    channel = getattr(settings, 'discord_channel_id', None)
    return bool(
        getattr(settings, 'discord_bot_enabled', False)
        and token and token.get_secret_value()
        and channel
    )


def notify(settings, cache, *, kind, message, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS):
    """Send at most one Discord message per kind inside the cooldown window.

    Returns True when a message was delivered. Never raises to callers.
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
            },
            payload={'content': body},
            timeout=10,
        )
        try:
            cache.set(key, datetime.now(timezone.utc).isoformat(), ex=int(cooldown_seconds))
        except Exception:
            pass
        return True
    except ProviderError as exc:
        logging.error('Ops Discord alert failed: %s', type(exc).__name__)
        return False
    except Exception:
        logging.error('Ops Discord alert failed', exc_info=False)
        return False


def watch_worker_health(settings, cache):
    """Inspect Redis health keys and alert on actionable failures."""
    if cache is None or not alerts_enabled(settings):
        return []
    from services.ctrader.stream import prefix

    sent = []
    key = prefix(settings)
    try:
        status = cache.get(key + ':status') or ''
        last_err = cache.get('paper:last_error')
        prop_ok = cache.get('prop-sim:preflight:ok')
        budget = cache.zcard(f'ctrader:outbound:v1:{settings.ctrader_account_id}') if settings.ctrader_account_id else 0
    except Exception:
        return sent

    status = status if isinstance(status, str) else str(status)
    if 'budget' in status.lower() or (isinstance(budget, int) and budget >= getattr(settings, 'ctrader_requests_per_24h', 1000)):
        if notify(settings, cache, kind='collector_budget',
                  message=f'Presupuesto cTrader agotado o bloqueado (`{status or budget}`). '
                          'Esses no tendrá velas hasta que libere cupo.'):
            sent.append('collector_budget')
    elif status.startswith('error:') or status in ('stale_bars', 'waiting_live_bars'):
        if notify(settings, cache, kind='collector_status',
                  message=f'Collector enfermo: `{status}`. Sin M1 fresco no hay entradas Esses.'):
            sent.append('collector_status')

    if last_err and last_err not in ('None', ''):
        if notify(settings, cache, kind='paper_error',
                  message=f'Último error del paper cycle: `{last_err}`.'):
            sent.append('paper_error')

    if getattr(settings, 'trading_mode', '') == 'prop-sim' and prop_ok != '1':
        if notify(settings, cache, kind='prop_sim_preflight',
                  message='Preflight prop-sim no OK en Redis. Renovar/verificar socket VERIFIED.',
                  cooldown_seconds=900):
            sent.append('prop_sim_preflight')
    return sent
