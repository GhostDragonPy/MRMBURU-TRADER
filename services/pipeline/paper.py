from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from core.contracts import Signal
from core.models import Account, AccountSnapshot, AuditEvent
from services.ctrader.types import account_state_from_feed, market_state_from_tick
from services.execution_engine.paper import fill_paper
from services.journal import service as journal
from services.risk_engine.service import evaluate_scenario
from services.strategy_engine.engine import Candle, SmaConfig, SmaCross


def bars_to_candles(bars):
    return [Candle(close=bar.close, closed_at=bar.closed_at) for bar in bars]


def run(session, feed, *, account_id, request_key, symbol, timeframe='M15',
        quantity=Decimal('1'), value_per_price_unit=Decimal('1'),
        ai_opinion=None, news_known=True, high_impact_events=(),
        strategy_config=None):
    """cTrader data → Strategy Engine → Risk Engine → paper fill → journal.

    DeepSeek/ai_opinion is advisory only. Risk Engine can always reject.
    """
    tick = feed.tick(symbol)
    bars = feed.ohlc(symbol, timeframe)
    broker = feed.account()
    account = session.get(Account, account_id)
    if account is None:
        from services.risk_engine.service import NotFound
        raise NotFound('Account not found')
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(account.prop_rules.get('timezone', 'Europe/Prague'))
    state = account_state_from_feed(
        broker, initial_balance=account.initial_balance, risk_day=now.astimezone(tz).date(),
        enabled=account.enabled,
    )
    snapshot = AccountSnapshot(
        account_id=account_id, observed_at=state.as_of, risk_day=state.risk_day,
        state=state.model_dump(mode='json'), source='ctrader',
    )
    session.add(snapshot)
    session.flush()
    signal = SmaCross(strategy_config or SmaConfig()).generate(
        bars_to_candles(bars), now=now, symbol=symbol, timeframe=timeframe,
        quantity=quantity, value_per_price_unit=value_per_price_unit,
    )
    market = market_state_from_tick(
        tick, news_known=news_known, news_checked_at=now,
        high_impact_events=high_impact_events,
        connected=True, platform_ready=True,
    )
    if signal is None:
        session.add(AuditEvent(actor='strategy', action='pipeline.no_signal',
                               payload={'symbol': symbol, 'timeframe': timeframe}))
        return {
            'mode': 'paper',
            'flow': ['ctrader', 'strategy'],
            'signal': None,
            'ai_opinion': ai_opinion,
            'risk_override': 'risk_engine_is_final',
            'paper_fill': None,
            'executable': False,
        }
    context = dict(signal.context)
    context['ai_opinion'] = ai_opinion
    context['ai_cannot_override_risk'] = True
    signal = signal.model_copy(update={'context': context})
    result = evaluate_scenario(session, account_id, request_key, signal, market)
    result['ai_opinion'] = ai_opinion
    result['ai_cannot_override_risk'] = True
    result['market'] = {
        'source': 'ctrader', 'bid': str(tick.bid), 'ask': str(tick.ask),
        'spread_bps': str(tick.spread_bps), 'symbol': tick.symbol,
    }
    result['account_feed'] = {
        'source': 'ctrader', 'balance': str(broker.balance), 'equity': str(broker.equity),
        'positions': [p.model_dump(mode='json') for p in broker.positions],
    }
    result['executable'] = False
    if not result.get('allowed'):
        journal.record(
            session, account_id=account_id, signal_id=result['signal_id'],
            trade_context={'rejected': True, 'reasons': result.get('reasons'), 'ai_opinion': ai_opinion},
            opened_at=now, rule_compliant=False, pnl=Decimal('0'),
            entry_reason=','.join(result.get('reasons') or ()),
            exit_reason='RISK_REJECTED',
        )
        result['paper_fill'] = None
        result['journaled'] = 'rejected'
        return result
    fill = fill_paper(signal, tick)
    journal.record(
        session, account_id=account_id, signal_id=result['signal_id'],
        trade_context={
            'venue': 'paper',
            'broker_order': False,
            'fill_price': str(fill.fill_price),
            'bid': str(tick.bid),
            'ask': str(tick.ask),
            'ai_opinion': ai_opinion,
        },
        opened_at=fill.filled_at, rule_compliant=True, pnl=Decimal('0'),
        entry_reason=','.join(signal.reasons),
    )
    result['paper_fill'] = {
        'price': str(fill.fill_price),
        'venue': 'paper',
        'broker_order': False,
        'filled_at': fill.filled_at.isoformat(),
    }
    result['journaled'] = 'paper_open'
    result['scenario_only'] = False
    return result
