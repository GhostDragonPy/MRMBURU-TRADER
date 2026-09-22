from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from copy import deepcopy
import pytest
from sqlalchemy import select, func
from core.contracts import RiskPolicy, PropRules, Signal
from core.models import Account, PaperLedger, PaperEvent, KillSwitch
from services.ctrader.types import Tick, SymbolInfo, OhlcBar
from services.pipeline.simulator import advance, initial, cycle, COMMISSION, SLIPPAGE

NOW = datetime(2026, 9, 22, 12, 0, 10, tzinfo=timezone.utc)

def tick(now=NOW, bid='1.10000', ask='1.10002'):
    return Tick(symbol='EURUSD', bid=bid, ask=ask, spread_bps='0.2', as_of=now)

def bars(now=NOW):
    end = now.replace(minute=now.minute//15*15, second=0, microsecond=0)
    return [OhlcBar(symbol='EURUSD', timeframe='M15', open='1.1', high='1.2', low='1',
        close='1.1', volume='1', closed_at=end-timedelta(minutes=15*i)) for i in reversed(range(30))]

META = SymbolInfo(name='EURUSD', digits=5, pip_position=4, lot_size='100000',
    min_volume='1000', max_volume='1000000', step_volume='1000')

class Strategy:
    def __init__(self, side='buy'): self.side = side
    def generate(self, *a, **kw):
        return Signal(symbol='EURUSD', timeframe='M15', side=self.side, entry='1.1',
            stop_loss='1.09' if self.side=='buy' else '1.11',
            take_profit='1.12' if self.side=='buy' else '1.08', quantity='1',
            value_per_price_unit='1', created_at=kw['now'], strategy_version='test')

def run(state=None, **kw):
    opts = dict(tick=tick(), bars=bars(), instrument=META, now=NOW,
        policy=RiskPolicy(), rules=PropRules(), enabled=True, killed=False,
        allow_unknown_news=True, strategy=Strategy())
    opts.update(kw)
    return advance(state or initial(D('100000'), NOW, '2026-09-22'), **opts)

@pytest.mark.parametrize('side,bid,ask,reason', [
    ('buy','1.12','1.12002','TARGET'), ('buy','1.085','1.08502','STOP'),
    ('sell','1.07998','1.08','TARGET'), ('sell','1.11498','1.115','STOP')])
def test_roundtrip_costs(side,bid,ask,reason):
    state, events = run(strategy=Strategy(side))
    assert events[0]['kind']=='opened'
    p = state['position']; units = D(p['units'])
    assert D(p['risk']) <= D('250')
    assert units % META.step_volume == 0
    assert D(state['balance']) == D('100000')-units*COMMISSION
    t = NOW+timedelta(seconds=10)
    end, events = run(state, now=t, tick=tick(t,bid,ask))
    closed = events[0]
    assert closed['reason']==reason
    direction = D(1) if side=='buy' else D(-1)
    exit = D(bid if side=='buy' else ask)-direction*SLIPPAGE
    pnl=(exit-D(p['entry']))*direction*units-2*units*COMMISSION
    assert D(closed['pnl'])==pnl
    assert D(end['balance'])==D('100000')+pnl
    assert end['equity']==end['balance']
    assert end['position'] is None and end['closed_trades']==1
    assert end['consecutive_losses']==int(reason=='STOP')

def test_same_tick_and_bar_never_duplicate():
    s, _=run()
    same, events=run(s)
    assert same==s and not events
    t=NOW+timedelta(seconds=10)
    again, events=run(s,now=t,tick=tick(t))
    assert again['position']==s['position'] and not events
    assert again['trades_today']==1

@pytest.mark.parametrize('kw,reason', [({'killed':True},'GLOBAL_KILL_SWITCH'),
    ({'enabled':False},'ACCOUNT_DISABLED'), ({'allow_unknown_news':False},'NEWS_UNKNOWN'),
    ({'policy':RiskPolicy(max_spread_bps='0.01')},'SPREAD_LIMIT')])
def test_entry_gates(kw,reason):
    s,e=run(**kw)
    assert s['position'] is None and reason in e[0]['reasons']

@pytest.mark.parametrize('field,value,reason', [('trades_today',5,'DAILY_TRADE_LIMIT'),
    ('consecutive_losses',3,'LOSING_STREAK_LIMIT'),('balance','97000','DAILY_LOSS_LIMIT'),
    ('balance','94000','TOTAL_LOSS_LIMIT')])
def test_account_limits(field,value,reason):
    s=initial(D('100000'),NOW,'2026-09-22'); s[field]=value
    s,e=run(s)
    assert reason in e[0]['reasons'] and s['position'] is None

def test_quote_gap_pauses_and_does_not_invent_historical_fill():
    s,_=run(); t=NOW+timedelta(minutes=3)
    s,e=run(s,now=t,tick=tick(t,'1.08','1.08002'))
    assert s['paused']=='QUOTE_GAP_REVIEW_REQUIRED'
    assert e[-1]['reason']=='STOP' and e[-1]['quote_gap']
    assert e[-1]['exit']=='1.07999'

def test_restart_preserves_open_position_and_stop_even_when_disabled():
    import json
    s,_=run(); recovered=json.loads(json.dumps(s))
    t=NOW+timedelta(seconds=10)
    s,e=run(recovered, now=t,tick=tick(t,'1.08','1.08002'),killed=True,enabled=False)
    assert s['position'] is None and e[-1]['reason']=='STOP'

def test_bad_quotes_leave_original_state_unchanged():
    s=initial(D('100000'),NOW,'2026-09-22'); before=deepcopy(s)
    for bad in [tick(NOW-timedelta(minutes=1)),tick(NOW+timedelta(seconds=1)),tick(bid='1.2',ask='1.1')]:
        with pytest.raises(ValueError): run(s,tick=bad)
    assert s==before

def test_daily_rollover_and_session_close():
    s,_=run(); t=NOW.replace(hour=20)
    s,e=run(s,now=t,tick=tick(t))
    assert e[-1]['reason']=='SESSION_END'
    t=t+timedelta(days=1)
    s,e=run(s,now=t,tick=tick(t),bars=bars(t))
    assert s['day']=='2026-09-23' and s['trades_today']==0
    assert s['day_start']==s['balance']

def test_no_signal_once_per_bar_and_short_history():
    s,e=run(strategy=SimpleNamespace(generate=lambda *a,**k:None))
    assert e[0]['kind']=='no_signal'
    t=NOW+timedelta(seconds=10)
    s,e=run(s,now=t,tick=tick(t))
    assert not e
    s,e=run(bars=bars()[:10]); assert not e and s['last_bar'] is None

def test_minimum_and_metadata_fail_closed():
    s,e=run(instrument=META.model_copy(update={'min_volume':D('1000000')}))
    assert e[0]['reasons']==['BELOW_MIN_VOLUME']
    with pytest.raises(ValueError,match='metadata'):
        run(instrument=META.model_copy(update={'step_volume':None}))

def test_bar_order_future_and_stale():
    with pytest.raises(ValueError):run(bars=list(reversed(bars())))
    with pytest.raises(ValueError):run(bars=bars(NOW+timedelta(minutes=15)))
    s,e=run(bars=bars(NOW-timedelta(minutes=15)))
    assert not e and s['last_bar'] is None

def test_persistence_and_transaction_rollback(factory):
    with factory.begin() as session:
        session.get(KillSwitch,1).active=False
        a=Account(name='paper',currency='USD',enabled=True,initial_balance='100000',
            risk_policy=RiskPolicy().model_dump(mode='json'),prop_rules=PropRules().model_dump(mode='json'))
        session.add(a); session.flush(); aid=a.id
    feed=SimpleNamespace(tick=lambda _:tick(),ohlc=lambda *a:bars(),instrument=lambda _:META)
    with factory.begin() as session:
        first=cycle(session,feed,aid,now=NOW)
    with factory.begin() as session:
        second=cycle(session,feed,aid,now=NOW)
        assert first['state']==second['state']
        assert session.scalar(select(func.count()).select_from(PaperEvent))==1
    with pytest.raises(RuntimeError):
        with factory.begin() as session:
            row=session.get(PaperLedger,aid); row.state={**row.state,'balance':'1'}
            session.flush(); raise RuntimeError('rollback')
    with factory() as session:
        assert session.get(PaperLedger,aid).state['balance']=='100000.00000000'

def test_status_auth_and_disabled_defaults(factory):
    from fastapi.testclient import TestClient
    from tests.test_api import settings, Cache
    from apps.api.main import create_app
    with TestClient(create_app(settings(),factory,Cache())) as client:
        assert client.get('/paper/v04/status').status_code==401
        r=client.get('/paper/v04/status',headers={'x-api-key':'r'*40}).json()
        assert not r['enabled'] and not r['execution_enabled']
        assert r['state'] is None and not r['unknown_news_waived']

def test_end_to_end_actual_sma_restart_and_target(factory):
    with factory.begin() as session:
        session.get(KillSwitch,1).active=False
        a=Account(name='automatic',currency='USD',enabled=True,initial_balance='100000',
            risk_policy=RiskPolicy().model_dump(mode='json'),prop_rules=PropRules().model_dump(mode='json'))
        session.add(a); session.flush(); aid=a.id
    history=bars()
    history[-1]=history[-1].model_copy(update={'close':D('1.101')})
    feed=SimpleNamespace(tick=lambda _:tick(bid='1.101',ask='1.10102'),
        ohlc=lambda *a:history,instrument=lambda _:META)
    with factory.begin() as session:
        first=cycle(session,feed,aid,now=NOW,allow_unknown_news=True)
        assert first['events'][0]['kind']=='opened'
        assert first['state']['position']['side']=='buy'
    later=NOW+timedelta(seconds=10)
    feed.tick=lambda _:tick(later,'1.13','1.13002')
    # Closing must work even if metadata/history providers are unavailable.
    def unavailable(*a): raise RuntimeError('unavailable')
    feed.ohlc=feed.instrument=unavailable
    with factory.begin() as session:
        result=cycle(session,feed,aid,now=later,allow_unknown_news=True)
        assert result['events'][0]['reason']=='TARGET'
        assert D(result['state']['balance'])>D('100000')
    feed.ohlc=lambda *a:history
    feed.instrument=lambda _:META
    with factory.begin() as session:
        again=cycle(session,feed,aid,now=later,allow_unknown_news=True)
        assert not again['events']
        assert session.scalar(select(func.count()).select_from(PaperEvent))==2

def test_legacy_pipeline_cannot_write_scheduler_account(factory):
    from fastapi.testclient import TestClient
    from tests.test_api import settings, Cache
    from apps.api.main import create_app
    with TestClient(create_app(settings(paper_account_id='dedicated'),factory,Cache())) as client:
        r=client.post('/paper/accounts/dedicated/run',headers={'x-api-key':'r'*40},
            json={'symbol':'EURUSD','request_key':'legacy'})
        assert r.status_code==409

def test_gap_acknowledgement_requires_admin(factory):
    from fastapi.testclient import TestClient
    from tests.test_api import settings, Cache
    from apps.api.main import create_app
    with TestClient(create_app(settings(),factory,Cache())) as client:
        for path in ['acknowledge-gap','enable-account']:
            assert client.post('/paper/v04/'+path,headers={'x-api-key':'r'*40},
                json={'reason':'testing'}).status_code==401
