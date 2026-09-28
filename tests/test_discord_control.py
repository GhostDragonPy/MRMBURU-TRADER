from decimal import Decimal
from datetime import datetime
import json
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

def test_enabled_bot_requires_channel_id():
    with pytest.raises(ValidationError):
        settings(discord_bot_enabled=True, discord_bot_token='t'*40, discord_api_key='d'*40,
                 discord_guild_id=1, discord_admin_role_id=2, discord_allowed_user_ids='10')
    configured=settings(discord_bot_enabled=True, discord_bot_token='t'*40, discord_api_key='d'*40,
                        discord_guild_id=1, discord_admin_role_id=2, discord_channel_id=99,
                        discord_allowed_user_ids='10')
    assert configured.discord_channel_id == 99

class Feed(FakeCTraderFeed):
    def instrument(self, symbol):
        return SymbolInfo(name='EURUSD', digits=5, pip_position=4, lot_size='100000',
            min_volume='1000', max_volume='1000000', step_volume='1000')

@pytest.fixture
def discord_client(factory, monkeypatch):
    monkeypatch.setattr('apps.api.discord.feed_from_settings', lambda *args:Feed())
    configured=settings(discord_api_key=API_KEY)
    with TestClient(create_app(configured,factory,Cache())) as client: yield client

SCOPE=dict(expected_guild_id=1,expected_channel_id=99,admin_role_id=20,allowed_user_ids='10')

@pytest.mark.parametrize('values',[
    dict(guild_id=2,user_id=10,role_ids=[20],channel_id=99),
    dict(guild_id=1,user_id=11,role_ids=[20],channel_id=99),
    dict(guild_id=1,user_id=10,role_ids=[21],channel_id=99),
    dict(guild_id=1,user_id=10,role_ids=[20],channel_id=100)])
def test_unauthorized_guild_user_role_and_channel_are_rejected(values):
    with pytest.raises(AuthorizationError):
        allowed(**values,expected_guild_id=1,expected_channel_id=99,admin_role_id=20,allowed_user_ids='10')

def test_allowlist_user_in_configured_channel_is_allowed():
    assert allowed(guild_id=1,user_id=10,role_ids=[20],channel_id=99,**SCOPE) is True

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
        for path in ('paper-order','paper-close','broker-order','pause','resume'):
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

def test_broker_order_disabled_by_default(discord_client):
    r=discord_client.post('/internal/discord/broker-order',headers=HEADERS,
        json={'interaction_id':'b1','side':'buy','reason':'connectivity check'})
    assert r.status_code==409

def test_broker_order_uses_min_lot_when_enabled(factory, monkeypatch):
    monkeypatch.setattr('services.ctrader.orders.place_min_market',
        lambda *args, **kwargs: {'broker':True,'side':kwargs['side'],'volume':1000})
    configured=settings(discord_api_key=API_KEY, ctrader_broker_orders=True)
    with TestClient(create_app(configured,factory,Cache())) as client:
        r=client.post('/internal/discord/broker-order',headers=HEADERS,
            json={'interaction_id':'b2','side':'buy','reason':'connectivity check'})
    assert r.status_code==200
    assert r.json()['broker'] is True
    assert r.json()['execution_enabled'] is False
    assert r.json()['side']=='buy'

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
    def expire(self, key, ttl):
        assert ttl == 60
    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
    def zrangebyscore(self, key, minimum, maximum):
        return [member for member, score in self.z.get(key, {}).items() if minimum <= score <= maximum]
    def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)
    def eval(self, script, numkeys, *args):
        key, payload = args[0], args[1]
        if self.kv.get(key) != payload:
            return 0
        if 'DEL' in script:
            self.kv.pop(key, None)
            return 1
        return 1


def test_identity_keeps_paper_and_existing_admin_commands():
    payload = identity.identity_payload()
    assert payload['name'] == 'GhostDragon'
    assert payload['execution_enabled'] is False
    assert payload['display_channel'] == '#tradehouse'
    assert 'operator' not in payload
    text = identity.help_text()
    for name in identity.ADMIN_COMMANDS + identity.PERSONAL_COMMANDS:
        assert '/'+name in text
    assert '#tradehouse' in text
    assert 'acfz' not in text


def test_gateway_lock_ttl_owner_and_atomic_release():
    cache = Memory()
    first = gateway_lock.GatewayLock('shared-token', ttl=90)
    assert first.acquire(cache) is True
    second = gateway_lock.GatewayLock('shared-token', ttl=90)
    with pytest.raises(gateway_lock.GatewayLockError, match='Another process'):
        second.acquire(cache)
    assert first.refresh(cache) is True
    assert second.release(cache) is True
    assert cache.get(gateway_lock.LOCK_KEY) == first.payload
    assert first.release(cache) is True
    assert cache.get(gateway_lock.LOCK_KEY) is None


def test_gateway_lock_fails_closed_when_redis_is_down():
    class Broken:
        def set(self, *args, **kwargs):
            raise ConnectionError('redis down')
    lock = gateway_lock.GatewayLock('token')
    with pytest.raises(gateway_lock.GatewayLockError, match='Redis unavailable'):
        lock.acquire(Broken())


def test_usage_report_never_includes_secrets():
    cache = Memory()
    cache.kv['DISCORD_BOT_TOKEN'] = 'secret-value'
    personal.record_usage(cache, 42, 'ayuda')
    personal.record_usage(cache, 42, 'tokens')
    report = personal.usage_report(cache, 42)
    blob = json.dumps(report)
    assert report['usage_count'] == 2
    assert 'command_count' not in report
    assert 'secret-value' not in blob
    assert 'DISCORD' not in blob
    assert '.env' not in blob
    lowered = blob.lower()
    assert 'secret-value' not in blob
    assert 'token' not in lowered
    assert 'password' not in lowered
    assert 'credential' not in lowered
    assert '.env' not in lowered
    assert report['execution_enabled'] is False
    assert report['by_command']['usage'] == 1


def test_personal_reminders_use_asuncion_and_length_limits():
    cache = Memory()
    saved = personal.schedule_reminder(cache, user_id=42, channel_id=99, minutes=1, text='revisar paper')
    assert saved['timezone'] == 'America/Asuncion'
    assert saved['paper_only'] is True
    assert 'America/Asuncion' in saved['due'] or saved['due'].endswith('-03:00') or saved['due'].endswith('-04:00')
    from datetime import timedelta
    due = personal.due_reminders(cache, now=datetime.now(personal.ZONE)+timedelta(minutes=2))
    assert due[0]['text'] == 'revisar paper'
    with pytest.raises(ValueError):
        personal.schedule_reminder(Memory(), user_id=1, channel_id=1, minutes=0, text='x')
    with pytest.raises(ValueError):
        personal.schedule_reminder(Memory(), user_id=1, channel_id=1, minutes=1, text='no')


def test_weather_and_search_reject_urls_and_use_allowlisted_https(monkeypatch):
    calls = []
    def fake_get(host, path, timeout=8, max_bytes=256):
        calls.append((host, path, timeout, max_bytes))
        assert host in {'wttr.in', 'api.duckduckgo.com'}
        assert path.startswith('/')
        if host == 'wttr.in':
            assert timeout == 8 and max_bytes == 256
            return b'Asuncion: 24C'
        return b'{"Heading":"EURUSD","AbstractText":"Par de divisas.","RelatedTopics":[]}'
    monkeypatch.setattr(personal, 'https_get', fake_get)
    assert 'Asuncion' in personal.weather('Asuncion')
    assert 'Par de divisas' in personal.search('EURUSD')
    with pytest.raises(ValueError):
        personal.weather('https://evil.test/x')
    with pytest.raises(ValueError):
        personal.weather('../etc/passwd')
    with pytest.raises(ValueError):
        personal.search('http://127.0.0.1/')
    with pytest.raises(ValueError):
        personal.search('file:///etc/passwd')
    assert all(host in {'wttr.in', 'api.duckduckgo.com'} for host, *_ in calls)


def test_lookups_reject_unknown_hosts():
    from apps.discord_bot import lookups
    with pytest.raises(RuntimeError, match='Host not allowed'):
        lookups.https_get('example.com', '/')
    with pytest.raises(RuntimeError, match='Path not allowed'):
        lookups.https_get('wttr.in', 'http://evil.test')
