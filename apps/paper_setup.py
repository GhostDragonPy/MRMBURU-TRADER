"""Explicit local admin action: create a dedicated paper account, disabled initially.

Does not change global kill switch, secrets, environment or enable scheduling.
"""
from sqlalchemy import select
from core.database import session_factory
from core.models import Account
from core.contracts import RiskPolicy, PropRules

def main():
    with session_factory().begin() as session:
        account = session.scalar(select(Account).where(Account.name == 'paper-v04-eurusd'))
        if account is None:
            account = Account(name='paper-v04-eurusd', mode='paper', currency='USD',
                platform='local-paper', enabled=False, initial_balance='100000',
                risk_policy=RiskPolicy(risk_per_trade='0.0025', max_positions=1).model_dump(mode='json'),
                prop_rules=PropRules().model_dump(mode='json'))
            session.add(account)
            session.flush()
        print('PAPER_ACCOUNT_ID=' + account.id)
        print('Account enabled:', account.enabled, '; scheduler and global stop unchanged')

if __name__ == '__main__':
    main()
