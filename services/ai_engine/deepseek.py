import json
from datetime import datetime, timezone
from decimal import Decimal
from core.contracts import Signal
from services.providers.http import ProviderError, request_json

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODELS_URL = 'https://api.deepseek.com/models'
MODEL = 'deepseek-chat'
FALLBACK_MODEL = 'deepseek-flash'
SYSTEM = (
    'You are a paper-trading research assistant. Return JSON only. '
    'Never claim execution. If no high-quality setup exists, set signal to null. '
    'JSON schema: {"rationale": string, "signal": null or {'
    '"symbol": string, "side": "buy"|"sell", "entry": string, "stop_loss": string, '
    '"take_profit": string, "timeframe": string, "reasons": [string]}}'
)


class DeepSeekUnavailable(RuntimeError):
    pass


def _headers(api_key):
    return {'Authorization': f'Bearer {api_key}'}


def configured(settings):
    key = settings.deepseek_api_key
    return bool(key and key.get_secret_value())


def ping(settings):
    if not configured(settings):
        raise DeepSeekUnavailable('DEEPSEEK_API_KEY is not set')
    data = request_json(DEEPSEEK_MODELS_URL, headers=_headers(settings.deepseek_api_key.get_secret_value()))
    ids = [item.get('id') for item in data.get('data', [])]
    return {'provider': 'deepseek', 'ok': True, 'models': ids[:8]}


REVIEW_SYSTEM = (
    'You are a risk-aware EURUSD structure reviewer for the Esses strategy. '
    'Return JSON only. Approve only coherent, timely setups. '
    'Never change stop, target, size, or side. Never claim broker execution. '
    'JSON schema: {"approved": boolean, "confidence": "low"|"medium"|"high", '
    '"rationale": string}'
)


def review_esses_setup(settings, *, signal, audit=None, quote=None):
    """Advisory pass/fail filter for an Esses signal. Never mutates brackets.

    Returns a dict with approved True/False, or approved None when skipped.
    """
    if signal is None:
        return {'provider': 'deepseek', 'approved': None, 'skipped': True, 'reason': 'no_signal'}
    if not configured(settings):
        return {'provider': 'deepseek', 'approved': None, 'skipped': True, 'reason': 'not_configured'}
    payload_user = {
        'task': 'Approve or reject this Esses EURUSD setup',
        'signal': {
            'symbol': signal.symbol,
            'side': signal.side,
            'entry': str(signal.entry),
            'stop_loss': str(signal.stop_loss),
            'take_profit': str(signal.take_profit),
            'timeframe': signal.timeframe,
            'strategy_version': signal.strategy_version,
            'reasons': list(signal.reasons),
            'context': signal.context,
        },
        'audit': audit or {},
        'quote': quote or {},
        'now': datetime.now(timezone.utc).isoformat(),
    }
    last_error = None
    used_model = MODEL
    data = None
    for used_model in (MODEL, FALLBACK_MODEL):
        body = {
            'model': used_model,
            'temperature': 0,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': REVIEW_SYSTEM},
                {'role': 'user', 'content': json.dumps(payload_user, default=str)},
            ],
        }
        try:
            data = request_json(
                DEEPSEEK_URL,
                method='POST',
                headers=_headers(settings.deepseek_api_key.get_secret_value()),
                payload=body,
                timeout=45,
            )
            break
        except ProviderError as exc:
            last_error = exc
    if data is None:
        raise DeepSeekUnavailable(str(last_error)) from last_error
    try:
        content = data['choices'][0]['message']['content']
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise DeepSeekUnavailable('DeepSeek returned an unreadable review') from exc
    approved = parsed.get('approved')
    if not isinstance(approved, bool):
        raise DeepSeekUnavailable('DeepSeek review missing boolean approved')
    confidence = str(parsed.get('confidence') or 'medium').lower()
    if confidence not in ('low', 'medium', 'high'):
        confidence = 'medium'
    return {
        'provider': 'deepseek',
        'model': used_model,
        'approved': approved,
        'confidence': confidence,
        'rationale': str(parsed.get('rationale') or '')[:800],
        'usage': data.get('usage'),
        'skipped': False,
    }


def propose(settings, *, symbol, timeframe, quantity, value_per_price_unit, quote=None, context=None):
    if not configured(settings):
        raise DeepSeekUnavailable('DEEPSEEK_API_KEY is not set')
    user = {
        'task': 'Propose at most one paper signal',
        'symbol': symbol,
        'timeframe': timeframe,
        'quantity': str(quantity),
        'quote': quote or {},
        'context': context or {},
        'now': datetime.now(timezone.utc).isoformat(),
    }
    last_error = None
    used_model = MODEL
    data = None
    for used_model in (MODEL, FALLBACK_MODEL):
        payload = {
            'model': used_model,
            'temperature': 0,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': json.dumps(user)},
            ],
        }
        try:
            data = request_json(
                DEEPSEEK_URL,
                method='POST',
                headers=_headers(settings.deepseek_api_key.get_secret_value()),
                payload=payload,
                timeout=45,
            )
            break
        except ProviderError as exc:
            last_error = exc
    if data is None:
        raise DeepSeekUnavailable(str(last_error)) from last_error
    try:
        content = data['choices'][0]['message']['content']
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise DeepSeekUnavailable('DeepSeek returned an unreadable proposal') from exc
    signal = None
    raw = parsed.get('signal')
    if raw:
        now = datetime.now(timezone.utc)
        signal = Signal(
            symbol=raw.get('symbol', symbol),
            side=raw['side'],
            entry=Decimal(str(raw['entry'])),
            stop_loss=Decimal(str(raw['stop_loss'])),
            take_profit=Decimal(str(raw['take_profit'])),
            quantity=quantity,
            value_per_price_unit=value_per_price_unit,
            timeframe=raw.get('timeframe', timeframe),
            strategy_version=f'deepseek:{MODEL}',
            created_at=now,
            reasons=tuple(raw.get('reasons') or ('deepseek-proposal',)),
            context={'provider': 'deepseek', 'model': used_model},
        )
    return {
        'provider': 'deepseek',
        'model': used_model,
        'executable': False,
        'rationale': parsed.get('rationale', ''),
        'signal': None if signal is None else signal.model_dump(mode='json'),
        'usage': data.get('usage'),
    }
