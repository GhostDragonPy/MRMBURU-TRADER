from datetime import timedelta
from decimal import Decimal
import pytest
from services.strategy_engine.engine import Candle,SmaConfig,SmaCross
from services.journal.classification import classify


def run(values,now):
    candles=[Candle(close=str(v),closed_at=now-timedelta(minutes=len(values)-i)) for i,v in enumerate(values)]
    return SmaCross(SmaConfig(fast=2,slow=3)).generate(candles,now=now,symbol='TEST',timeframe='M1',quantity=Decimal(1),value_per_price_unit=Decimal(1))

def test_buy(now):
    s=run([3,2,1,5],now)
    assert s.side=='buy' and s.stop_loss<s.entry<s.take_profit

def test_sell(now):
    assert run([1,2,3,0.5],now).side=='sell'

def test_no_signal(now):assert run([2,2,2,2],now) is None

def test_insufficient(now):assert run([2,3],now) is None

@pytest.mark.parametrize('future',[True,False])
def test_bad_candles(now,future):
    candles=[Candle(close=1,closed_at=now+timedelta(seconds=1) if future else now)]*4
    with pytest.raises(ValueError):
        SmaCross(SmaConfig(fast=2,slow=3)).generate(candles,now=now,symbol='X',timeframe='M1',quantity=1,value_per_price_unit=1)

def test_quality_independent_of_profit():
    assert classify(rule_compliant=True,pnl=-10)=={'quality':'good','outcome':'loss','error_type':None}
    assert classify(rule_compliant=False,pnl=10)['quality']=='bad'
