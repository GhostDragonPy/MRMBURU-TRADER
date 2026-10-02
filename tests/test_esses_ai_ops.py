from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch
import pytest
from core.contracts import Signal
from services.ai_engine import deepseek
from services.ops import alerts
from services.pipeline.esses import apply_ai_filter
from tests.test_providers import settings as base_settings

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _signal():
    return Signal(
        symbol='EURUSD', side='buy', entry=Decimal('1.112'), stop_loss=Decimal('1.1039'),
        take_profit=Decimal('1.14'), quantity=Decimal('1000'), value_per_price_unit=Decimal('1'),
        timeframe='M1', strategy_version='esses-research:1', created_at=NOW,
        reasons=('CISD',), context={'setup_id': 'setup-a', 'models': ['CISD'], 'min_rr': '1.5'},
    )


def test_review_esses_setup_approve_and_reject(monkeypatch):
    cfg = base_settings(deepseek_api_key='sk-test')

    def fake_request(url, **kw):
        assert 'chat/completions' in url
        approved = '"approved": true' in kw['payload']['messages'][1]['content'] or True
        # Force from model response body we craft:
        return {'choices': [{'message': {'content': '{"approved": true, "confidence": "high", "rationale": "ok"}'}}],
                'usage': {'total_tokens': 10}}

    monkeypatch.setattr(deepseek, 'request_json', fake_request)
    out = deepseek.review_esses_setup(cfg, signal=_signal(), audit={'reason': 'ok'}, quote={})
    assert out['approved'] is True
    assert out['confidence'] == 'high'
    assert out['skipped'] is False

    def reject_request(url, **kw):
        return {'choices': [{'message': {'content': '{"approved": false, "confidence": "low", "rationale": "late"}'}}]}

    monkeypatch.setattr(deepseek, 'request_json', reject_request)
    out = deepseek.review_esses_setup(cfg, signal=_signal())
    assert out['approved'] is False


def test_apply_ai_filter_blocks_on_reject_and_fail_opens_on_outage(monkeypatch):
    cfg = base_settings(deepseek_api_key='sk-test')
    signal = _signal()

    monkeypatch.setattr(deepseek, 'review_esses_setup', lambda *a, **k: {
        'provider': 'deepseek', 'approved': False, 'confidenceale': 'no', 'skipped': False})
    filtered, audit, events = apply_ai_filter(cfg, signal, {}, Mock())
    assert filtered is None
    assert events[0]['reasons'] == ['AI_REJECTED']
    assert audit['ai_filter']['approved'] is False

    def boom(*a, **k):
        raise deepseek.DeepSeekUnavailable('down')

    monkeypatch.setattr(deepseek, 'review_esses_setup', boom)
    filtered, audit, events = apply_ai_filter(cfg, signal, {}, Mock(), cache=Mock())
    assert filtered is signal
    assert audit['ai_filter']['skipped'] is True
    assert events == []


def test_ops_alert_dedupes_and_posts(monkeypatch):
    cfg = base_settings(
        deepseek_api_key='sk-test',
        discord_bot_enabled=True,
        discord_bot_token='b' * 40,
        discord_api_key='d' * 40,
        discord_guild_id=1,
        discord_admin_role_id=2,
        discord_channel_id=3,
        discord_allowed_user_ids='9',
    )
    store = {}
    queue = []
    cache = Mock()
    cache.get.side_effect = lambda k: store.get(k)
    cache.set.side_effect = lambda k, v, ex=None: store.__setitem__(k, v)
    cache.rpush.side_effect = lambda k, v: queue.append(v) or 1
    cache.ltrim.return_value = True
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw['payload']['content']))
        return {}

    monkeypatch.setattr(alerts, 'request_json', fake_request)
    assert alerts.notify(cfg, cache, kind='trader_health', message='budget dead') is True
    assert alerts.notify(cfg, cache, kind='trader_health', message='budget dead') is False
    assert len(calls) == 1
    assert 'trader_health' in calls[0][1]


def test_health_digest_only_on_change_or_after_cooldown(monkeypatch):
    cfg = base_settings(
        deepseek_api_key='sk-test',
        discord_bot_enabled=True,
        discord_bot_token='b' * 40,
        discord_api_key='d' * 40,
        discord_guild_id=1,
        discord_admin_role_id=2,
        discord_channel_id=3,
        discord_allowed_user_ids='9',
    )
    store = {}
    cache = Mock()
    cache.get.side_effect = lambda k: store.get(k)
    cache.set.side_effect = lambda k, v, ex=None: store.__setitem__(k, v)
    cache.delete.side_effect = lambda k: store.pop(k, None)
    sent = []

    def fake_notify(*a, **kw):
        sent.append(kw.get('message', ''))
        store[alerts.ALERT_PREFIX + kw['kind']] = '1'
        return True

    monkeypatch.setattr(alerts, 'notify', fake_notify)
    monkeypatch.setattr(alerts, 'collect_health_issues', lambda s, c: [
        ('paper_error', 'CTraderUnavailable'),
    ])
    assert alerts.watch_worker_health(cfg, cache) == ['trader_health']
    assert len(sent) == 1
    # Cooldown active — any flap stays silent.
    assert alerts.watch_worker_health(cfg, cache) == []
    monkeypatch.setattr(alerts, 'collect_health_issues', lambda s, c: [
        ('collector_status', 'status=stale_bars'),
    ])
    assert alerts.watch_worker_health(cfg, cache) == []
    monkeypatch.setattr(alerts, 'collect_health_issues', lambda s, c: [
        ('collector_budget', 'budget=1000'),
        ('prop_sim_preflight', 'missing'),
    ])
    assert alerts.watch_worker_health(cfg, cache) == []
    assert len(sent) == 1
    # After cooldown expires, one reminder is allowed.
    store.pop(alerts.ALERT_PREFIX + alerts.HEALTH_DIGEST_KIND)
    assert alerts.watch_worker_health(cfg, cache) == ['trader_health']
    assert len(sent) == 2


def test_collect_health_decodes_redis_bytes():
    cfg = base_settings(
        trading_mode='prop-sim',
        prop_sim_execution_enabled=True,
        prop_sim_acknowledged_live_environment=True,
        prop_sim_ctrader_account_id='48803059',
        prop_sim_trader_login='17204978',
        esses_broker_execution=True,
        ctrader_environment='live',
        ctrader_account_id='48803059',
        discord_bot_enabled=True,
        discord_bot_token='b' * 40,
        discord_api_key='d' * 40,
        discord_guild_id=1,
        discord_admin_role_id=2,
        discord_channel_id=3,
        discord_allowed_user_ids='9',
    )
    cache = Mock()
    cache.get.side_effect = lambda k: {
        'ctrader:stream:v1:live:48803059:status': b'stale_bars',
        'paper:last_error': b'CTraderUnavailable',
        'prop-sim:preflight:ok': b'1',
    }.get(k)
    cache.zcard.return_value = 10
    issues = alerts.collect_health_issues(cfg, cache)
    assert issues == [('collector_status', 'status=stale_bars')]
    assert not any(c == 'prop_sim_preflight' for c, _ in issues)


def test_ops_alert_queues_when_rest_blocked(monkeypatch):
    cfg = base_settings(
        deepseek_api_key='sk-test',
        discord_bot_enabled=True,
        discord_bot_token='b' * 40,
        discord_api_key='d' * 40,
        discord_guild_id=1,
        discord_admin_role_id=2,
        discord_channel_id=3,
        discord_allowed_user_ids='9',
    )
    store = {}
    queue = []
    cache = Mock()
    cache.get.side_effect = lambda k: store.get(k)
    cache.set.side_effect = lambda k, v, ex=None: store.__setitem__(k, v)
    cache.rpush.side_effect = lambda k, v: queue.append(v) or len(queue)
    cache.ltrim.return_value = True
    cache.lpop.side_effect = lambda k: queue.pop(0) if queue else None

    def boom(*a, **k):
        from services.providers.http import ProviderError
        raise ProviderError('403 blocked')

    monkeypatch.setattr(alerts, 'request_json', boom)
    assert alerts.notify(cfg, cache, kind='collector_budget', message='budget dead') is True
    assert len(queue) == 1
    due = alerts.due_alerts(cache)
    assert due[0]['kind'] == 'collector_budget'
