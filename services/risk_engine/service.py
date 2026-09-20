from datetime import datetime, timezone
from sqlalchemy import select
from core.contracts import AccountState, MarketState, PropRules, RiskPolicy, Signal
from core.models import Account, AccountSnapshot, AuditEvent, KillSwitch, RiskDecisionRecord, SignalRecord, uid
from services.risk_engine.engine import evaluate
from services.prop_firm_engine.engine import evaluate as evaluate_prop

class NotFound(ValueError): pass
class Conflict(ValueError): pass


def evaluate_scenario(session, account_id: str, request_key: str, signal: Signal, market: MarketState):
    # Shared global lock serializes STOP with scenario approvals. No execution occurs.
    gate = session.scalar(select(KillSwitch).where(KillSwitch.id == 1).with_for_update())
    account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if account is None: raise NotFound('Account not found')
    previous = session.scalar(select(SignalRecord).where(
        SignalRecord.account_id == account_id, SignalRecord.request_key == request_key))
    signal_json, market_json = signal.model_dump(mode='json'), market.model_dump(mode='json')
    if previous:
        if previous.payload != signal_json or previous.market_context != market_json:
            raise Conflict('Idempotency key already used with another payload')
        return session.scalar(select(RiskDecisionRecord).where(
            RiskDecisionRecord.signal_id == previous.id)).result
    snapshot = session.scalar(select(AccountSnapshot).where(AccountSnapshot.account_id == account_id)
                              .order_by(AccountSnapshot.observed_at.desc()).limit(1))
    if snapshot is None: raise Conflict('No account snapshot; submit synthetic state as administrator')
    state = AccountState.model_validate(snapshot.state).model_copy(update={'enabled': account.enabled})
    policy = RiskPolicy.model_validate(account.risk_policy)
    rules = PropRules.model_validate(account.prop_rules)
    decision = evaluate(signal, state, market, policy, killed=gate is None or gate.active,
                        now=datetime.now(timezone.utc), risk_timezone=rules.timezone)
    reasons = decision.reasons+evaluate_prop(signal, state, rules)
    result = decision.model_copy(update={'allowed': not reasons, 'reasons': reasons}).model_dump(mode='json')
    result.update({'signal_id': uid(), 'scenario_only': True})
    session.add(SignalRecord(id=result['signal_id'], account_id=account_id, request_key=request_key,
                             payload=signal_json, market_context=market_json, snapshot_id=snapshot.id))
    session.flush()
    session.add(RiskDecisionRecord(signal_id=result['signal_id'], result=result,
                                   policy_snapshot=account.risk_policy, rules_snapshot=account.prop_rules))
    session.add(AuditEvent(actor='research', action='scenario.evaluated', payload=result))
    return result


def set_kill_switch(session, *, active: bool, reason: str, actor: str):
    gate = session.scalar(select(KillSwitch).where(KillSwitch.id == 1).with_for_update())
    if gate is None: raise Conflict('Kill switch missing; apply migrations, cannot resume')
    gate.active, gate.reason, gate.changed_at = active, reason, datetime.now(timezone.utc)
    session.add(AuditEvent(actor=actor, action='kill_switch.stop' if active else 'kill_switch.resume_paper',
                           payload={'active': active, 'reason': reason}))
    return {'active': active, 'reason': reason, 'execution_enabled': False}
