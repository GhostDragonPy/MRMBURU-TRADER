"""EURUSD/USD paper simulator. No broker execution; all prices are sampled quotes.

Costs are explicit simulation assumptions, NOT the broker's fee schedule.
Missing quote intervals halt entries for review; stops are not guaranteed fills.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo
from sqlalchemy import select
from core.contracts import AccountState, MarketState, RiskPolicy, PropRules
from core.models import Account, AutomaticPaperControl, KillSwitch, PaperLedger, PaperEvent
from services.risk_engine.engine import evaluate
from services.prop_firm_engine.engine import evaluate as evaluate_prop
from services.strategy_engine.engine import Candle, SmaCross, SmaConfig

D = Decimal
# USD per base EUR unit, each side. $3.50/100,000 units and 0.1 pip slippage.
COMMISSION = D('0.000035')
SLIPPAGE = D('0.00001')
VERSION = 'eurusd-paper-v1'

def calculate_position_size(*, equity, entry, stop_loss, risk_fraction, instrument,
                            commission=COMMISSION, slippage=SLIPPAGE):
    """Shared EURUSD sizing for automatic and manual paper entries."""
    equity, entry, stop_loss = D(equity), D(entry), D(stop_loss)
    risk_fraction = min(D(risk_fraction), D('0.0025'))
    if risk_fraction <= 0 or not all((instrument.min_volume, instrument.max_volume,
                                      instrument.step_volume, instrument.lot_size)):
        raise ValueError('Invalid risk or volume metadata')
    per_unit = abs(entry-stop_loss) + 2*commission + slippage
    units = min(equity*risk_fraction/per_unit, instrument.max_volume, equity/entry)
    units = (units/instrument.step_volume).to_integral_value(rounding=ROUND_FLOOR)*instrument.step_volume
    if units < instrument.min_volume: raise ValueError('BELOW_MIN_VOLUME')
    return units

def stamp(value):
    return datetime.fromisoformat(value)

def initial(balance, now, day):
    return dict(version=VERSION, initial=str(balance), balance=str(balance),
        equity=str(balance), day=day, day_start=str(balance), trades_today=0,
        consecutive_losses=0, position=None, last_bar=None, last_tick=None,
        paused=None, realized='0', closed_trades=0, wins=0,
        news_known=False, executable=False, mode='paper')

def advance(state, *, tick, bars, instrument, now, policy, rules,
            enabled, killed, allow_unknown_news=False, strategy=None,
            timeframe='M15', esses=False, audit=None, news=None):
    """Pure transition; caller persists state and events atomically under row lock."""
    state = deepcopy(state)
    events = []
    def event(kind, **data):
        events.append(dict(kind=kind, at=now.isoformat(), **data))
    if state['version'] != VERSION:
        raise ValueError('Unsupported ledger version')
    if now.tzinfo is None or tick.as_of.tzinfo is None:
        raise ValueError('Timezone required')
    if tick.symbol != 'EURUSD' or instrument.name != 'EURUSD':
        raise ValueError('Only exact EURUSD is supported')
    if tick.ask < tick.bid:
        raise ValueError('Crossed quote')
    age = (now - tick.as_of).total_seconds()
    if not 0 <= age <= policy.max_data_age_seconds:
        raise ValueError('Stale or future quote: no fills or new entries')
    if state['last_tick'] and tick.as_of <= stamp(state['last_tick']):
        return state, events
    if state['last_tick'] and state['position'] and (tick.as_of-stamp(state['last_tick'])).total_seconds() > 60:
        if not state['paused']:
            state['paused'] = 'QUOTE_GAP_REVIEW_REQUIRED'
            event('paused', reason=state['paused'])
    state['last_tick'] = tick.as_of.isoformat()
    day = now.astimezone(ZoneInfo(rules.timezone)).date().isoformat()
    if state['day'] != day:
        state.update(day=day, day_start=state['balance'], trades_today=0, allocated_risk='0', used_setups=[])
        event('daily_rollover', balance=state['balance'])
    p = state['position']
    if p:
        direction = D(1) if p['side'] == 'buy' else D(-1)
        mark = tick.bid if p['side'] == 'buy' else tick.ask
        exit_price = mark - direction * SLIPPAGE
        units = D(p['units'])
        unrealized = (exit_price-D(p['entry']))*direction*units - COMMISSION*units
        state['equity'] = str(D(state['balance']) + unrealized)
        stopped = mark <= D(p['stop']) if p['side'] == 'buy' else mark >= D(p['stop'])
        target = mark >= D(p['target']) if p['side'] == 'buy' else mark <= D(p['target'])
        # No swap model: flatten at 20:00 UTC; after outages fills use observed quote.
        local = now.astimezone(ZoneInfo('America/New_York'))
        timed = (local.hour >= 11 or local.date() > stamp(p['opened_at']).astimezone(ZoneInfo('America/New_York')).date()) if esses else (now.hour >= 20 or now.date() > stamp(p['opened_at']).date())
        breached = (D(state['equity']) <= D(state['day_start'])-D(state['initial'])*policy.daily_loss_fraction
            or D(state['equity']) <= D(state['initial'])*(1-policy.total_loss_fraction))
        if stopped or target or timed or breached:
            pnl = unrealized - D(p['entry_fee'])
            state['balance'] = str(D(state['balance']) + unrealized)
            state['equity'] = state['balance']
            state['realized'] = str(D(state['realized']) + pnl)
            state['closed_trades'] += 1
            state['wins'] += int(pnl > 0)
            state['consecutive_losses'] = state['consecutive_losses'] + 1 if pnl < 0 else 0
            state['position'] = None
            event('closed', position=p, exit=str(exit_price), pnl=str(pnl),
                reason='STOP' if stopped else 'TARGET' if target else 'RISK_LIMIT' if breached else 'SESSION_END',
                quote_gap=bool(state['paused']))
        elif esses and bars and not state['paused']:
            from services.strategy_engine.esses import pivots, zones
            after = [b for b in bars if stamp(p['opened_at']) < b.closed_at <= now]
            if len(after) >= 6 and (now-after[-1].closed_at).total_seconds() <= 90:
                same_side = [z for z in zones(after) if z[1] == p['side'] and z[4] == 'FVG']
                retested = any(any(b.low <= z[3] and b.high >= z[2] for b in after[z[0]+1:-1]) for z in same_side)
                points = [x for x in pivots(after[:-1]) if x[1] == ('high' if direction > 0 else 'low')]
                broken = points and (after[-1].close > points[-1][2] if direction > 0 else after[-1].close < points[-1][2])
                better = D(p['stop']) < D(p['entry']) < mark if direction > 0 else mark < D(p['entry']) < D(p['stop'])
                if retested and broken and better:
                    p['stop'] = p['entry']
                    event('break_even', reason='POST_ENTRY_FVG_RETEST_AND_SWING_BREAK', stop=p['stop'])
        # Never close and reopen on the same sample.
        return state, events
    state['equity'] = state['balance']
    if not bars:
        return state, events
    if any(b.symbol != 'EURUSD' or b.timeframe != timeframe or b.closed_at > now for b in bars):
        raise ValueError('Invalid or unclosed bars')
    if any(a.closed_at >= b.closed_at for a, b in zip(bars, bars[1:])):
        raise ValueError('Bars must be unique and chronological')
    last = bars[-1].closed_at
    if state['last_bar'] and last <= stamp(state['last_bar']):
        return state, events
    if (now-last).total_seconds() > 120 or len(bars) < 21:
        return state, events
    if any((b.closed_at-a.closed_at).total_seconds() != (60 if timeframe == 'M1' else 900) for a,b in zip(bars[-21:],bars[-20:])):
        return state, events
    state['last_bar'] = last.isoformat()
    reasons = []
    if killed: reasons.append('GLOBAL_KILL_SWITCH')
    if not enabled: reasons.append('ACCOUNT_DISABLED')
    if state['paused']: reasons.append(state['paused'])
    if now.weekday() >= 5 or not 1 <= now.hour < 20: reasons.append('OUTSIDE_PAPER_SESSION')
    if D(state['equity']) <= 0: reasons.append('INSOLVENT')
    if esses:
        if state['trades_today'] >= 2: reasons.append('ESSES_TWO_TRADES_LIMIT')
        if D(state.get('allocated_risk', '0')) >= D('0.01'): reasons.append('ESSES_DAILY_RISK_BUDGET')
    if reasons:
        event('blocked', bar=last.isoformat(), reasons=reasons)
        return state, events
    strategy = strategy or SmaCross(SmaConfig())
    signal = strategy.generate([Candle(close=b.close, closed_at=b.closed_at) for b in bars],
        now=now, symbol='EURUSD', timeframe=timeframe, quantity=D(1), value_per_price_unit=D(1))
    if signal is None:
        event('no_signal', bar=last.isoformat(), audit=audit or {})
        return state, events
    if esses and signal.context.get('setup_id') in state.get('used_setups', []):
        event('blocked', reasons=['SETUP_ALREADY_TRADED'])
        return state, events
    direction = D(1) if signal.side == 'buy' else D(-1)
    entry = (tick.ask if signal.side == 'buy' else tick.bid) + direction*SLIPPAGE
    if not (signal.stop_loss < entry < signal.take_profit if direction > 0 else signal.take_profit < entry < signal.stop_loss):
        event('blocked', reasons=['PRICE_OUTSIDE_BRACKET'])
        return state, events
    if esses and abs(signal.take_profit-entry)/abs(entry-signal.stop_loss) < D(signal.context['min_rr']):
        event('blocked', reasons=['RR_AFTER_SPREAD_SLIPPAGE'])
        return state, events
    if not all((instrument.min_volume, instrument.max_volume, instrument.step_volume, instrument.lot_size)):
        raise ValueError('Missing volume metadata')
    if (instrument.pip_position != 4 or instrument.min_volume > instrument.max_volume
            or instrument.min_volume % instrument.step_volume != 0):
        raise ValueError('Unsupported EURUSD instrument metadata')
    budget = min(D(state['equity'])*policy.risk_per_trade, D(state['equity'])*D('0.0025'))
    if esses:
        budget = min(budget, max(D(0), D('0.01')-D(state.get('allocated_risk','0')))*D(state['equity']))
    try:
        units = calculate_position_size(equity=state['equity'], entry=entry,
            stop_loss=signal.stop_loss, risk_fraction=budget/D(state['equity']), instrument=instrument)
    except ValueError as exc:
        if str(exc) != 'BELOW_MIN_VOLUME':
            raise
        event('blocked', reasons=['BELOW_MIN_VOLUME'])
        return state, events
    signal = signal.model_copy(update=dict(entry=entry, quantity=units,
        value_per_price_unit=D(1), cost_reserve=units*(2*COMMISSION+SLIPPAGE)))
    account = AccountState(initial_balance=state['initial'], balance=state['balance'], equity=state['equity'],
        day_start_balance=state['day_start'], risk_day=day, open_risk='0', open_positions=0,
        trades_today=state['trades_today'], consecutive_losses=state['consecutive_losses'], as_of=now, enabled=enabled)
    spread = (tick.ask-tick.bid)/((tick.ask+tick.bid)/2)*10000
    returns = [abs(b.close/a.close-1) for a,b in zip(bars[-21:],bars[-20:])]
    market = MarketState(as_of=tick.as_of, connected=True, platform_ready=True,
        spread_bps=spread, slippage_bps=SLIPPAGE/entry*10000, volatility=max(returns),
        news_known=bool(news), news_checked_at=now,
        high_impact_events=tuple(stamp(t) for t in news.get('events', [])) if news else ())
    decision = evaluate(signal, account, market, policy, killed=killed, now=now, risk_timezone=rules.timezone)
    reasons = list(decision.reasons) + list(evaluate_prop(signal, account, rules))
    if allow_unknown_news:
        reasons = [r for r in reasons if r != 'NEWS_UNKNOWN']
    if reasons:
        event('blocked', reasons=reasons, news_known=False)
        return state, events
    fee = units*COMMISSION
    state['balance'] = str(D(state['balance'])-fee)
    state['position'] = dict(side=signal.side, units=str(units), entry=str(entry),
        lots=str(units/instrument.lot_size), pip_value_usd=str(units*D('0.0001')),
        stop=str(signal.stop_loss), target=str(signal.take_profit), entry_fee=str(fee),
        opened_at=now.isoformat(), risk=str(signal.risk_amount), bar=last.isoformat(),
        news_known=bool(news), unknown_news_waived=allow_unknown_news and not bool(news),
        strategy=signal.strategy_version, context=signal.context)
    if esses:
        state['allocated_risk'] = str(D(state.get('allocated_risk','0')) + signal.risk_amount/D(state['equity']))
        state['used_setups'] = state.get('used_setups', []) + [signal.context['setup_id']]
    mark = tick.bid if direction > 0 else tick.ask
    state['equity'] = str(D(state['balance'])+(mark-direction*SLIPPAGE-entry)*direction*units-fee)
    state['trades_today'] += 1
    event('opened', position=state['position'], broker_order=False)
    return state, events

def cycle(session, feed, account_id, *, now=None, allow_unknown_news=False):
    # Network outside locks; account lock then serializes creation and all transitions.
    tick = feed.tick('EURUSD')
    previous = session.get(PaperLedger, account_id)
    # Existing positions need only a quote to exit: history/metadata outages
    # must not prevent a stop or take-profit. Refresh again after row locks.
    if previous is not None and previous.state.get('position'):
        from services.ctrader.types import SymbolInfo
        bars = []
        instrument = SymbolInfo(name='EURUSD', digits=5)
    else:
        bars = feed.ohlc('EURUSD', 'M15', 30)
        instrument = feed.instrument('EURUSD')
    now = now or datetime.now(timezone.utc)
    gate = session.scalar(select(KillSwitch).where(KillSwitch.id == 1).with_for_update())
    account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if account is None or account.mode != 'paper' or account.currency != 'USD':
        raise ValueError('A paper USD account is required')
    control = session.get(AutomaticPaperControl, 1, populate_existing=True)
    rules = PropRules.model_validate(account.prop_rules)
    row = session.get(PaperLedger, account_id, populate_existing=True)
    if row is None:
        row = PaperLedger(account_id=account_id, state=initial(account.initial_balance, now,
            now.astimezone(ZoneInfo(rules.timezone)).date().isoformat()))
        session.add(row)
    state, events = advance(row.state, tick=tick, bars=bars, instrument=instrument, now=now,
        policy=RiskPolicy.model_validate(account.risk_policy), rules=rules,
        enabled=account.enabled, killed=gate is None or gate.active or bool(control and control.paused), allow_unknown_news=allow_unknown_news)
    row.state = state
    for payload in events:
        session.add(PaperEvent(account_id=account_id, payload=payload, created_at=now))
    session.flush()
    return dict(state=state, events=events, executable=False)
