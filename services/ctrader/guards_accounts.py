"""Account identity helpers shared by market-data and prop-sim."""
FORBIDDEN_EXECUTION_ACCOUNTS = frozenset({'48803059'})


def mask_account(account_id):
    text = str(account_id or '')
    if len(text) <= 4:
        return '****'
    return ('*' * (len(text) - 4)) + text[-4:]
