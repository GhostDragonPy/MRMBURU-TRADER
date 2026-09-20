from contextlib import asynccontextmanager
from hmac import compare_digest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import Field
from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from core.config import get_settings
from core.contracts import Contract, AccountState, MarketState, PropRules, RiskPolicy, Signal, Positive
from core.database import session_factory
from core.models import Account, AccountSnapshot, AuditEvent, KillSwitch, RiskDecisionRecord, StrategyVersion
from services.risk_engine.service import Conflict, NotFound, evaluate_scenario, set_kill_switch
from services.strategy_engine.engine import Candle, SmaConfig, SmaCross

class CreateAccount(Contract):
    name: str = Field(min_length=1, max_length=128)
    initial_balance: Positive
    currency: str = Field(default='USD', pattern='^[A-Z]{3}$')
    policy: RiskPolicy = Field(default_factory=RiskPolicy)
    prop_rules: PropRules = Field(default_factory=PropRules)

class Scenario(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    signal: Signal
    market: MarketState

class Control(Contract):
    reason: str = Field(min_length=3, max_length=512)

class StrategyRequest(Contract):
    config: SmaConfig = Field(default_factory=SmaConfig)
    candles: list[Candle] = Field(max_length=10000)
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    quantity: Positive
    value_per_price_unit: Positive


def create_app(settings=None, factory=None, redis_client=None):
    settings = settings or get_settings()
    factory = factory or session_factory()
    redis_client = redis_client or Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)

    @asynccontextmanager
    async def lifespan(app):
        yield
        redis_client.close()

    app = FastAPI(title='MRMBURU TRADER', version='0.2.0', lifespan=lifespan)

    def admin(x_api_key: str = Header(default='')):
        if not compare_digest(x_api_key, settings.admin_api_key.get_secret_value()):
            raise HTTPException(401, 'Administrator key required')

    def research(x_api_key: str = Header(default='')):
        if not any(compare_digest(x_api_key,k.get_secret_value()) for k in
                   [settings.admin_api_key,settings.research_api_key]):
            raise HTTPException(401, 'API key required')

    def db():
        try:
            with factory.begin() as session:
                yield session
        except IntegrityError:
            raise HTTPException(409, 'Conflicting or invalid record') from None
        except SQLAlchemyError:
            raise HTTPException(503, 'Database unavailable; execution remains disabled') from None
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None

    @app.get('/')
    def root():
        # Avoid bare FastAPI {"detail":"Not Found"} on the public host root.
        return {
            'service': 'MRMBURU TRADER',
            'version': '0.2.0',
            'mode': 'paper',
            'execution_enabled': False,
            'health': '/health',
            'ready': '/ready',
            'docs': '/docs',
            'openapi': '/openapi.json',
        }

    @app.get('/health')
    def health():
        return {'status':'ok','mode':'paper','execution_enabled':False,'version':'0.2.0'}

    @app.get('/ready')
    def ready():
        try:
            with factory() as s:
                s.execute(text('SELECT 1'))
                if s.get(KillSwitch, 1) is None: raise RuntimeError('Missing migration')
            redis_client.ping()
        except Exception:
            raise HTTPException(503,'Dependencies or migrations not ready') from None
        return {'status':'ready'}

    @app.get('/status', dependencies=[Depends(research)])
    def status(s=Depends(db)):
        gate=s.get(KillSwitch,1)
        return {'mode':'paper','execution_enabled':False,'kill_switch':gate is None or gate.active}

    @app.post('/accounts', dependencies=[Depends(admin)], status_code=201)
    def add_account(body:CreateAccount,s=Depends(db)):
        try: ZoneInfo(body.prop_rules.timezone)
        except Exception: raise HTTPException(422,'Invalid risk timezone') from None
        row=Account(name=body.name,initial_balance=body.initial_balance,currency=body.currency,
                    risk_policy=body.policy.model_dump(mode='json'),prop_rules=body.prop_rules.model_dump(mode='json'))
        s.add(row); s.flush()
        s.add(AuditEvent(actor='admin',action='account.created',payload={'account_id':row.id}))
        return {'id':row.id,'mode':'paper','enabled':False}

    @app.get('/accounts', dependencies=[Depends(research)])
    def accounts(s=Depends(db)):
        return [{'id':a.id,'name':a.name,'enabled':a.enabled,'mode':a.mode} for a in s.scalars(select(Account))]

    @app.post('/accounts/{account_id}/synthetic-state', dependencies=[Depends(admin)])
    def state(account_id:str,body:AccountState,s=Depends(db)):
        account=s.scalar(select(Account).where(Account.id==account_id).with_for_update())
        if account is None: raise NotFound('Account not found')
        if body.initial_balance != account.initial_balance:
            raise HTTPException(422,'Initial balance cannot change through a snapshot')
        now=datetime.now(timezone.utc)
        age=(now-body.as_of).total_seconds()
        if age<0 or age>30: raise HTTPException(422,'Snapshot must be current')
        account.enabled=body.enabled
        row=AccountSnapshot(account_id=account_id,observed_at=body.as_of,risk_day=body.risk_day,
                            state=body.model_dump(mode='json'),source='synthetic')
        s.add(row);s.flush()
        s.add(AuditEvent(actor='admin',action='synthetic_state.updated',payload={'snapshot_id':row.id}))
        return {'snapshot_id':row.id,'source':'synthetic'}

    @app.post('/research/accounts/{account_id}/evaluate', dependencies=[Depends(research)])
    def evaluate_route(account_id:str,body:Scenario,s=Depends(db)):
        return evaluate_scenario(s,account_id,body.request_key,body.signal,body.market)

    @app.get('/research/decisions', dependencies=[Depends(research)])
    def decisions(s=Depends(db)):
        return [r.result for r in s.scalars(select(RiskDecisionRecord).order_by(
            RiskDecisionRecord.created_at.desc()).limit(100))]

    @app.post('/research/strategies/sma/signal', dependencies=[Depends(research)])
    def generate(body:StrategyRequest):
        try:
            signal=SmaCross(body.config).generate(body.candles,now=datetime.now(timezone.utc),
                     symbol=body.symbol,timeframe=body.timeframe,quantity=body.quantity,
                     value_per_price_unit=body.value_per_price_unit)
        except ValueError as e: raise HTTPException(422,str(e)) from None
        return {'signal':signal.model_dump(mode='json') if signal else None,'executable':False}

    @app.post('/control/stop', dependencies=[Depends(admin)])
    def stop(body:Control,s=Depends(db)):
        return set_kill_switch(s,active=True,reason=body.reason,actor='admin')

    @app.post('/control/resume-paper', dependencies=[Depends(admin)])
    def resume(body:Control,s=Depends(db)):
        return set_kill_switch(s,active=False,reason=body.reason,actor='admin')

    return app
