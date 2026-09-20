def classify(*, rule_compliant: bool, pnl, execution_error: bool = False,
             strategy_error: bool = False) -> dict:
    # Process quality and P&L are independent; never infer quality from a win.
    return {'quality': 'good' if rule_compliant else 'bad',
            'outcome': 'win' if pnl > 0 else 'loss' if pnl < 0 else 'flat',
            'error_type': 'execution' if execution_error else 'strategy' if strategy_error else None}
