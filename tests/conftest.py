from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.models import Base, KillSwitch
from core.contracts import Signal, AccountState, MarketState

@pytest.fixture
def now(): return datetime.now(timezone.utc)
@pytest.fixture
def signal(now):
    return Signal(symbol='TEST',side='buy',entry='100',stop_loss='99',take_profit='102',
                  quantity='10',value_per_price_unit='1',timeframe='M5',strategy_version='test:1',created_at=now)
@pytest.fixture
def account(now):
    return AccountState(initial_balance='10000',balance='10000',equity='10000',
        day_start_balance='10000',risk_day=now.astimezone(ZoneInfo('Europe/Prague')).date(),
        open_risk='0',open_positions=0,trades_today=0,consecutive_losses=0,as_of=now,enabled=True)
@pytest.fixture
def market(now):
    return MarketState(as_of=now,connected=True,platform_ready=True,spread_bps='1',
        slippage_bps='1',volatility='0.01',news_known=True,news_checked_at=now)
@pytest.fixture
def factory():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    @event.listens_for(engine,'connect')
    def fk(dbapi,record):dbapi.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory.begin() as s:s.add(KillSwitch(id=1,active=True,reason='test'))
    yield factory
    engine.dispose()
