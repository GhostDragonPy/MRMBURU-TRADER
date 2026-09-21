from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from core.config import Settings
from core.models import Account, AuditEvent, KillSwitch, RiskDecisionRecord
from sqlalchemy import select,func
from apps.api.main import create_app

ADMIN='a'*40
RESEARCH='r'*40

def settings(**kw):
    return Settings(_env_file=None,postgres_password='p'*32,admin_api_key=ADMIN,research_api_key=RESEARCH,**kw)

class Cache:
    def ping(self):return True
    def close(self):pass
    def get(self, key): return None
    def setex(self, *args, **kwargs): return True
    def delete(self, key): return 0

@pytest.fixture
def client(factory):
    with TestClient(create_app(settings(),factory,Cache())) as c:yield c

@pytest.mark.parametrize('mode',['demo','challenge','funded','personal','live','PAPER'])
def test_mode_locked(mode):
    with pytest.raises(ValidationError):settings(trading_mode=mode)

def test_execution_env_locked():
    with pytest.raises(ValidationError):settings(execution_enabled=True)

def test_execution_enabled_coerces_false_string():
    s = settings(execution_enabled='false')
    assert s.execution_enabled is False

def test_secret_keys_distinct():
    with pytest.raises(ValidationError):
        Settings(_env_file=None,postgres_password='p'*32,admin_api_key=ADMIN,research_api_key=ADMIN)

def test_health_and_auth(client):
    root = client.get('/').json()
    assert root['service'] == 'MRMBURU TRADER'
    assert root['docs'] == '/docs'
    assert client.get('/health').json()['execution_enabled'] is False
    assert client.get('/ready').status_code==200
    providers=client.get('/research/providers',headers={'x-api-key':RESEARCH}).json()
    assert providers['macro']['provider']=='fred'
    assert providers['macro']['prices'] is False
    assert providers['market_data']['provider']=='ctrader'
    assert providers['market_data']['account_id'] is None
    assert client.get('/market/ctrader/quote',headers={'x-api-key':RESEARCH},params={'symbol':'EURUSD'}).status_code==401
    assert client.get('/accounts').status_code==401
    assert client.post('/control/resume-paper',headers={'x-api-key':RESEARCH},json={'reason':'bypass'}).status_code==401
    assert client.post('/accounts',headers={'x-api-key':RESEARCH},json={}).status_code==401
    assert client.post('/orders',json={}).status_code==404


def test_workflow(client,factory,account,signal,market):
    ah={'x-api-key':ADMIN};rh={'x-api-key':RESEARCH}
    r=client.post('/accounts',headers=ah,json={'name':'test','initial_balance':'10000'})
    assert r.status_code==201
    aid=r.json()['id']
    assert client.post(f'/accounts/{aid}/synthetic-state',headers=ah,json=account.model_dump(mode='json')).status_code==200
    body={'request_key':'one','signal':signal.model_dump(mode='json'),'market':market.model_dump(mode='json')}
    path=f'/research/accounts/{aid}/evaluate'
    r=client.post(path,headers=rh,json=body)
    assert r.status_code==200,r.text
    assert 'GLOBAL_KILL_SWITCH' in r.json()['reasons']
    assert client.post('/control/resume-paper',headers=ah,json={'reason':'test scenario'}).status_code==200
    body['request_key']='two'
    r=client.post(path,headers=rh,json=body)
    assert r.json()['allowed'] and r.json()['executable'] is False
    duplicate=client.post(path,headers=rh,json=body)
    assert duplicate.json()==r.json()
    body['signal']['quantity']='11'
    assert client.post(path,headers=rh,json=body).status_code==409
    assert client.post('/control/stop',headers=ah,json={'reason':'end test'}).status_code==200
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(RiskDecisionRecord))==2
        assert s.get(KillSwitch,1).active
        assert s.scalar(select(func.count()).select_from(AuditEvent))>=5

def test_missing_gate_fails_closed(client,factory):
    with factory.begin() as s:s.delete(s.get(KillSwitch,1))
    assert client.get('/ready').status_code==503
    assert client.get('/status',headers={'x-api-key':RESEARCH}).json()['kill_switch']
    assert client.post('/control/resume-paper',headers={'x-api-key':ADMIN},json={'reason':'test'}).status_code==409

def test_missing_redis(factory):
    class Broken(Cache):
        def ping(self):raise ConnectionError()
    with TestClient(create_app(settings(),factory,Broken())) as c:
        assert c.get('/ready').status_code==503
