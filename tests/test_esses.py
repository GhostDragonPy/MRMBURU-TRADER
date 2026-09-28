from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from dataclasses import replace
import pytest
from core.contracts import RiskPolicy, PropRules
from services.ctrader.types import OhlcBar, Tick
from services.strategy_engine.esses import evaluate, Config, pivots, cisd, structure
from services.pipeline.simulator import advance, initial
from services.pipeline.esses import SelectedSignal
from tests.test_simulator import META

NOW = datetime(2026,9,22,14,0,1,tzinfo=timezone.utc)


def bar(tf, end, o, h, l, c):
    return OhlcBar(symbol='EURUSD', timeframe=tf, closed_at=end,
        open=str(o), high=str(h), low=str(l), close=str(c), volume='1')


def fixture():
    # A known upside structure break, untouched FVG, confirmed low, higher target.
    values = [(1.09,1.095,1.085,1.09)]*36
    values[10] = (1.09,1.10,1.085,1.09)
    values[23] = (1.09,1.10,1.088,1.098)
    values[24] = (1.098,1.12,1.097,1.115)
    values[25] = (1.115,1.13,1.105,1.125)
    values[26] = (1.125,1.14,1.12,1.13)
    values[27] = (1.13,1.135,1.115,1.12)
    values[28] = (1.12,1.125,1.11,1.115)
    values[29] = (1.115,1.12,1.108,1.11)
    values[30] = (1.11,1.115,1.106,1.11)
    for i in range(31,36):
        values[i] = (1.11,1.115,1.107,1.11)
    frames={}
    for tf,minutes in [('H1',60),('M15',15),('H4',240),('D1',1440)]:
        end=NOW.replace(second=0)
        seconds=minutes*60
        end=datetime.fromtimestamp(int(end.timestamp())//seconds*seconds,timezone.utc)
        if tf=='M15':
            end-=timedelta(minutes=15)
        frames[tf]=[bar(tf,end-timedelta(minutes=minutes*(35-i)),*v) for i,v in enumerate(values)]
    values1=[(1.11,1.113,1.107,1.11)]*40
    values1[-4]=(1.111,1.112,1.108,1.109)
    values1[-3]=(1.109,1.11,1.104,1.107) # sweep confirmed M15 low, touches gap
    values1[-2]=(1.107,1.110,1.106,1.109)
    values1[-1]=(1.109,1.114,1.108,1.112) # CISD above 1.111
    frames['M1']=[bar('M1',NOW.replace(second=0)-timedelta(minutes=39-i),*v) for i,v in enumerate(values1)]
    frames['M5']=[bar('M5',NOW.replace(second=0)-timedelta(minutes=5*(39-i)),1.11,1.115,1.107,1.11) for i in range(40)]
    return frames


def test_complete_cisd_setup_and_paper_entry():
    frames=fixture()
    signal,audit=evaluate(frames,NOW)
    assert signal is not None, audit
    assert 'CISD' in signal.context['models']
    assert signal.stop_loss == D('1.1039')
    assert signal.take_profit == D('1.14')
    tick=Tick(symbol='EURUSD',bid='1.112',ask='1.11202',as_of=NOW,spread_bps='0.2')
    state,events=advance(initial(D(100000),NOW,'2026-09-22'), tick=tick,
        bars=frames['M1'],instrument=META,now=NOW,policy=RiskPolicy(),rules=PropRules(),
        enabled=True,killed=False,allow_unknown_news=True,strategy=SelectedSignal(signal),
        timeframe='M1',esses=True,audit=audit)
    assert events[-1]['kind']=='opened',events
    assert state['position']['strategy']=='esses-research:1'
    assert D(state['position']['risk'])<=250
    assert state['trades_today']==1


def test_future_bar_and_unsorted_history_rejected():
    frames=fixture()
    frames['M1'][-1]=frames['M1'][-1].model_copy(update={'closed_at':NOW+timedelta(minutes=1)})
    assert evaluate(frames,NOW)[1]['reason']=='INVALID_HISTORY'
    frames=fixture(); frames['H1'].reverse()
    assert evaluate(frames,NOW)[1]['reason']=='INVALID_HISTORY'


def test_no_repaint_pivot():
    rows=fixture()['M1'][-4:-1]
    assert pivots(rows)==[]


def test_touch_without_close_does_not_confirm():
    frames=fixture()
    frames['M1'][-1]=frames['M1'][-1].model_copy(update={'close':D('1.1105')})
    assert evaluate(frames,NOW)[0] is None


def test_no_poi_after_previous_touch():
    frames=fixture()
    frames['M1'][-8]=frames['M1'][-8].model_copy(update={'low':D('1.104')})
    assert evaluate(frames,NOW)[0] is None


def test_models_can_be_tested_separately():
    signal,_=evaluate(fixture(),NOW,replace(Config(),models=('BOS_FVG',)))
    assert signal is None


def test_ny_session_uses_dst():
    from services.ctrader.stream import active, fx_open
    assert active(datetime(2026,9,22,13,30,tzinfo=timezone.utc))
    assert not active(datetime(2026,9,22,12,30,tzinfo=timezone.utc))
    assert active(datetime(2026,1,20,14,30,tzinfo=timezone.utc))
    assert not active(datetime(2026,1,20,13,30,tzinfo=timezone.utc))
    assert fx_open(datetime(2026,9,28,5,0,tzinfo=timezone.utc))
    assert not fx_open(datetime(2026,9,26,22,0,tzinfo=timezone.utc))


def test_cisd_anchor_and_doji_boundary():
    rows=fixture()['M1']
    assert cisd(rows,len(rows)-3,'buy')==D('1.111')
    assert structure(fixture()['H1'])=='buy'


@pytest.mark.parametrize('model', ['IFVG','CISD','BOS_FVG'])
@pytest.mark.parametrize('side', ['buy','sell'])
def test_all_entry_models_in_both_directions(model,side):
    f=fixture(); m=f['M1']
    variations={
        'IFVG':{35:(1.111,1.112,1.11,1.1105),36:(1.1105,1.111,1.106,1.108),37:(1.108,1.109,1.104,1.107)},
        'BOS_FVG':{32:(1.11,1.114,1.108,1.11),33:(1.11,1.112,1.108,1.109),34:(1.109,1.109,1.107,1.108),
            35:(1.108,1.11,1.104,1.107),36:(1.111,1.118,1.110,1.116),37:(1.116,1.12,1.113,1.118),
            38:(1.118,1.119,1.111,1.115),39:(1.115,1.116,1.109,1.112)}}
    for i,v in variations.get(model,{}).items():
        m[i]=bar('M1',m[i].closed_at,*v)
    if side=='sell':
        f={tf:[b.model_copy(update={'open':D('2.3')-b.open,'high':D('2.3')-b.low,
            'low':D('2.3')-b.high,'close':D('2.3')-b.close}) for b in rows] for tf,rows in f.items()}
    signal,audit=evaluate(f,NOW,replace(Config(),models=(model,)))
    assert signal is not None,audit
    assert signal.side==side
    assert signal.context['models']==[model]


@pytest.mark.parametrize('case,reason', [('news','HIGH_IMPACT_NEWS'),('unknown','NEWS_UNKNOWN'),
    ('trades','ESSES_TWO_TRADES_LIMIT'),('duplicate','SETUP_ALREADY_TRADED')])
def test_esses_entry_controls(case,reason):
    f=fixture(); signal,audit=evaluate(f,NOW)
    state=initial(D(100000),NOW,'2026-09-22')
    if case=='trades': state['trades_today']=2
    if case=='duplicate': state['used_setups']=[signal.context['setup_id']]
    news={'events':[NOW.isoformat()]} if case=='news' else None
    s,events=advance(state,tick=Tick(symbol='EURUSD',bid='1.112',ask='1.11202',as_of=NOW,spread_bps='0.2'),
        bars=f['M1'],instrument=META,now=NOW,policy=RiskPolicy(),rules=PropRules(),
        enabled=True,killed=False,allow_unknown_news=False,strategy=SelectedSignal(signal),
        timeframe='M1',esses=True,audit=audit,news=news)
    assert s['position'] is None
    assert any(reason in e.get('reasons',[]) for e in events),events


def test_structural_break_even_only_tightens_stop():
    f=fixture(); signal,audit=evaluate(f,NOW)
    tick=Tick(symbol='EURUSD',bid='1.112',ask='1.11202',as_of=NOW,spread_bps='0.2')
    opts=dict(instrument=META,policy=RiskPolicy(),rules=PropRules(),enabled=True,
        killed=False,allow_unknown_news=True,strategy=SelectedSignal(signal),timeframe='M1',esses=True)
    state,_=advance(initial(D(100000),NOW,'2026-09-22'),tick=tick,bars=f['M1'],now=NOW,**opts)
    vals=[(1.113,1.114,1.1125,1.1135),(1.1135,1.117,1.113,1.116),
        (1.116,1.119,1.116,1.118),(1.118,1.122,1.117,1.120),
        (1.120,1.121,1.115,1.118),(1.118,1.120,1.117,1.119),
        (1.119,1.124,1.118,1.123)]
    rows=[bar('M1',NOW.replace(second=0)+timedelta(minutes=i+1),*v) for i,v in enumerate(vals)]
    later=rows[-1].closed_at+timedelta(seconds=1)
    state['last_tick']=(later-timedelta(seconds=1)).isoformat()
    t=Tick(symbol='EURUSD',bid='1.123',ask='1.12302',as_of=later,spread_bps='0.2')
    state,events=advance(state,tick=t,bars=rows,now=later,**opts)
    assert state['position']['stop']==state['position']['entry'],events
    assert events[-1]['kind']=='break_even'


def test_position_management_survives_missing_history_and_news(factory):
    from services.pipeline.esses import cycle
    from core.models import Account, KillSwitch, PaperLedger
    from unittest.mock import Mock
    f=fixture()
    class Feed:
        def tick(self,symbol):
            return Tick(symbol='EURUSD',bid='1.112',ask='1.11202',as_of=NOW,spread_bps='0.2')
        def ohlc(self,symbol,tf,count):return f[tf]
        def instrument(self,symbol):return META
    cache=Mock();cache.get.return_value=None
    with factory.begin() as session:
        acct=Account(name='new',mode='paper',currency='USD',platform='local-paper',enabled=True,
            initial_balance=100000,risk_policy=RiskPolicy().model_dump(mode='json'),prop_rules=PropRules().model_dump(mode='json'))
        session.add(acct);session.flush();aid=acct.id
        session.get(KillSwitch,1).active=False
        result=cycle(session,Feed(),aid,cache=cache,now=NOW,allow_unknown_news=True)
        assert result['state']['position']
    feed=Mock();feed.tick.return_value=Tick(symbol='EURUSD',bid='1.100',ask='1.10002',
        as_of=NOW+timedelta(seconds=5),spread_bps='0.2')
    feed.ohlc.side_effect=RuntimeError('history unavailable')
    cache.get.side_effect=RuntimeError('calendar unavailable')
    with factory.begin() as session:
        result=cycle(session,feed,aid,cache=cache,now=NOW+timedelta(seconds=5))
        assert result['state']['position'] is None
        assert result['events'][-1]['reason']=='STOP'
