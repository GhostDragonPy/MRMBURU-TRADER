from datetime import timedelta
from decimal import Decimal
import pytest
from pydantic import ValidationError
from core.contracts import RiskPolicy, Signal, PropRules, AccountState
from services.risk_engine.engine import evaluate
from services.prop_firm_engine.engine import evaluate as prop_evaluate
from services.execution_engine.gateway import ExecutionGateway, ExecutionDisabled


def check(signal,account,market,now,**kw):
    return evaluate(signal,account,market,RiskPolicy(),now=now,killed=kw.get('killed',False),risk_timezone='Europe/Prague')

def test_valid_is_never_executable(signal,account,market,now):
    d=check(signal,account,market,now)
    assert d.allowed and not d.executable and d.risk_amount==10

def test_stop(signal,account,market,now):
    assert 'GLOBAL_KILL_SWITCH' in check(signal,account,market,now,killed=True).reasons

@pytest.mark.parametrize('field,value,reason',[
 ('connected',False,'DISCONNECTED'),('platform_ready',False,'PLATFORM_NOT_READY'),
 ('news_known',False,'NEWS_UNKNOWN'),('spread_bps',Decimal('6'),'SPREAD_LIMIT'),
 ('slippage_bps',Decimal('4'),'SLIPPAGE_LIMIT'),('volatility',Decimal('.04'),'VOLATILITY_LIMIT')])
def test_market_blocks(signal,account,market,now,field,value,reason):
    assert reason in check(signal,account,market.model_copy(update={field:value}),now).reasons

@pytest.mark.parametrize('field,value,reason',[
 ('enabled',False,'ACCOUNT_DISABLED'),('open_positions',3,'POSITION_LIMIT'),
 ('trades_today',5,'DAILY_TRADE_LIMIT'),('consecutive_losses',3,'LOSING_STREAK_LIMIT'),
 ('open_risk',Decimal('100'),'OPEN_RISK_LIMIT'),('equity',Decimal('9810'),'DAILY_LOSS_LIMIT'),
 ('equity',Decimal('9510'),'TOTAL_LOSS_LIMIT')])
def test_account_blocks(signal,account,market,now,field,value,reason):
    assert reason in check(signal,account.model_copy(update={field:value}),market,now).reasons

@pytest.mark.parametrize('offset',[-31,1])
def test_stale_future(signal,account,market,now,offset):
    stamp=now+timedelta(seconds=offset)
    result=check(signal.model_copy(update={'created_at':stamp}),account.model_copy(update={'as_of':stamp}),
                 market.model_copy(update={'as_of':stamp,'news_checked_at':stamp}),now)
    assert all(f'{s}_STALE_OR_FUTURE' in result.reasons for s in ['SIGNAL','ACCOUNT','MARKET','NEWS'])

@pytest.mark.parametrize('offset',[-30,0,30])
def test_news_boundary(signal,account,market,now,offset):
    m=market.model_copy(update={'high_impact_events':(now+timedelta(minutes=offset),)})
    assert 'HIGH_IMPACT_NEWS' in check(signal,account,m,now).reasons

def test_rollover(signal,account,market,now):
    a=account.model_copy(update={'risk_day':account.risk_day-timedelta(days=1)})
    assert 'DAILY_BASELINE_NOT_ROLLED' in check(signal,a,market,now).reasons

def test_costs_count(signal,account,market,now):
    d=check(signal.model_copy(update={'cost_reserve':Decimal('41')}),account,market,now)
    assert d.risk_amount==51 and 'TRADE_RISK_LIMIT' in d.reasons

def test_equity_includes_unrealized_loss(signal,account,market,now):
    a=account.model_copy(update={'equity':Decimal('9790')})
    assert 'DAILY_LOSS_LIMIT' in check(signal,a,market,now).reasons

@pytest.mark.parametrize('field,value',[('quantity','NaN'),('entry','Infinity'),('quantity','-1'),
 ('stop_loss','101'),('take_profit','99'),('value_per_price_unit','0')])
def test_invalid_signal(signal,field,value):
    data=signal.model_dump();data[field]=value
    with pytest.raises(ValidationError):Signal.model_validate(data)

def test_prop_static_limit(signal,account):
    a=account.model_copy(update={'equity':Decimal('9510')})
    assert 'PROP_DAILY_LOSS_LIMIT' in prop_evaluate(signal,a,PropRules())

def test_execution_cannot_be_enabled():
    with pytest.raises(ExecutionDisabled):ExecutionGateway().submit({'approved':True,'mode':'live'})
