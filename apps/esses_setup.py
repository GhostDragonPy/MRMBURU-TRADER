"""Create/enable a dedicated Esses paper ledger; never places broker orders."""
import argparse
import json
from sqlalchemy import select
from core.database import session_factory
from core.models import Account, KillSwitch, AuditEvent
from core.contracts import RiskPolicy, PropRules


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--enable', action='store_true')
    p.add_argument('--resume-paper', action='store_true')
    a = p.parse_args()
    with session_factory().begin() as session:
        row = session.scalar(select(Account).where(Account.name == 'esses-v1-eurusd'))
        if row is None:
            row = Account(name='esses-v1-eurusd', mode='paper', currency='USD',
                platform='local-paper', enabled=False, initial_balance='100000',
                risk_policy=RiskPolicy(risk_per_trade='0.0025', max_positions=1,
                    max_trades_daily=2, daily_loss_fraction='0.01').model_dump(mode='json'),
                prop_rules=PropRules(timezone='America/New_York').model_dump(mode='json'))
            session.add(row)
            session.flush()
        if a.enable:
            row.enabled = True
        if a.resume_paper:
            gate=session.get(KillSwitch,1)
            if gate is None:
                gate=KillSwitch(id=1,active=False,reason='Explicit Esses demo installation')
                session.add(gate)
            else:
                gate.active=False
                gate.reason='Explicit Esses demo installation'
            session.add(AuditEvent(actor='local-admin',action='esses.resume-paper',payload={'account_id':row.id}))
        print(json.dumps({'account_id':row.id, 'enabled':row.enabled, 'execution_enabled':False}))


if __name__ == '__main__':
    main()
