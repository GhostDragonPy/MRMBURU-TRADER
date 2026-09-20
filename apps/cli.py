import argparse
from core.database import session_factory
from services.risk_engine.service import set_kill_switch


def main():
    p=argparse.ArgumentParser(description='MRMBURU operator controls; Linux administrator only')
    p.add_argument('action',choices=['stop','resume-paper'])
    p.add_argument('--reason',required=True)
    args=p.parse_args()
    if len(args.reason.strip())<3:p.error('Provide a reason')
    with session_factory().begin() as s:
        result=set_kill_switch(s,active=args.action=='stop',reason=args.reason,actor='linux-cli')
    print(result)

if __name__=='__main__':main()
