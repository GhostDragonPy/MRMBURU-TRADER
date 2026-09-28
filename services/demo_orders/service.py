"""Typed Esses -> DEMO broker path. Never uses Discord HTTP /broker_order."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from core.models import AuditEvent, AutomaticPaperControl, DemoControl, DemoOrderIntent, DemoOwnedPosition, KillSwitch
from services.ctrader.stream import active as esses_window
from services.demo_orders.gateway import DemoCTraderExecutionGateway, UncertainBrokerResult
from services.demo_orders.guards import DemoGuardError, next_rollout, reject_live_identity
from services.demo_orders.preflight import mask_account, public_preflight, require_preflight

D = Decimal
MAX_RISK = D('0.0025')
MAX_DAILY_LOSS = D('0.01')
MAX_TRADES = 2


def signal_id_for(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode()
    return sha256(raw).hexdigest()


def control(session):
    row = session.get(DemoControl, 1)
    if row is None:
        row = DemoControl(id=1, rollout='disabled', blocked=False, protection_failed=False,
                          canary_consumed=False, reason='initial',
                          changed_at=datetime.now(timezone.utc))
        session.add(row)
        session.flush()
    return row


def _block_demo(session, reason):
    row = control(session)
    row.blocked = True
    row.reason = reason
    row.changed_at = datetime.now(timezone.utc)
    session.add(AuditEvent(actor='demo-orders', action='demo.blocked', payload={'reason': reason}))


def _normalize_volume(units, instrument, canary=False):
    step = D(instrument['step_volume'])
    minimum = D(instrument['min_volume'])
    maximum = D(instrument['max_volume'])
    qty = minimum if canary else D(units)
    qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if qty < minimum:
        raise DemoGuardError('volume below minimum')
    if qty > maximum:
        qty = maximum
    return qty


def _signal_born_at(position):
    raw = position.get('bar')
    if not raw:
        raise DemoGuardError('SIGNAL_TIMESTAMP_MISSING')
    born = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    return born


def _freshness(settings, demo, position, now):
    born = _signal_born_at(position)
    if demo.armed_at is None:
        raise DemoGuardError('DEMO_NOT_ARMED')
    armed = demo.armed_at if demo.armed_at.tzinfo else demo.armed_at.replace(tzinfo=timezone.utc)
    if born <= armed:
        raise DemoGuardError('SIGNAL_BEFORE_ARM')
    max_age = int(getattr(settings, 'signal_max_age_seconds', 90) or 90)
    if (now - born).total_seconds() > max_age:
        raise DemoGuardError('SIGNAL_EXPIRED')
    return born


def _dependencies(session, redis_client):
    session.execute(select(KillSwitch).where(KillSwitch.id == 1))
    if redis_client is None:
        return
    try:
        if not redis_client.ping():
            raise DemoGuardError('REDIS_UNAVAILABLE')
    except DemoGuardError:
        raise
    except Exception as exc:
        raise DemoGuardError('REDIS_UNAVAILABLE') from exc


def on_paper_cycle(session, settings, result, *, now, gateway, token_scope, paused=None,
                   redis_client=None):
    if settings.trading_mode != 'demo-orders':
        return {'skipped': 'paper-mode'}
    gate = session.get(KillSwitch, 1)
    demo = control(session)
    paper_pause = session.get(AutomaticPaperControl, 1)
    if paused is None:
        paused = bool(paper_pause and paper_pause.paused)
    events = [e for e in result.get('events', []) if e.get('kind') == 'opened']
    if not events:
        return {'skipped': 'no-open'}
    try:
        reject_live_identity(
            account_id=settings.demo_ctrader_account_id,
            is_live=gateway.transport.is_live,
            host=gateway.transport.host,
            environment=gateway.transport.environment,
            scope=token_scope,
        )
        _dependencies(session, redis_client)
    except DemoGuardError as exc:
        _block_demo(session, str(exc))
        return {'blocked': str(exc)}
    unknown = session.scalars(select(DemoOrderIntent).where(
        DemoOrderIntent.status == 'uncertain')).first()
    if unknown is not None:
        return {'blocked': 'UNKNOWN'}
    if demo.blocked or demo.protection_failed:
        return {'blocked': demo.reason}
    if gate is None or gate.active:
        return {'blocked': 'GLOBAL_KILL_SWITCH'}
    if paused:
        return {'blocked': 'paused'}
    if demo.rollout == 'disabled':
        return {'skipped': 'disabled'}
    if not esses_window(now):
        return {'blocked': 'outside_esses_window'}
    outputs = []
    for event in events:
        outputs.append(_handle_open(session, settings, event, now, gateway, demo, token_scope))
        demo = control(session)
    return {'results': outputs}


def _handle_open(session, settings, event, now, gateway: DemoCTraderExecutionGateway, demo, token_scope):
    position = event['position']
    payload = {
        'account': settings.demo_ctrader_account_id,
        'setup_id': position.get('context', {}).get('setup_id'),
        'side': position['side'],
        'stop': str(position['stop']),
        'target': str(position['target']),
        'bar': position.get('bar'),
        'day': now.date().isoformat(),
    }
    signal_id = signal_id_for(payload)
    existing = session.get(DemoOrderIntent, signal_id)
    if existing is not None:
        return {'signal_id': signal_id, 'duplicate': True, 'status': existing.status}
    intent = DemoOrderIntent(signal_id=signal_id, status='reserved', request=payload, response={})
    session.add(intent)
    try:
        session.flush()
    except IntegrityError:
        session.expunge(intent)
        existing = session.get(DemoOrderIntent, signal_id)
        return {'signal_id': signal_id, 'duplicate': True,
                'status': existing.status if existing else 'reserved'}
    if demo.rollout == 'shadow':
        intent.status = 'shadow'
        intent.response = {'proposed': {
            'symbol': 'EURUSD', 'side': position['side'], 'units': str(position.get('units')),
            'stop_loss': str(position.get('stop')), 'take_profit': str(position.get('target')),
            'entry': str(position.get('entry') or ''), 'reason': position.get('context', {}).get('models'),
        }, 'sent': False}
        session.add(AuditEvent(actor='demo-orders', action='demo.shadow',
                               payload={'signal_id': signal_id}))
        return {'signal_id': signal_id, 'status': 'shadow'}
    try:
        require_preflight(session, settings, now=now)
        _freshness(settings, demo, position, now)
    except DemoGuardError as exc:
        intent.status = 'failed'
        intent.response = {'reason': str(exc)}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': str(exc)}
    if not position.get('stop') or not position.get('target'):
        intent.status = 'failed'
        intent.response = {'reason': 'SL_TP_REQUIRED'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'SL_TP_REQUIRED'}
    risk = D(position.get('risk') or 0)
    try:
        snap = gateway.snapshot()
    except DemoGuardError as exc:
        intent.status = 'failed'
        intent.response = {'reason': str(exc)}
        _block_demo(session, 'BROKER_UNAVAILABLE')
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'BROKER_UNAVAILABLE'}
    equity = D(snap['equity'])
    if risk > equity * MAX_RISK:
        intent.status = 'failed'
        intent.response = {'reason': 'TRADE_RISK_LIMIT'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'TRADE_RISK_LIMIT'}
    if int(snap['open_positions']) >= 1:
        intent.status = 'failed'
        intent.response = {'reason': 'POSITION_LIMIT'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'POSITION_LIMIT'}
    filled_today = session.scalars(select(DemoOrderIntent).where(
        DemoOrderIntent.status.in_(('sent', 'filled')))).all()
    today = now.date().isoformat()
    daily = sum(1 for row in filled_today if row.request.get('day') == today)
    if daily >= MAX_TRADES:
        intent.status = 'failed'
        intent.response = {'reason': 'DAILY_TRADE_LIMIT'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'DAILY_TRADE_LIMIT'}
    day_start = D(snap.get('day_start_balance') or snap['equity'])
    if D(snap['equity']) <= day_start - D(snap.get('initial_balance') or snap['equity']) * MAX_DAILY_LOSS:
        intent.status = 'failed'
        intent.response = {'reason': 'DAILY_LOSS_LIMIT'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'DAILY_LOSS_LIMIT'}
    instrument = snap['instrument']
    canary = demo.rollout == 'canary'
    if canary and (demo.canary_consumed or demo.canary_day == today):
        intent.status = 'failed'
        intent.response = {'reason': 'CANARY_ALREADY_USED'}
        return {'signal_id': signal_id, 'status': 'failed'}
    try:
        volume = _normalize_volume(position['units'], instrument, canary=canary)
    except DemoGuardError as exc:
        intent.status = 'failed'
        intent.response = {'reason': str(exc)}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': str(exc)}
    paper_units = D(position['units'])
    if paper_units > 0 and volume > paper_units:
        scaled = risk * (D(volume) / paper_units)
        if scaled > equity * MAX_RISK:
            intent.status = 'failed'
            intent.response = {'reason': 'VOLUME_EXCEEDS_RISK'}
            return {'signal_id': signal_id, 'status': 'failed', 'reason': 'VOLUME_EXCEEDS_RISK'}
    order = {
        'symbol': 'EURUSD',
        'side': position['side'],
        'volume': str(volume),
        'stop_loss': str(position['stop']),
        'take_profit': str(position['target']),
        'signal_id': signal_id,
        'entry': str(position.get('entry') or ''),
        'reason': position.get('context', {}).get('models'),
    }
    if gate_after_send_killed(session):
        intent.status = 'failed'
        intent.response = {'reason': 'GLOBAL_KILL_SWITCH'}
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'GLOBAL_KILL_SWITCH'}
    try:
        intent.status = 'sent'
        result = gateway.submit_market(order)
    except UncertainBrokerResult:
        intent.status = 'uncertain'
        recovered = gateway.reconcile(signal_id)
        if recovered and recovered.get('position_id'):
            return _fill(session, intent, demo, recovered, today, canary)
        intent.response = {'reason': 'UNKNOWN'}
        _block_demo(session, 'UNKNOWN')
        return {'signal_id': signal_id, 'status': 'uncertain', 'reason': 'UNKNOWN'}
    except DemoGuardError as exc:
        intent.status = 'failed'
        intent.response = {'reason': str(exc)}
        _block_demo(session, str(exc))
        return {'signal_id': signal_id, 'status': 'failed', 'reason': str(exc)}
    if gate_after_send_killed(session):
        intent.response = dict(result, killed_after=True)
    if not result.get('sl_confirmed'):
        demo.protection_failed = True
        demo.blocked = True
        demo.reason = 'SL_UNCONFIRMED'
        _block_demo(session, 'SL_UNCONFIRMED')
        intent.status = 'failed'
        intent.response = dict(result, reason='SL_UNCONFIRMED')
        return {'signal_id': signal_id, 'status': 'failed', 'reason': 'SL_UNCONFIRMED'}
    return _fill(session, intent, demo, result, today, canary)


def gate_after_send_killed(session):
    gate = session.get(KillSwitch, 1)
    return gate is None or gate.active


def _fill(session, intent, demo, result, today, canary):
    intent.status = 'filled'
    intent.broker_order_id = str(result.get('order_id') or '')
    intent.position_id = str(result.get('position_id') or '')
    intent.fill_price = str(result.get('fill_price') or '')
    intent.stop_loss = str(result.get('stop_loss') or '')
    intent.take_profit = str(result.get('take_profit') or '')
    intent.response = {k: result.get(k) for k in
                       ('order_id', 'position_id', 'fill_price', 'stop_loss', 'take_profit', 'status')}
    if intent.position_id:
        session.add(DemoOwnedPosition(position_id=intent.position_id, signal_id=intent.signal_id))
    if canary:
        demo.canary_day = today
        demo.canary_consumed = True
    session.add(AuditEvent(actor='demo-orders', action='demo.filled',
                           payload={'signal_id': intent.signal_id, 'order_id': intent.broker_order_id}))
    return {'signal_id': intent.signal_id, 'status': 'filled', 'order_id': intent.broker_order_id}


def advance_rollout(session, target, *, confirmed, now=None):
    if not confirmed:
        raise DemoGuardError('Administrative confirmation required')
    demo = control(session)
    demo.rollout = next_rollout(demo.rollout, target)
    stamp = now or datetime.now(timezone.utc)
    demo.changed_at = stamp
    demo.reason = 'rollout:' + target
    if target == 'shadow' and demo.armed_at is None:
        demo.armed_at = stamp
    session.add(AuditEvent(actor='admin', action='demo.rollout', payload={'rollout': demo.rollout}))
    return {'rollout': demo.rollout}


def emergency_stop(session, reason):
    demo = control(session)
    demo.blocked = True
    demo.reason = reason
    demo.changed_at = datetime.now(timezone.utc)
    session.add(AuditEvent(actor='discord', action='demo.emergency_stop', payload={'reason': reason}))
    return {'blocked': True, 'rollout': demo.rollout, 'positions_closed': False}


def _news_status(redis_client):
    if redis_client is None:
        return 'unknown'
    try:
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        raw = redis_client.get('esses:news:' + day)
        return 'loaded' if raw else 'missing'
    except Exception:
        return 'unknown'


CANARY_SIGNAL_ID = 'MRMBURU-CANARY-DIAGNOSTIC'


def place_diagnostic_canary(session, settings, gateway, *, now=None):
    """One authorized DEMO diagnostic order. Not an Esses signal."""
    now = now or datetime.now(timezone.utc)
    demo = control(session)
    if settings.trading_mode != 'demo-orders' or not settings.demo_execution_enabled:
        raise DemoGuardError('DEMO execution is not enabled')
    if demo.rollout != 'canary':
        raise DemoGuardError('Diagnostic canary requires rollout=canary')
    if demo.blocked or demo.protection_failed:
        raise DemoGuardError(demo.reason or 'DEMO blocked')
    if demo.canary_consumed:
        raise DemoGuardError('CANARY_ALREADY_USED')
    require_preflight(session, settings, now=now)
    existing = session.get(DemoOrderIntent, CANARY_SIGNAL_ID)
    if existing is not None:
        raise DemoGuardError('CANARY_ALREADY_USED')
    snap = gateway.snapshot()
    instrument = snap['instrument']
    volume = _normalize_volume(instrument['min_volume'], instrument, canary=True)
    quote = getattr(gateway.transport.session, 'quote', None)
    if quote is None:
        raise DemoGuardError('Quote unavailable')
    prices = quote(instrument['symbol_id'])
    ask = D(prices['ask'])
    bid = D(prices['bid'])
    pip_pos = int(instrument.get('pip_position') or 4)
    pip = D(10) ** (-pip_pos)
    stop = ask - (pip * 10)
    target = ask + (pip * 20)
    intent = DemoOrderIntent(
        signal_id=CANARY_SIGNAL_ID, status='sent',
        request={'kind': 'diagnostic-canary', 'symbol': 'EURUSD', 'side': 'buy',
                 'volume': str(volume), 'day': now.date().isoformat()},
        response={})
    session.add(intent)
    session.flush()
    order = {
        'symbol': 'EURUSD', 'side': 'buy', 'volume': str(volume),
        'stop_loss': str(stop), 'take_profit': str(target),
        'signal_id': CANARY_SIGNAL_ID,
        'label': 'MRMBURU-CANARY',
        'client_order_id': 'MRMBURU-CANARY-DIAG',
        'entry': str(ask),
    }
    try:
        result = gateway.submit_market(order)
    except UncertainBrokerResult:
        recovered = gateway.reconcile(CANARY_SIGNAL_ID)
        if not recovered or not recovered.get('position_id'):
            intent.status = 'uncertain'
            intent.response = {'reason': 'UNKNOWN'}
            _block_demo(session, 'UNKNOWN')
            raise DemoGuardError('UNKNOWN')
        result = recovered
    except DemoGuardError as exc:
        intent.status = 'failed'
        intent.response = {'reason': str(exc)}
        _block_demo(session, str(exc))
        raise
    if not result.get('sl_confirmed'):
        demo.protection_failed = True
        _block_demo(session, 'SL_UNCONFIRMED')
        intent.status = 'failed'
        intent.response = dict(result, reason='SL_UNCONFIRMED')
        raise DemoGuardError('SL_UNCONFIRMED')
    filled = _fill(session, intent, demo, result, now.date().isoformat(), canary=True)
    closed = gateway.close_owned(result['position_id'], owned_ids={result['position_id']})
    intent.response = dict(intent.response or {}, closed=True, close=closed)
    return {
        'status': 'closed',
        'order_id': filled.get('order_id'),
        'position_id': str(result.get('position_id') or ''),
        'sl_confirmed': True,
        'tp_confirmed': True,
        'closed': True,
        'kind': 'diagnostic-canary',
        'bid': str(bid),
    }


def status_payload(session, settings, token_scope, *, redis_client=None):
    from services.ctrader.stream import active
    demo = control(session)
    last = session.scalars(select(DemoOrderIntent).order_by(DemoOrderIntent.created_at.desc())).first()
    paused = session.get(AutomaticPaperControl, 1)
    ny_now = datetime.now(timezone.utc)
    transport = 'unconfigured'
    if settings.trading_mode == 'demo-orders' and demo.rollout in ('shadow', 'canary', 'enabled'):
        transport = 'sdk-demo'
    connection = {'state': 'idle', 'host': 'demo.ctraderapi.com', 'auth': False}
    if redis_client is not None:
        raw = redis_client.get('demo:socket')
        if raw:
            import json as json_lib
            try:
                payload = json_lib.loads(raw.decode() if isinstance(raw, bytes) else raw)
                connection = {
                    'state': 'healthy' if payload.get('healthy') else 'down',
                    'host': payload.get('host'),
                    'app_auth': payload.get('app_auth'),
                    'account_auth': payload.get('account_auth'),
                    'trading_permission': payload.get('trading_permission'),
                }
            except Exception:
                connection = {'state': 'invalid'}
    return {
        'trading_mode': settings.trading_mode,
        'rollout': demo.rollout,
        'transport': transport,
        'preflight': public_preflight(session, settings),
        'connection': connection,
        'blocked': demo.blocked,
        'protection_failed': demo.protection_failed,
        'emergency_stop': demo.blocked,
        'demo_account': mask_account(settings.demo_ctrader_account_id),
        'strategy': settings.paper_strategy,
        'ny_window': active(ny_now),
        'paused': bool(paused and paused.paused),
        'last_signal': last.signal_id if last else None,
        'last_order': last.broker_order_id if last else None,
        'block_reason': demo.reason if demo.blocked else None,
        'scope': token_scope,
        'allow_live_trading': False,
        'execution_enabled': False,
        'demo_execution_enabled': bool(settings.demo_execution_enabled),
        'paper_available': True,
        'paper_scheduler_enabled': bool(settings.paper_scheduler_enabled),
        'canary_consumed': bool(demo.canary_consumed),
        'armed_at': demo.armed_at.isoformat() if demo.armed_at else None,
        'host': 'demo.ctraderapi.com',
        'is_live': False,
        'news_status': _news_status(redis_client),
    }
