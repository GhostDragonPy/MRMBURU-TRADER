from contextlib import asynccontextmanager
from hmac import compare_digest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import Field
from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from core.config import get_settings
from core.contracts import Contract, AccountState, MarketState, PropRules, RiskPolicy, Signal, Positive
from core.database import session_factory
from core.models import Account, AccountSnapshot, AuditEvent, KillSwitch, RiskDecisionRecord, StrategyVersion
from services.ai_engine import deepseek
from services.ctrader import client as ctrader
from services.ctrader.feed import feed_from_settings
from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
from services.market_data import fred
from services.pipeline import paper as paper_pipeline
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

class AiProposeRequest(Contract):
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(default='M15', min_length=1, max_length=16)
    quantity: Positive = Field(default='1')
    value_per_price_unit: Positive = Field(default='1')
    context: dict = Field(default_factory=dict)

class CtraderTokenBody(Contract):
    access_token: str = Field(min_length=8, max_length=4096)
    refresh_token: str | None = None
    expires_in: int = Field(default=86400, gt=0)


class PaperRunRequest(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(default='M15', min_length=1, max_length=16)
    quantity: Positive = Field(default='1')
    value_per_price_unit: Positive = Field(default='1')
    include_ai: bool = False


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
        return {
            'mode':'paper',
            'execution_enabled':False,
            'kill_switch':gate is None or gate.active,
            'ai':'deepseek' if deepseek.configured(settings) else None,
            'macro':'fred' if fred.configured(settings) else None,
            'market_data':'ctrader',
            'ctrader_configured': ctrader.configured(settings),
            'ctrader_authorized': bool(ctrader.access_token(settings, redis_client)),
        }

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

    @app.get('/research/providers', dependencies=[Depends(research)])
    def providers():
        return {
            'ai': {'provider': 'deepseek', 'configured': deepseek.configured(settings), 'role': 'advisory'},
            'macro': {'provider': 'fred', 'configured': fred.configured(settings), 'prices': False},
            'market_data': ctrader.status(settings, redis_client),
            'execution_enabled': False,
            'paper_pipeline': ['ctrader', 'strategy', 'risk', 'paper_fill', 'journal'],
        }

    @app.get('/research/ai/health', dependencies=[Depends(research)])
    def ai_health():
        try:
            return deepseek.ping(settings)
        except deepseek.DeepSeekUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post('/research/ai/propose', dependencies=[Depends(research)])
    def ai_propose(body:AiProposeRequest):
        quote = None
        try:
            quote = feed_from_settings(settings, redis_client).tick(body.symbol).model_dump(mode='json')
        except (CTraderAuthRequired, CTraderUnavailable):
            quote = None
        try:
            proposal = deepseek.propose(
                settings,
                symbol=body.symbol,
                timeframe=body.timeframe,
                quantity=body.quantity,
                value_per_price_unit=body.value_per_price_unit,
                quote=quote,
                context=body.context,
            )
        except (deepseek.DeepSeekUnavailable, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 503, str(exc)) from None
        return proposal | {
            'quote': quote,
            'price_source': 'ctrader' if quote else None,
            'risk_override': 'risk_engine_is_final',
            'executable': False,
        }

    @app.get('/research/macro/series', dependencies=[Depends(research)])
    def macro_series(symbol: str):
        try:
            return fred.quote(settings, symbol) | {'usage': 'macro_only', 'not_for_signals': True}
        except fred.FredUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get('/research/macro/calendar', dependencies=[Depends(research)])
    def macro_calendar():
        try:
            return fred.calendar(settings) | {'usage': 'macro_only'}
        except fred.FredUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    def _feed():
        try:
            return feed_from_settings(settings, redis_client)
        except CTraderAuthRequired as exc:
            raise HTTPException(401, str(exc)) from None
        except CTraderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get('/market/ctrader/status', dependencies=[Depends(research)])
    def ctrader_status():
        return ctrader.status(settings, redis_client)

    @app.get('/market/ctrader/authorize', dependencies=[Depends(research)])
    def ctrader_authorize():
        try:
            url = ctrader.authorization_url(settings, redis_client)
        except CTraderAuthRequired as exc:
            raise HTTPException(401, str(exc)) from None
        return {'authorization_url': url, 'account_id': settings.ctrader_account_id, 'orders': 'disabled',
                'scope': 'accounts', 'note': 'Use Account info until the Spotware app is Active. Trading scope needs KYC.'}

    @app.post('/market/ctrader/token', dependencies=[Depends(admin)])
    def ctrader_save_token(body: CtraderTokenBody):
        from services.ctrader import tokens as token_store
        token_store.save_tokens(redis_client, {
            'access_token': body.access_token,
            'refresh_token': body.refresh_token,
            'expires_in': body.expires_in,
        })
        return {'authorized': True, 'account_id': settings.ctrader_account_id, 'orders': 'disabled', 'scope': 'accounts'}

    @app.get('/research/ctrader/callback', response_class=HTMLResponse)
    def ctrader_callback(code: str = '', state: str = '', error: str = '', access_token: str = ''):
        from services.ctrader import tokens as token_store
        if error:
            return HTMLResponse(f'<h1>cTrader OAuth error</h1><p>{error}</p>', status_code=400)
        if access_token:
            token_store.save_tokens(redis_client, {'access_token': access_token, 'expires_in': 86400})
            return HTMLResponse('<h1>cTrader connected</h1><p>Account info token saved. Orders stay disabled.</p>')
        if state and not token_store.consume_state(redis_client, state):
            return HTMLResponse('<h1>Invalid OAuth state</h1><p>Retry /market/ctrader/authorize</p>', status_code=400)
        if not code:
            return HTMLResponse('<h1>Missing code</h1><p>Click Get token with Account info, then paste the token if shown.</p>', status_code=400)
        try:
            payload = ctrader.exchange_code(settings, code)
            if payload.get('errorCode') or payload.get('error'):
                return HTMLResponse(f'<h1>Token exchange failed</h1><p>{payload.get("description") or payload.get("error")}</p>', status_code=400)
            token_store.save_tokens(redis_client, payload)
        except (CTraderAuthRequired, CTraderUnavailable) as exc:
            return HTMLResponse(f'<h1>cTrader unavailable</h1><p>{exc}</p>', status_code=503)
        account = settings.ctrader_account_id or 'unknown'
        return HTMLResponse(
            f'<h1>cTrader connected</h1><p>Account {account} authorized for account/market data only. Orders stay disabled.</p>'
        )

    @app.get('/market/ctrader/symbols', dependencies=[Depends(research)])
    def ctrader_symbols():
        return [row.model_dump(mode='json') for row in _feed().symbols()]

    @app.get('/market/ctrader/quote', dependencies=[Depends(research)])
    def ctrader_quote(symbol: str):
        return _feed().tick(symbol).model_dump(mode='json')

    @app.get('/market/ctrader/ohlc', dependencies=[Depends(research)])
    def ctrader_ohlc(symbol: str, timeframe: str = 'M15', count: int = 100):
        return [row.model_dump(mode='json') for row in _feed().ohlc(symbol, timeframe, count)]

    @app.get('/market/ctrader/account', dependencies=[Depends(research)])
    def ctrader_account():
        return _feed().account().model_dump(mode='json')

    @app.get('/market/ctrader/positions', dependencies=[Depends(research)])
    def ctrader_positions():
        return [row.model_dump(mode='json') for row in _feed().positions()]

    @app.post('/paper/accounts/{account_id}/run', dependencies=[Depends(research)])
    def paper_run(account_id:str, body:PaperRunRequest, s=Depends(db)):
        feed = _feed()
        ai_opinion = None
        if body.include_ai:
            try:
                tick = feed.tick(body.symbol)
                ai_opinion = deepseek.propose(
                    settings, symbol=body.symbol, timeframe=body.timeframe,
                    quantity=body.quantity, value_per_price_unit=body.value_per_price_unit,
                    quote=tick.model_dump(mode='json'),
                )
            except deepseek.DeepSeekUnavailable:
                ai_opinion = {'available': False}
        try:
            return paper_pipeline.run(
                s, feed, account_id=account_id, request_key=body.request_key,
                symbol=body.symbol, timeframe=body.timeframe,
                quantity=body.quantity, value_per_price_unit=body.value_per_price_unit,
                ai_opinion=ai_opinion,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/journal', dependencies=[Depends(research)])
    def journal_list(s=Depends(db)):
        from core.models import JournalEntry
        rows = s.scalars(select(JournalEntry).order_by(JournalEntry.opened_at.desc()).limit(100))
        return [{'id': r.id, 'account_id': r.account_id, 'signal_id': r.signal_id,
                 'outcome': r.outcome, 'rule_compliant': r.rule_compliant,
                 'entry_reason': r.entry_reason, 'exit_reason': r.exit_reason,
                 'trade_context': r.trade_context} for r in rows]

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
