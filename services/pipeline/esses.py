"""Use the common accounting engine with multi-timeframe Esses signals."""
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from core.models import Account, AutomaticPaperControl, KillSwitch, PaperLedger, PaperEvent
from core.contracts import RiskPolicy, PropRules
from services.pipeline.simulator import initial, advance
from services.strategy_engine.esses import evaluate
from services.ctrader.types import SymbolInfo


class SelectedSignal:
    def __init__(self, signal):
        self.signal = signal

    def generate(self, *args, **kwargs):
        return self.signal


def calendar(cache, now):
    day = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    raw = cache.get('esses:news:'+day)
    if not raw:
        return None
    data = json.loads(raw)
    if data.get('date') != day or not data.get('reviewed'):
        return None
    for t in data['events']:
        if datetime.fromisoformat(t).tzinfo is None:
            raise ValueError('News timestamps must include timezone')
    return data


def apply_ai_filter(settings, signal, audit, tick, cache=None):
    """DeepSeek pass/fail for Esses entries. Reject blocks; outages fail-open + alert."""
    if signal is None or settings is None:
        return signal, audit, []
    from services.ai_engine import deepseek
    if not deepseek.configured(settings):
        audit = dict(audit or {})
        audit['ai_filter'] = {'provider': 'deepseek', 'approved': None, 'skipped': True,
                              'reason': 'not_configured'}
        return signal, audit, []
    extra = []
    quote = tick.model_dump(mode='json') if tick is not None else {}
    try:
        review = deepseek.review_esses_setup(
            settings, signal=signal, audit=audit or {}, quote=quote)
    except deepseek.DeepSeekUnavailable as exc:
        logging.error('Esses AI filter unavailable: %s', type(exc).__name__)
        review = {'provider': 'deepseek', 'approved': None, 'skipped': True,
                  'reason': 'unavailable', 'error': type(exc).__name__}
        try:
            from services.ops.alerts import notify
            notify(settings, cache, kind='ai_filter_down',
                   message='DeepSeek no respondió; Esses sigue sin filtro IA (fail-open).')
        except Exception:
            pass
        try:
            from services.ops.failure_log import record_failure_standalone
            record_failure_standalone(
                kind='ai_filter', code='deepseek_unavailable',
                message='DeepSeek review unavailable; Esses fail-open',
                detail={'exc_type': type(exc).__name__, 'source': 'esses'},
            )
        except Exception:
            pass
    audit = dict(audit or {})
    audit['ai_filter'] = review
    if review.get('approved') is False:
        extra.append({
            'kind': 'blocked',
            'at': datetime.now(timezone.utc).isoformat(),
            'reasons': ['AI_REJECTED'],
            'ai_filter': review,
        })
        return None, audit, extra
    if review.get('approved') is True:
        ctx = dict(signal.context or {})
        ctx['ai_filter'] = {
            'provider': review.get('provider'),
            'model': review.get('model'),
            'approved': True,
            'confidence': review.get('confidence'),
            'rationale': review.get('rationale'),
        }
        signal = signal.model_copy(update={'context': ctx})
    return signal, audit, extra


def cycle(session, feed, account_id, *, cache, now=None, allow_unknown_news=False, settings=None):
    tick = feed.tick('EURUSD')
    now = now or datetime.now(timezone.utc)
    # Lock before deciding whether entry history is necessary, so management
    # of an already-open position never depends on calendar/history availability.
    gate = session.scalar(select(KillSwitch).where(KillSwitch.id == 1).with_for_update())
    account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if account is None or account.mode != 'paper' or account.currency != 'USD':
        raise ValueError('Dedicated USD paper account required')
    control = session.get(AutomaticPaperControl, 1, populate_existing=True)
    rules = PropRules.model_validate(account.prop_rules)
    row = session.get(PaperLedger, account_id, populate_existing=True)
    if row is None:
        row = PaperLedger(account_id=account_id, state=initial(account.initial_balance, now,
            now.astimezone(ZoneInfo(rules.timezone)).date().isoformat()))
        session.add(row)
    signal, audit, bars, news = None, {}, [], None
    ai_events = []
    meta = SymbolInfo(name='EURUSD', digits=5)
    if row.state.get('position'):
        # Optional BE history must never prevent quote-based exits.
        try:
            bars = feed.ohlc('EURUSD', 'M1', 200)
        except Exception:
            bars = []
    else:
        frames = {tf: feed.ohlc('EURUSD',tf,200) for tf in ('D1','H4','H1','M15','M5','M1')}
        signal, audit = evaluate(frames, now)
        bars = frames['M1']
        meta = feed.instrument('EURUSD')
        news = calendar(cache, now)
        signal, audit, ai_events = apply_ai_filter(settings, signal, audit, tick, cache=cache)
    policy = RiskPolicy.model_validate(account.risk_policy)
    policy = policy.model_copy(update={'max_trades_daily': min(policy.max_trades_daily,2)})
    state, events = advance(row.state, tick=tick, bars=bars, instrument=meta, now=now,
        policy=policy, rules=rules, enabled=account.enabled, killed=gate is None or gate.active or bool(control and control.paused),
        allow_unknown_news=allow_unknown_news, strategy=SelectedSignal(signal),
        timeframe='M1', esses=True, audit=audit, news=news)
    events = list(ai_events) + list(events)
    state['strategy'] = 'esses-research:1'
    state['news_known'] = bool(news) if not row.state.get('position') else row.state.get('news_known', False)
    state['last_analysis'] = audit if audit else state.get('last_analysis', {})
    row.state = state
    for payload in events:
        session.add(PaperEvent(account_id=account_id, payload=payload, created_at=now))
    session.flush()
    return dict(state=state, events=events, executable=False)
