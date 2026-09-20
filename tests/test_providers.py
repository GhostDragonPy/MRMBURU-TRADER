from core.config import Settings
from services.ai_engine import deepseek
from services.ctrader import client as ctrader
from services.market_data import fred

ADMIN='a'*40
RESEARCH='r'*40

def settings(**kw):
    return Settings(_env_file=None,postgres_password='p'*32,admin_api_key=ADMIN,research_api_key=RESEARCH,**kw)

def test_optional_provider_keys_default_off():
    s=settings()
    assert deepseek.configured(s) is False
    assert fred.configured(s) is False
    assert ctrader.configured(s) is False

def test_fred_maps_fx_aliases():
    assert fred.resolve_series('eurusd')=='DEXUSEU'
    assert fred.resolve_series('DGS10')=='DGS10'

def test_providers_report_without_secrets():
    s=settings(deepseek_api_key='sk-test',fred_api_key='fred-test',
               ctrader_client_id='40796_id',ctrader_client_secret='secret')
    assert deepseek.configured(s) and fred.configured(s) and ctrader.configured(s)
    status=ctrader.status(s)
    assert status['execution_enabled'] is False
    assert status['orders']=='disabled'
    assert status['market_data']=='principal'
    assert '40796_id' in status['authorization_url']
    assert 'secret' not in str(status)
