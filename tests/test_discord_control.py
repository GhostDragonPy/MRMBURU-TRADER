from decimal import Decimal
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from apps.api.main import create_app
from apps.discord_bot import gateway_lock, identity, personal
from apps.discord_bot.security import AuthorizationError, allowed, rate_limit
from apps.discord_bot import healthcheck
from core.models import (Account, AuditEvent, AutomaticPaperControl, DiscordInteraction,
    DiscordPaperPosition, JournalEntry, KillSwitch, PaperLedger)
from services.ctrader.fake import FakeCTraderFeed
from services.ctrader.types import SymbolInfo
from services.pipeline import discord_sandbox
from tests.test_api import Cache, settings

API_KEY='d'*40
HEADERS={'x-discord-api-key':API_KEY,'x-discord-user-id':'42'}

def test_service_key_must_be_strong_and_distinct_even_without_bot():
    with pytest.raises(ValidationError): settings(discord_api_key='short')
    with pytest.raises(ValidationError): settings(discord_api_key='a'*40)

class Feed(FakeCTraderFeed):
    def instrument(self, symbol):
        return SymbolInfo(name='EURUSD', digits=5, pip_position=4, lot_size='100000',
            min_volume='1000', max_volume='1000000', step_volume='1000')

@pytest.fixture
def discord_client(factory, monkeypatch):
    monkeypatch.setattr('apps.api.discord.feed_from_settings', lambda *args:Feed())
    configured=settings(discord_api_key=API_KEY)
    with TestClient(create_app(configured,factory,Cache())) as client: yield client

@pytest.mark.parametrize('values',[
    dict(guild_id=2,user_id=10,role_ids=[20]),
    dict(guild_id=1,user_id=11,role_ids=[20]),
    dict(guild_id=1,user_id=10,role_ids=[21])])
def test_unauthorized_guild_user_and_role_are_rejected(values):
    with pytest.raises(AuthorizationError):
        allowed(**values,expected_guild_id=1,admin_role_id=20,allowed_user_ids='10')

def test_api_service_authentication_required(discord_client):
    assert discord_client.get('/internal/discord/status').status_code == 401
    assert discord_client.get('/internal/discord/status', headers={
        'x-discord-api-key':'wrong','x-discord-user-id':'42'}).status_code == 401
    assert discord_client.get('/discord/status', headers=HEADERS).status_code == 404
    assert discord_client.get('/internal/discord/status',
        headers={'x-discord-api-key':API_KEY,'x-discord-user-id':'not-an-id'}).status_code == 400

def test_discord_routes_fail_closed_without_service_key(factory):
    with TestClient(create_app(settings(),factory,Cache())) as client:
        for path in ('status','positions','history','daily-report'):
            assert client.get('/internal/discord/'+path,headers=HEADERS).status_code == 401
        for path in ('paper-order','paper-close','pause','resume'):
            assert client.post('/internal/discord/'+path,headers=HEADERS,json={}).status_code == 401

def test_risk_above_point_25_and_missing_brackets_rejected(discord_client):
    base={'interaction_id':'100','side':'buy','stop_loss':'1.09','take_profit':'1.12',
          'risk_percent':'0.26','reason':'manual test'}
    assert discord_client.post('/internal/discord/paper-order',headers=HEADERS,json=base).status_code == 422
    del base['stop_loss']
    assert discord_client.post('/internal/discord/paper-order',headers=HEADERS,json=base).status_code == 422

@pytest.mark.parametrize('field,value', [
    ('risk_percent','NaN'), ('risk_percent','Infinity'),
    ('stop_loss','NaN'), ('take_profit','Infinity'),
    ('stop_loss','0'), ('take_profit','-1'),
])
def test_nonfinite_and_nonpositive_order_values_rejected(discord_client,field,value):
    body={'interaction_id':'numeric','side':'buy','stop_loss':'1.09',
          'take_profit':'1.12','risk_percent':'0.25','reason':'manual test'}
    body[field]=value
    assert discord_client.post('/internal/discord/paper-order',headers=HEADERS,json=body).status_code == 422

def test_wrong_direction_bracket_rejected(discord_client):
    body={'interaction_id':'bracket','side':'sell','stop_loss':'1.09',
          'take_profit':'1.12','risk_percent':'0.25','reason':'manual test'}
    assert discord_client.post('/internal/discord/paper-order',headers=HEADERS,json=body).status_code == 422

def test_manual_order_is_idempotent_isolated_and_never_broker(factory,monkeypatch):
    broker_calls=[]
    monkeypatch.setattr('services.execution_engine.gateway.ExecutionGateway.submit',
        lambda *args,**kwargs: broker_calls.append((args,kwargs)))
    feed=Feed()
    with factory.begin() as session:
        automatic=Account(name='esses-auto',initial_balance='100000',currency='USD',
            platform='local-paper',enabled=True,risk_policy={},prop_rules={})
        session.add(automatic); session.flush()
        original={'version':'eurusd-paper-v1','balance':'100000','position':None}
        session.add(PaperLedger(account_id=automatic.id,state=original))
    with factory.begin() as session:
        first=discord_sandbox.open_position(session,feed,interaction_id='777',user_id='42',
            side='buy',stop_loss='1.09',take_profit='1.12',
            risk_fraction=Decimal('0.0025'),reason='manual setup')
        second=discord_sandbox.open_position(session,feed,interaction_id='777',user_id='42',
            side='buy',stop_loss='1.09',take_profit='1.12',
            risk_fraction=Decimal('0.0025'),reason='manual setup')
        assert first == second
        assert first['account']=='discord-sandbox' and first['broker_order'] is False
        owner=session.scalar(select(Account).where(Account.name=='discord-sandbox'))
        assert owner.id != automatic.id and owner.initial_balance == Decimal('100000')
        assert session.scalar(select(func.count()).select_from(DiscordPaperPosition)) == 1
        assert session.scalar(select(func.count()).select_from(DiscordInteraction)) == 1
        assert broker_calls == []
        assert session.scalar(select(func.count()).select_from(JournalEntry).where(
            JournalEntry.account_id==owner.id)) == 1
        assert session.get(PaperLedger,automatic.id).state == original
        closed=discord_sandbox.close_position(session,feed,interaction_id='778',user_id='42',
            position_id=first['position_id'],reason='manual exit')
        replay=discord_sandbox.close_position(session,feed,interaction_id='778',user_id='42',
            position_id=first['position_id'],reason='manual exit')
        assert closed == replay and closed['broker_order'] is False
        assert discord_sandbox.report(session)['account'] == 'discord-sandbox'

def test_pause_resume_only_changes_automatic_paper_control(discord_client,factory):
    pause=discord_client.post('/internal/discord/pause',headers=HEADERS,
        json={'interaction_id':'pause-1','reason':'manual review'})
    assert pause.status_code == 200
    with factory() as session:
        assert session.get(AutomaticPaperControl,1).paused is True
        assert session.get(KillSwitch,1).active is True
    resume=discord_client.post('/internal/discord/resume',headers=HEADERS,
        json={'interaction_id':'resume-1','reason':'review complete'})
    assert resume.status_code == 200
    assert resume.json()['paper_only'] is True
    with factory() as session:
        assert session.get(AutomaticPaperControl,1).paused is False
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 2

def test_execution_remains_false(discord_client):
    assert discord_client.get('/internal/discord/status',headers=HEADERS).json()['execution_enabled'] is False

def test_rate_limit_uses_redis_counter():
    class Counter:
        def __init__(self): self.value=0
        def incr(self,key): self.value+=1; return self.value
        def expire(self,key,ttl): assert ttl==60
    cache=Counter()
    rate_limit(cache,user_id=1,limit=1)
    with pytest.raises(RuntimeError,match='Rate limit'): rate_limit(cache,user_id=1,limit=1)

def test_bot_healthcheck_requires_fresh_gateway_heartbeat(monkeypatch):
    class Cache:
        healthy = False
        def exists(self, key):
            assert key == 'discord:bot:healthy'
            return self.healthy
        def close(self): pass
    cache = Cache()
    monkeypatch.setenv('REDIS_URL','redis://unused:6379/0')
    monkeypatch.setattr(healthcheck.Redis, 'from_url', lambda *args, **kwargs:cache)
    with pytest.raises(SystemExit) as exc: healthcheck.main()
    assert exc.value.code == 1
    cache.healthy = True
    healthcheck.main()


class Memory:
    def __init__(self):
        self.kv = {}
        self.z = {}
    def set(self, key, value, nx=False, xx=False, ex=None):
        if nx and key in self.kv:
            return False
        if xx and key not in self.kv:
            return False
        self.kv[key] = value
        return True
    def get(self, key):
        return self.kv.get(key)
    def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0
    def incr(self, key):
        self.kv[key] = int(self.kv.get(key) or 0) + 1
        return self.kv[key]
    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
    def zrangebyscore(self, key, minimum, maximum):
        return [member for member, score in self.z.get(key, {}).items() if minimum <= score <= maximum]
    def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)


def test_identity_keeps_paper_and_existing_admin_commands():
    payload = identity.identity_payload()
    assert payload['name'] == 'GhostDragon'
    assert payload['execution_enabled'] is False
    assert payload['mode'] == 'paper'
    text = identity.help_text()
    for name in identity.ADMIN_COMMANDS + identity.PERSONAL_COMMANDS:
        assert '/'+name in text
    assert 'discord-sandbox' in text
    assert 'cTrader' in text


def test_gateway_lock_rejects_a_second_process_with_the_same_token(monkeypatch):
    cache = Memory()
    monkeypatch.setattr(gateway_lock.os, 'getpid', lambda: 11)
    assert gateway_lock.acquire(cache, 'shared-token') is True
    monkeypatch.setattr(gateway_lock.os, 'getpid', lambda: 12)
    with pytest.raises(RuntimeError, match='Another process'):
        gateway_lock.acquire(cache, 'shared-token')


def test_personal_reminders_and_usage_stay_local():
    cache = Memory()
    saved = personal.schedule_reminder(cache, user_id=42, channel_id=99, minutes=1, text='revisar paper')
    assert saved['paper_only'] is True
    personal.record_usage(cache, 42, 'ayuda')
    personal.record_usage(cache, 42, 'tokens')
    report = personal.usage_report(cache, 42)
    assert report['command_count'] == 2
    assert report['execution_enabled'] is False
    from datetime import datetime, timezone, timedelta
    due = personal.due_reminders(cache, now=datetime.now(timezone.utc)+timedelta(minutes=2))
    assert due[0]['text'] == 'revisar paper'
    assert personal.due_reminders(cache) == []


def test_weather_and_search_use_public_lookups(monkeypatch):
    class Response:
        def __init__(self, body): self.body = body.encode()
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def fake_open(request, timeout=8):
        url = request.get_full_url() if hasattr(request, 'get_full_url') else str(request)
        if 'wttr.in' in url:
            return Response('Asuncion: ⛅ +24°C')
        return Response('{"Heading":"EURUSD","AbstractText":"Par de divisas.","RelatedTopics":[]}')
    monkeypatch.setattr(personal, 'urlopen', fake_open)
    assert 'Asuncion' in personal.weather('Asuncion')
    assert 'Par de divisas' in personal.search('EURUSD')
    with pytest.raises(ValueError):
        personal.weather(' ')
    with pytest.raises(ValueError):
        personal.schedule_reminder(Memory(), user_id=1, channel_id=1, minutes=0, text='x')
