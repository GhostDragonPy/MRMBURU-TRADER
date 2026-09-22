from decimal import Decimal
from datetime import datetime, timezone
from core.models import JournalEntry, uid
from services.journal.classification import classify


def record(session, *, account_id, signal_id, trade_context, opened_at,
           pnl=None, result_r=None, rule_compliant=None, entry_reason=None,
           exit_reason=None, closed_at=None, execution_error=False, strategy_error=False):
    quality = None
    if rule_compliant is not None and pnl is not None:
        quality = classify(rule_compliant=rule_compliant, pnl=pnl,
                           execution_error=execution_error, strategy_error=strategy_error)
    row = JournalEntry(
        id=uid(), account_id=account_id, signal_id=signal_id,
        trade_context=trade_context, pnl=pnl, result_r=result_r,
        rule_compliant=rule_compliant,
        outcome=None if quality is None else quality['outcome'],
        error_type=None if quality is None else quality['error_type'],
        entry_reason=entry_reason, exit_reason=exit_reason,
        opened_at=opened_at, closed_at=closed_at,
    )
    session.add(row)
    session.flush()
    return {'journal_id': row.id, 'quality': quality}
