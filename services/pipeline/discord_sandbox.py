"""Isolated manual paper trading for Discord."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from core.contracts import AccountState, MarketState, PropRules, RiskPolicy, Signal
from core.models import (Account, AccountSnapshot, AuditEvent, DiscordInteraction,
    DiscordPaperPosition, RiskDecisionRecord, SignalRecord, uid)
from services.execution_engine.paper import close_paper, fill_paper
from services.journal import service as journal
from services.pipeline.simulator import COMMISSION, SLIPPAGE, calculate_position_size
from services.risk_engine.engine import evaluate

D = Decimal
ACCOUNT_NAME = 'discord-sandbox'
INITIAL_BALANCE = D('100000')
MAX_RISK = D('0.0025')

def account(session):
    row = session.scalar(select(Account).where(Account.name == ACCOUNT_NAME).with_for_update())
    if row is None:
        policy = RiskPolicy(risk_per_trade=MAX_RISK, max_open_risk=MAX_RISK,
                            max_positions=1, max_trades_daily=100)
        row = Account(name=ACCOUNT_NAME, initial_balance=INITIAL_BALANCE, currency='USD',
            platform='discord-paper', enabled=True, risk_policy=policy.model_dump(mode='json'),
            prop_rules=PropRules(name=ACCOUNT_NAME).model_dump(mode='json'))
        session.add(row); session.flush()
    if (row.mode != 'paper' or row.currency != 'USD' or D(row.initial_balance) != INITIAL_BALANCE
            or row.platform != 'discord-paper'):
        raise ValueError('Invalid discord-sandbox account configuration')
    return row

def balance(session, account_id):
    value = session.scalar(select(func.coalesce(func.sum(DiscordPaperPosition.pnl), 0)).where(
        DiscordPaperPosition.account_id == account_id,
        DiscordPaperPosition.closed_at.is_not(None)))
    return INITIAL_BALANCE + D(value or 0)

def _digest(action, payload):
    raw = json.dumps({'action':action, 'payload':payload}, sort_keys=True,
                     separators=(',', ':'), default=str).encode()
    return sha256(raw).hexdigest()

def idempotent(session, *, interaction_id, user_id, action, payload, operation):
    digest = _digest(action, payload)
    prior = session.get(DiscordInteraction, interaction_id)
    if prior is None:
        try:
            with session.begin_nested():
                prior = DiscordInteraction(interaction_id=interaction_id, user_id=user_id,
                    action=action, request_hash=digest, response={})
                session.add(prior)
                session.flush()
        except IntegrityError:
            prior = session.get(DiscordInteraction, interaction_id, populate_existing=True)
            if prior is None:
                raise
        else:
            result = dict(operation(), idempotent_replay=False, execution_enabled=False)
            prior.response = result
            session.add(AuditEvent(actor='discord:'+user_id, action='discord.'+action,
                payload={'interaction_id':interaction_id, 'result':result}))
            session.flush()
            return result
    if prior:
        if prior.request_hash != digest or prior.user_id != user_id:
            raise ValueError('Discord interaction ID already used with another request')
        return prior.response

def open_position(session, feed, *, interaction_id, user_id, side, stop_loss,
                  take_profit, risk_fraction, reason, now=None):
    now = now or datetime.now(timezone.utc)
    side = side.lower()
    risk_fraction, stop_loss, take_profit = map(D, (risk_fraction, stop_loss, take_profit))
    payload = {'side':side, 'stop_loss':str(stop_loss), 'take_profit':str(take_profit),
               'risk_fraction':str(risk_fraction), 'reason':reason}
    def execute():
        if side not in ('buy','sell'): raise ValueError('Side must be buy or sell')
        if not reason.strip(): raise ValueError('Reason is required')
        if (not all(value.is_finite() and value > 0 for value in
                    (risk_fraction, stop_loss, take_profit))
                or risk_fraction > MAX_RISK):
            raise ValueError('Risk must be between 0 and 0.25%')
        owner = account(session)
        opened = session.scalar(select(DiscordPaperPosition).where(
            DiscordPaperPosition.account_id == owner.id,
            DiscordPaperPosition.closed_at.is_(None)).with_for_update())
        if opened: raise ValueError('discord-sandbox already has an open position')
        tick, instrument = feed.tick('EURUSD'), feed.instrument('EURUSD')
        direction = D(1) if side == 'buy' else D(-1)
        entry = D(tick.ask if side == 'buy' else tick.bid) + direction*SLIPPAGE
        valid = stop_loss < entry < take_profit if side == 'buy' else take_profit < entry < stop_loss
        if not valid: raise ValueError('Invalid SL/market/TP bracket')
        equity = balance(session, owner.id)
        units = calculate_position_size(equity=equity, entry=entry, stop_loss=stop_loss,
            risk_fraction=risk_fraction, instrument=instrument)
        signal = Signal(symbol='EURUSD', side=side, entry=entry, stop_loss=stop_loss,
            take_profit=take_profit, quantity=units, value_per_price_unit=D(1),
            cost_reserve=units*(2*COMMISSION+SLIPPAGE), timeframe='manual',
            strategy_version='discord-sandbox:1', created_at=now,
            reasons=(reason.strip(),), context={'source':'discord'})
        state = AccountState(initial_balance=INITIAL_BALANCE, balance=equity, equity=equity,
            day_start_balance=equity, risk_day=now.date(), open_risk=0, open_positions=0,
            trades_today=0, consecutive_losses=0, as_of=now, enabled=True)
        spread = (D(tick.ask)-D(tick.bid))/((D(tick.ask)+D(tick.bid))/2)*10000
        market = MarketState(as_of=now, connected=True, platform_ready=True,
            spread_bps=spread, slippage_bps=SLIPPAGE/entry*10000, volatility=0,
            news_known=True, news_checked_at=now, source='ctrader')
        decision = evaluate(signal, state, market, RiskPolicy.model_validate(owner.risk_policy),
                            killed=False, now=now)
        if not decision.allowed: raise ValueError('Risk rejected: '+','.join(decision.reasons))
        snapshot = AccountSnapshot(account_id=owner.id, observed_at=now, risk_day=now.date(),
            state=state.model_dump(mode='json'), source='discord-paper')
        session.add(snapshot); session.flush()
        signal_id = uid()
        result = decision.model_dump(mode='json') | {'signal_id':signal_id,'scenario_only':False}
        session.add(SignalRecord(id=signal_id, account_id=owner.id,
            request_key='discord:'+interaction_id, payload=signal.model_dump(mode='json'),
            market_context=market.model_dump(mode='json'), snapshot_id=snapshot.id))
        session.flush()
        session.add(RiskDecisionRecord(signal_id=signal_id, result=result,
            policy_snapshot=owner.risk_policy, rules_snapshot=owner.prop_rules))
        fill = fill_paper(signal, tick, slippage=SLIPPAGE)
        position = DiscordPaperPosition(account_id=owner.id, signal_id=signal_id,
            side=side, symbol='EURUSD', units=units, entry=fill.fill_price,
            stop_loss=stop_loss, take_profit=take_profit, risk_amount=signal.risk_amount,
            reason=reason.strip(), opened_at=fill.filled_at)
        session.add(position)
        journal.record(session, account_id=owner.id, signal_id=signal_id,
            trade_context={'venue':'paper','broker_order':False,'source':'discord',
                           'fill_price':str(fill.fill_price)}, opened_at=fill.filled_at,
            rule_compliant=True, entry_reason=reason.strip())
        session.flush()
        return {'position_id':position.id,'account':ACCOUNT_NAME,'symbol':'EURUSD',
            'side':side,'units':str(units),'entry':str(fill.fill_price),
            'stop_loss':str(stop_loss),'take_profit':str(take_profit),
            'risk_amount':str(signal.risk_amount),'broker_order':False}
    return idempotent(session, interaction_id=interaction_id, user_id=user_id,
        action='paper_order', payload=payload, operation=execute)

def close_position(session, feed, *, interaction_id, user_id, position_id, reason, now=None):
    now = now or datetime.now(timezone.utc)
    payload = {'position_id':position_id,'reason':reason}
    def execute():
        if not reason.strip(): raise ValueError('Reason is required')
        owner = account(session)
        position = session.scalar(select(DiscordPaperPosition).where(
            DiscordPaperPosition.id == position_id,
            DiscordPaperPosition.account_id == owner.id).with_for_update())
        if position is None or position.closed_at is not None:
            raise ValueError('Open discord-sandbox position not found')
        exit_price = close_paper(position.side, feed.tick('EURUSD'))
        direction = D(1) if position.side == 'buy' else D(-1)
        pnl = (exit_price-D(position.entry))*direction*D(position.units)-2*D(position.units)*COMMISSION
        position.exit_price, position.pnl, position.closed_at = exit_price, pnl, now
        position.close_reason = reason.strip()
        risk = D(position.risk_amount)
        journal.close(session, signal_id=position.signal_id, pnl=pnl,
            result_r=pnl/risk if risk else None, exit_reason=reason.strip(), closed_at=now)
        return {'position_id':position.id,'account':ACCOUNT_NAME,'exit':str(exit_price),
                'pnl':str(pnl),'broker_order':False}
    return idempotent(session, interaction_id=interaction_id, user_id=user_id,
        action='paper_close', payload=payload, operation=execute)

def positions(session, *, include_closed=False, limit=20):
    owner = account(session)
    query = select(DiscordPaperPosition).where(DiscordPaperPosition.account_id == owner.id)
    if not include_closed: query = query.where(DiscordPaperPosition.closed_at.is_(None))
    rows = session.scalars(query.order_by(DiscordPaperPosition.opened_at.desc()).limit(limit))
    return [{'id':p.id,'symbol':p.symbol,'side':p.side,'units':str(p.units),
        'entry':str(p.entry),'stop_loss':str(p.stop_loss),'take_profit':str(p.take_profit),
        'risk_amount':str(p.risk_amount),'reason':p.reason,
        'opened_at':p.opened_at.isoformat(),'closed_at':p.closed_at.isoformat() if p.closed_at else None,
        'exit_price':str(p.exit_price) if p.exit_price is not None else None,
        'pnl':str(p.pnl) if p.pnl is not None else None} for p in rows]

def report(session):
    owner = account(session)
    all_rows = list(session.scalars(select(DiscordPaperPosition).where(
        DiscordPaperPosition.account_id == owner.id,
        DiscordPaperPosition.closed_at.is_not(None))))
    current = balance(session, owner.id)
    today = datetime.now(timezone.utc).date()
    rows = [item for item in all_rows if item.closed_at.date() == today]
    daily_pnl = sum((D(item.pnl) for item in rows), D(0))
    wins = sum(p.pnl > 0 for p in rows)
    peak, drawdown, running = current-daily_pnl, D(0), current-daily_pnl
    for p in sorted(rows, key=lambda item:item.closed_at):
        running += D(p.pnl); peak=max(peak,running); drawdown=max(drawdown,peak-running)
    total_r = sum((D(p.pnl)/D(p.risk_amount) for p in rows if p.risk_amount), D(0))
    return {'account':ACCOUNT_NAME,'date':today.isoformat(),'balance':str(current),
        'pnl':str(daily_pnl),
        'trades':len(rows),'win_rate':str(D(wins)/len(rows)*100 if rows else 0),
        'rr':str(total_r/len(rows) if rows else 0),'drawdown':str(drawdown),
        'execution_enabled':False}
