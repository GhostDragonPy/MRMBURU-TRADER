from decimal import Decimal
import pytest
from sqlalchemy import select, func
from core.models import Account, JournalEntry, KillSwitch, RiskDecisionRecord
from core.contracts import RiskPolicy, PropRules
from services.ctrader.fake import FakeCTraderFeed
from services.pipeline import paper as pipeline
from services.strategy_engine.engine import SmaConfig
from services.execution_engine.gateway import ExecutionGateway, ExecutionDisabled


def _account(session, *, enabled=True):
    row = Account(
        name='pipe', initial_balance=Decimal('10000'), enabled=enabled,
        risk_policy=RiskPolicy().model_dump(mode='json'),
        prop_rules=PropRules().model_dump(mode='json'),
        platform='ctrader',
    )
    session.add(row)
    session.flush()
    return row


def test_risk_rejects_even_if_ai_approves(factory):
    feed = FakeCTraderFeed(closes=[3, 2, 1, 5])
    with factory.begin() as s:
        s.get(KillSwitch, 1).active = True
        account = _account(s)
        result = pipeline.run(
            s, feed, account_id=account.id, request_key='ai-bypass',
            symbol='EURUSD', quantity=Decimal('10'), value_per_price_unit=Decimal('1'),
            ai_opinion={'approved': True, 'provider': 'deepseek'},
            strategy_config=SmaConfig(fast=2, slow=3),
        )
        assert result['allowed'] is False
        assert 'GLOBAL_KILL_SWITCH' in result['reasons']
        assert result['ai_opinion']['approved'] is True
        assert result['ai_cannot_override_risk'] is True
        assert result['paper_fill'] is None
        assert result['executable'] is False
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 1


def test_paper_fill_never_hits_broker(factory):
    feed = FakeCTraderFeed(closes=[3, 2, 1, 5], bid='1.10000', ask='1.10020')
    with factory.begin() as s:
        s.get(KillSwitch, 1).active = False
        account = _account(s)
        result = pipeline.run(
            s, feed, account_id=account.id, request_key='fill',
            symbol='EURUSD', quantity=Decimal('10'), value_per_price_unit=Decimal('1'),
            strategy_config=SmaConfig(fast=2, slow=3),
        )
        assert result['allowed'] is True
        assert result['executable'] is False
        assert result['paper_fill']['broker_order'] is False
        assert result['paper_fill']['venue'] == 'paper'
        assert result['market']['source'] == 'ctrader'
        assert result['account_feed']['source'] == 'ctrader'
        with pytest.raises(ExecutionDisabled):
            ExecutionGateway().submit(result)


def test_no_signal_skips_risk_and_fill(factory):
    feed = FakeCTraderFeed(closes=[2, 2, 2, 2])
    with factory.begin() as s:
        s.get(KillSwitch, 1).active = False
        account = _account(s)
        result = pipeline.run(
            s, feed, account_id=account.id, request_key='flat',
            symbol='EURUSD', strategy_config=SmaConfig(fast=2, slow=3),
        )
        assert result['signal'] is None
        assert result['paper_fill'] is None
        assert s.scalar(select(func.count()).select_from(RiskDecisionRecord)) == 0
