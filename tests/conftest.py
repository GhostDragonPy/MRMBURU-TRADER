from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.models import Base, KillSwitch
from core.contracts import Signal, AccountState, MarketState

ISOLATE_ENV = (
    'TRADING_MODE', 'DEMO_EXECUTION_ENABLED', 'DEMO_CTRADER_ACCOUNT_ID',
    'ESSES_BROKER_EXECUTION',     'CTRADER_ACCOUNT_ID', 'CTRADER_CLIENT_ID',
    'CTRADER_CLIENT_SECRET', 'CTRADER_ACCESS_TOKEN', 'CTRADER_NETWORK_ENABLED',
    'CTRADER_ENVIRONMENT',
    'DISCORD_BOT_ENABLED', 'DISCORD_BOT_TOKEN', 'DISCORD_API_KEY',
    'DISCORD_GUILD_ID', 'DISCORD_ADMIN_ROLE_ID', 'DISCORD_CHANNEL_ID',
    'DISCORD_ALLOWED_USER_IDS',
    'DEEPSEEK_API_KEY', 'FRED_API_KEY', 'PAPER_SCHEDULER_ENABLED',
    'PAPER_ALLOW_UNKNOWN_NEWS', 'PAPER_ACCOUNT_ID',
    'DEEPSEEK_API_KEY', 'FRED_API_KEY', 'PAPER_SCHEDULER_ENABLED',
    'EXECUTION_ENABLED', 'ALLOW_LIVE_TRADING', 'PROP_SIM_CTRADER_ACCOUNT_ID',
    'PROP_SIM_EXECUTION_ENABLED', 'PROP_SIM_ALLOWED_ACCOUNT_IDS',
    'PROP_SIM_ACKNOWLEDGED_LIVE_ENVIRONMENT', 'CTRADER_OAUTH_STATE_SECRET',
    'PROP_SIM_TRADER_LOGIN',
)


@pytest.fixture(autouse=True)
def isolate_process_env(monkeypatch):
    for key in ISOLATE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ALLOW_LIVE_TRADING', 'false')
    monkeypatch.setenv('EXECUTION_ENABLED', 'false')
    monkeypatch.setenv('PROP_SIM_EXECUTION_ENABLED', 'false')


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
