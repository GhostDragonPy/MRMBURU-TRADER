from datetime import datetime, timedelta, timezone
from core.models import OpsFailure
from services.ops import failure_log
from tests.test_providers import settings as base_settings

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def test_record_failure_dedupes_within_window(factory):
    with factory.begin() as session:
        a = failure_log.record_failure(
            session, kind='collector', code='stale_bars',
            message='Cached M1 bars went stale', detail={'failures': 1, 'token': 'SECRET'},
            now=NOW)
        b = failure_log.record_failure(
            session, kind='collector', code='stale_bars',
            message='Cached M1 bars went stale again', detail={'failures': 2},
            now=NOW + timedelta(minutes=5))
        assert a == b
        row = session.get(OpsFailure, a)
        assert row.occurrences == 2
        assert row.detail.get('failures') == 2
        assert 'token' not in row.detail
        c = failure_log.record_failure(
            session, kind='collector', code='stale_bars',
            message='later after quiet gap', now=NOW + timedelta(minutes=5 + 16))
        assert c != a
        assert session.get(OpsFailure, c).occurrences == 1


def test_capture_exception_scrubs_secrets(factory):
    with factory.begin() as session:
        rid = failure_log.record_failure(
            session, kind='unit', code='Boom',
            message='token=abc123secret and Bearer xyz sk-abcdefghijklmnop',
            detail={'source': 'test', 'where': 'unit', 'token': 'should-drop'})
        row = session.get(OpsFailure, rid)
        assert '[redacted]' in row.message
        assert 'sk-abcdefghijklmnop' not in row.message
        assert 'token' not in (row.detail or {})
        assert failure_log.scrub_text('Authorization: Bearer supersecret') == 'Authorization: [redacted]'


def test_list_and_resolve_failures(factory):
    with factory.begin() as session:
        failure_log.record_failure(
            session, kind='paper_cycle', code='CTraderUnavailable',
            message='Paper cycle failed', now=NOW)
        rows = failure_log.list_failures(session, unresolved_only=True)
        assert len(rows) == 1
        resolved = failure_log.resolve_failure(session, rows[0]['id'], now=NOW)
        assert resolved['resolved_at'] is not None
        assert failure_log.list_failures(session, unresolved_only=True) == []


def test_ops_failures_api(factory, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from tests.test_api import Cache, settings, ADMIN, RESEARCH

    cfg = settings()
    with factory.begin() as session:
        failure_log.record_failure(
            session, kind='collector', code='budget_exhausted',
            message='budget dead', now=NOW)
    app = create_app(cfg, factory, Cache())
    with TestClient(app) as client:
        denied = client.get('/ops/failures')
        assert denied.status_code in (401, 403, 422)
        ok = client.get('/ops/failures', headers={'X-API-Key': RESEARCH})
        assert ok.status_code == 200
        body = ok.json()
        assert body['failures']
        assert body['failures'][0]['code'] == 'budget_exhausted'
        fid = body['failures'][0]['id']
        resolved = client.post(f'/ops/failures/{fid}/resolve', headers={'X-API-Key': ADMIN})
        assert resolved.status_code == 200
        assert resolved.json()['resolved_at']
