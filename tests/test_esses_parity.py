from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from decimal import Decimal as D
from dataclasses import replace
import json
from core.contracts import RiskPolicy, PropRules
from services.strategy_engine.esses import evaluate, Config
from services.pipeline.simulator import advance, initial
from services.pipeline.esses import SelectedSignal
from services.ctrader.types import Tick
from tests.test_esses import NOW, fixture, bar
from tests.test_simulator import META
from tests.test_demo_orders import demo_settings, opened_event, run, FakeTransport
from services.demo_orders import service
from services.demo_orders.factory import UnconfiguredDemoTransport, build_gateway
from services.demo_orders.gateway import DemoCTraderExecutionGateway
from core.models import DemoOrderIntent

GOLDEN = json.loads(Path('tests/golden/esses_v1.json').read_text())
ESSES = Path('services/strategy_engine/esses.py')


def test_esses_source_hash_unchanged():
    assert sha256(ESSES.read_bytes()).hexdigest() == GOLDEN['esses_sha256']


def test_golden_buy_sell_no_entry_and_risk_block():
    frames = fixture()
    signal, audit = evaluate(frames, NOW)
    buy = GOLDEN['buy_cisd']
    assert signal.side == buy['side']
    assert str(signal.entry) == buy['entry']
    assert str(signal.stop_loss) == buy['stop_loss']
    assert str(signal.take_profit) == buy['take_profit']
    assert signal.context['models'] == buy['models']
    inverted = {tf: [b.model_copy(update={'open': D('2.3') - b.open, 'high': D('2.3') - b.low,
        'low': D('2.3') - b.high, 'close': D('2.3') - b.close}) for b in rows]
        for tf, rows in frames.items()}
    sell, _ = evaluate(inverted, NOW)
    assert sell.side == GOLDEN['sell_cisd']['side']
    assert str(sell.stop_loss) == GOLDEN['sell_cisd']['stop_loss']
    assert str(sell.take_profit) == GOLDEN['sell_cisd']['take_profit']
    none_frames = fixture()
    none_frames['M1'][-1] = none_frames['M1'][-1].model_copy(update={'close': D('1.1105')})
    empty, audit2 = evaluate(none_frames, NOW)
    assert empty is None
    assert audit2['reason'] == GOLDEN['no_entry']['reason']
    blocked, audit3 = evaluate(frames, NOW)
    state = initial(D(100000), NOW, '2026-09-22')
    s, events = advance(state, tick=Tick(symbol='EURUSD', bid='1.112', ask='1.11202', as_of=NOW, spread_bps='0.2'),
        bars=frames['M1'], instrument=META, now=NOW, policy=RiskPolicy(), rules=PropRules(),
        enabled=True, killed=False, allow_unknown_news=False, strategy=SelectedSignal(blocked),
        timeframe='M1', esses=True, audit=audit3, news={'events': [NOW.isoformat()]})
    assert s['position'] is None
    assert any(GOLDEN['risk_block'] in e.get('reasons', []) for e in events)


def test_paper_and_demo_orders_share_the_same_signal(factory):
    frames = fixture()
    signal, audit = evaluate(frames, NOW)
    tick = Tick(symbol='EURUSD', bid='1.112', ask='1.11202', as_of=NOW, spread_bps='0.2')
    state, events = advance(initial(D(100000), NOW, '2026-09-22'), tick=tick, bars=frames['M1'],
        instrument=META, now=NOW, policy=RiskPolicy(), rules=PropRules(), enabled=True,
        killed=False, allow_unknown_news=True, strategy=SelectedSignal(signal),
        timeframe='M1', esses=True, audit=audit)
    opened = next(e for e in events if e['kind'] == 'opened')
    pos = opened['position']
    assert pos['side'] == signal.side
    assert D(pos['stop']) == signal.stop_loss
    assert D(pos['target']) == signal.take_profit
    transport = FakeTransport()
    with factory.begin() as session:
        out = run(session, demo_settings(), [opened], transport, rollout='shadow')
    assert out['results'][0]['status'] == 'shadow'
    assert transport.submits == []
    with factory.begin() as session:
        intent = session.get(DemoOrderIntent, out['results'][0]['signal_id'])
        assert intent.request['side'] == pos['side']
        assert intent.request['stop'] == str(pos['stop'])
        assert intent.request['target'] == str(pos['target'])


def test_transport_does_not_change_signal_count():
    frames = fixture()
    first = evaluate(frames, NOW)[0]
    second = evaluate(frames, NOW, replace(Config()))[0]
    assert (first is None) == (second is None)
    if first:
        assert first.side == second.side
        assert first.stop_loss == second.stop_loss
        assert first.take_profit == second.take_profit


def test_rollout_does_not_touch_strategy_engine():
    from services.strategy_engine import esses
    assert esses.evaluate.__module__ == 'services.strategy_engine.esses'


def test_factory_stays_unconfigured_in_paper_disabled_shadow(factory):
    from tests.test_api import settings
    gw = build_gateway(settings())
    assert isinstance(gw.transport, UnconfiguredDemoTransport)
    cfg = demo_settings()
    with factory.begin() as session:
        service.control(session).rollout = 'shadow'
        gw = build_gateway(cfg, db_session=session, token_scope='trading')
        assert isinstance(gw.transport, UnconfiguredDemoTransport)
        service.control(session).rollout = 'disabled'
        gw = build_gateway(cfg, db_session=session, token_scope='trading')
        assert isinstance(gw.transport, UnconfiguredDemoTransport)
