"""Durable, deduped ops failure log. Never stores secrets or raw tokens."""
import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.models import OpsFailure

DEDUPE_SECONDS = 900  # merge identical kind+code within 15 minutes
SAFE_DETAIL_KEYS = frozenset({
    'status', 'budget', 'failures', 'label', 'exc_type', 'trading_mode',
    'paper_strategy', 'reason', 'approved', 'confidence', 'model', 'source',
    'where', 'mode', 'component', 'thread', 'rollout',
})
_SECRET_RE = re.compile(
    r'(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)'
)


def _utcnow():
    return datetime.now(timezone.utc)


def scrub_text(value, *, limit=500):
    text = '' if value is None else str(value)
    text = _SECRET_RE.sub('[redacted]', text)
    return text[:limit]


def _clean_detail(detail):
    if not isinstance(detail, dict):
        return {}
    out = {}
    for key, value in detail.items():
        if key not in SAFE_DETAIL_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, (int, float, bool)):
            out[str(key)[:64]] = value
        else:
            out[str(key)[:64]] = scrub_text(value, limit=200)
    return out


def _normalize(kind, code, message):
    kind = str(kind or 'ops')[:64]
    code = str(code or 'unknown')[:64]
    message = scrub_text(message or code, limit=500)
    return kind, code, message


def record_failure(session, *, kind, code, message, detail=None, now=None,
                   dedupe_seconds=DEDUPE_SECONDS):
    """Insert or bump an unresolved ops failure. Returns the row id."""
    now = now or _utcnow()
    kind, code, message = _normalize(kind, code, message)
    detail = _clean_detail(detail or {})
    since = now - timedelta(seconds=int(dedupe_seconds))
    row = session.scalar(
        select(OpsFailure)
        .where(
            OpsFailure.kind == kind,
            OpsFailure.code == code,
            OpsFailure.resolved_at.is_(None),
            OpsFailure.last_seen_at >= since,
        )
        .order_by(OpsFailure.last_seen_at.desc())
        .limit(1)
    )
    if row is None:
        row = OpsFailure(
            kind=kind, code=code, message=message, detail=detail,
            occurrences=1, first_seen_at=now, last_seen_at=now,
        )
        session.add(row)
        session.flush()
        return row.id
    row.occurrences = int(row.occurrences or 0) + 1
    row.last_seen_at = now
    row.message = message
    merged = dict(row.detail or {})
    merged.update(detail)
    row.detail = merged
    session.flush()
    return row.id


def record_failure_standalone(*, kind, code, message, detail=None):
    """Open a short DB session and record. Never raises to callers."""
    try:
        from core.database import session_factory
        factory = session_factory()
        with factory.begin() as session:
            return record_failure(
                session, kind=kind, code=code, message=message, detail=detail)
    except Exception:
        logging.error('Ops failure log write failed: %s/%s', kind, code, exc_info=False)
        return None


def capture_exception(kind, exc, *, detail=None, code=None, message=None):
    """Record any exception safely. Never raises to callers."""
    if exc is None:
        return None
    detail = dict(detail or {})
    detail.setdefault('exc_type', type(exc).__name__)
    detail.setdefault('where', kind)
    return record_failure_standalone(
        kind=kind,
        code=code or type(exc).__name__,
        message=message or scrub_text(exc, limit=300),
        detail=detail,
    )


def install_process_hooks(kind_prefix='worker'):
    """Capture uncaught exceptions in the main thread and worker threads."""
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            capture_exception(
                f'{kind_prefix}.uncaught',
                exc if isinstance(exc, BaseException) else exc_type('unknown'),
                detail={'source': 'sys.excepthook', 'thread': 'main'},
            )
        except Exception:
            pass
        return previous(exc_type, exc, tb)

    sys.excepthook = _hook

    if hasattr(threading, 'excepthook'):
        previous_thread = threading.excepthook

        def _thread_hook(args):
            try:
                capture_exception(
                    f'{kind_prefix}.thread',
                    args.exc_value,
                    detail={
                        'source': 'threading.excepthook',
                        'thread': getattr(args.thread, 'name', 'unknown'),
                    },
                )
            except Exception:
                pass
            return previous_thread(args)

        threading.excepthook = _thread_hook


def list_failures(session, *, limit=50, unresolved_only=True):
    limit = max(1, min(int(limit), 200))
    stmt = select(OpsFailure).order_by(OpsFailure.last_seen_at.desc()).limit(limit)
    if unresolved_only:
        stmt = stmt.where(OpsFailure.resolved_at.is_(None))
    rows = session.scalars(stmt).all()
    return [public_failure(row) for row in rows]


def public_failure(row):
    return {
        'id': row.id,
        'kind': row.kind,
        'code': row.code,
        'message': row.message,
        'detail': row.detail or {},
        'occurrences': row.occurrences,
        'first_seen_at': row.first_seen_at.isoformat() if row.first_seen_at else None,
        'last_seen_at': row.last_seen_at.isoformat() if row.last_seen_at else None,
        'resolved_at': row.resolved_at.isoformat() if row.resolved_at else None,
    }


def resolve_failure(session, failure_id, *, now=None):
    now = now or _utcnow()
    row = session.get(OpsFailure, failure_id)
    if row is None:
        return None
    if row.resolved_at is None:
        row.resolved_at = now
    session.flush()
    return public_failure(row)
