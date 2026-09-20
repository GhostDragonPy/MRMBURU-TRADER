"""Synthetic local API walkthrough. Never changes STOP or sends an order."""
import json
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from urllib.request import Request,urlopen
from core.config import get_settings


def main():
    settings=get_settings()
    def post(path,body,key):
        req=Request('http://127.0.0.1:8000'+path,data=json.dumps(body).encode(),
                    headers={'Content-Type':'application/json','X-API-Key':key},method='POST')
        with urlopen(req,timeout=10) as r:return json.load(r)
    admin=settings.admin_api_key.get_secret_value()
    research=settings.research_api_key.get_secret_value()
    a=post('/accounts',{'name':'synthetic-'+str(uuid4())[:8],'initial_balance':'10000'},admin)
    now=datetime.now(timezone.utc)
    stamp=now.isoformat()
    post('/accounts/'+a['id']+'/synthetic-state',{
        'initial_balance':'10000','balance':'10000','equity':'10000','day_start_balance':'10000',
        'risk_day':now.astimezone(ZoneInfo('Europe/Prague')).date().isoformat(),
        'open_risk':'0','open_positions':0,'trades_today':0,'consecutive_losses':0,
        'as_of':stamp,'enabled':True},admin)
    result=post('/research/accounts/'+a['id']+'/evaluate',{
        'request_key':str(uuid4()),
        'signal':{'symbol':'SYNTHETIC','side':'buy','entry':'100','stop_loss':'99',
                  'take_profit':'102','quantity':'10','value_per_price_unit':'1','timeframe':'M5',
                  'strategy_version':'manual-scenario:1','created_at':stamp},
        'market':{'as_of':stamp,'connected':True,'platform_ready':True,'spread_bps':'1',
                  'slippage_bps':'1','volatility':'.01','news_known':True,'news_checked_at':stamp}},research)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
