from datetime import datetime, timezone
from decimal import Decimal
from hmac import compare_digest
from fastapi import Depends, Header, HTTPException
from pydantic import Field
from sqlalchemy import select
from core.contracts import Contract, Positive
from core.models import Account, AuditEvent, AutomaticPaperControl, KillSwitch, PaperLedger
from services.ctrader.feed import feed_from_settings
from services.pipeline import discord_sandbox

class DiscordOrder(Contract):
    interaction_id: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern='^(buy|sell)$')
    stop_loss: Positive
    take_profit: Positive
    risk_percent: Decimal = Field(gt=0, le=Decimal('0.25'), allow_inf_nan=False)
    reason: str = Field(min_length=3, max_length=512)

class DiscordBrokerOrder(Contract):
    interaction_id: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern='^(buy|sell)$')
    reason: str = Field(min_length=3, max_length=512)

class DiscordClose(Contract):
    interaction_id: str = Field(min_length=1, max_length=32)
    position_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=512)

class DiscordControl(Contract):
    interaction_id: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=3, max_length=512)

def mount(app, *, settings, factory, redis_client, db):
    def authorized(x_discord_api_key: str = Header(default=''),
                   x_discord_user_id: str = Header(default='')):
        secret = settings.discord_api_key
        if secret is None or not compare_digest(x_discord_api_key, secret.get_secret_value()):
            raise HTTPException(401, 'Discord service authentication required')
        if not x_discord_user_id.isdigit():
            raise HTTPException(400, 'Discord user ID required')
        return x_discord_user_id

    def cached(key):
        value = redis_client.get(key)
        return value.decode() if isinstance(value, bytes) else value

    def audit_read(session, user, action):
        session.add(AuditEvent(actor='discord:'+user, action='discord.'+action,
                               payload={'execution_enabled':False}))

    @app.get('/internal/discord/status')
    def status(user=Depends(authorized), session=Depends(db)):
        from services.ctrader.stream import prefix
        gate = session.get(KillSwitch, 1)
        control = session.get(AutomaticPaperControl, 1)
        ledger = session.get(PaperLedger, settings.paper_account_id) if settings.paper_account_id else None
        audit_read(session, user, 'status')
        return {'mode':'paper','execution_enabled':False,'strategy':settings.paper_strategy,
            'automatic_paused':bool(control and control.paused),
            'kill_switch':gate is None or gate.active,
            'collector':cached(prefix(settings)+':status'),
            'last_success':cached('paper:last_success'),
            'last_error':cached('paper:last_error'),
            'automatic_position_open':bool(ledger and ledger.state.get('position'))}

    @app.get('/internal/discord/positions')
    def open_positions(user=Depends(authorized), session=Depends(db)):
        result = discord_sandbox.positions(session)
        audit_read(session, user, 'positions')
        return {'account':discord_sandbox.ACCOUNT_NAME,'positions':result,'execution_enabled':False}

    @app.get('/internal/discord/history')
    def history(user=Depends(authorized), session=Depends(db)):
        result = discord_sandbox.positions(session, include_closed=True, limit=20)
        audit_read(session, user, 'history')
        return {'account':discord_sandbox.ACCOUNT_NAME,'trades':result,'execution_enabled':False}

    @app.get('/internal/discord/daily-report')
    def daily_report(user=Depends(authorized), session=Depends(db)):
        result = discord_sandbox.report(session)
        audit_read(session, user, 'daily_report')
        return result

    @app.post('/internal/discord/paper-order')
    def paper_order(body:DiscordOrder, user=Depends(authorized), session=Depends(db)):
        try:
            return discord_sandbox.open_position(session, feed_from_settings(settings, redis_client),
                interaction_id=body.interaction_id, user_id=user, side=body.side,
                stop_loss=body.stop_loss, take_profit=body.take_profit,
                risk_fraction=body.risk_percent/Decimal(100), reason=body.reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/internal/discord/broker-order')
    def broker_order(body:DiscordBrokerOrder, user=Depends(authorized), session=Depends(db)):
        from services.ctrader.orders import place_min_market
        from services.ctrader.types import CTraderAuthRequired, CTraderUnavailable
        def execute():
            result = place_min_market(settings, redis_client, side=body.side)
            result['reason'] = body.reason
            result['user_id'] = user
            return result
        try:
            return discord_sandbox.idempotent(session, interaction_id=body.interaction_id,
                user_id=user, action='broker-order',
                payload={'side':body.side,'reason':body.reason}, operation=execute)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except CTraderAuthRequired as exc:
            raise HTTPException(401, str(exc)) from None
        except CTraderUnavailable as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/internal/discord/paper-close')
    def paper_close(body:DiscordClose, user=Depends(authorized), session=Depends(db)):
        try:
            return discord_sandbox.close_position(session, feed_from_settings(settings, redis_client),
                interaction_id=body.interaction_id, user_id=user,
                position_id=body.position_id, reason=body.reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def control(body, user, session, paused):
        payload = {'paused':paused,'reason':body.reason}
        def execute():
            if settings.paper_account_id:
                session.scalar(select(Account).where(
                    Account.id == settings.paper_account_id).with_for_update())
            row = session.scalar(select(AutomaticPaperControl).where(
                AutomaticPaperControl.id == 1).with_for_update())
            if row is None:
                row = AutomaticPaperControl(id=1, paused=paused, reason=body.reason)
                session.add(row)
            else:
                row.paused, row.reason, row.changed_at = paused, body.reason, datetime.now(timezone.utc)
            return {'automatic_paused':paused,'paper_only':True}
        return discord_sandbox.idempotent(session, interaction_id=body.interaction_id,
            user_id=user, action='pause' if paused else 'resume', payload=payload, operation=execute)

    @app.post('/internal/discord/pause')
    def pause(body:DiscordControl, user=Depends(authorized), session=Depends(db)):
        return control(body, user, session, True)

    @app.get('/internal/discord/demo-status')
    def demo_status(user=Depends(authorized), session=Depends(db)):
        from services.ctrader import tokens as token_store
        from services.demo_orders.service import status_payload
        audit_read(session, user, 'demo_status')
        payload = status_payload(session, settings, token_store.effective_scope(redis_client))
        payload['allow_live_trading'] = False
        return payload

    @app.post('/internal/discord/demo-emergency-stop')
    def demo_emergency_stop(body:DiscordControl, user=Depends(authorized), session=Depends(db)):
        from services.demo_orders.service import emergency_stop
        def execute():
            return emergency_stop(session, body.reason)
        return discord_sandbox.idempotent(session, interaction_id=body.interaction_id,
            user_id=user, action='demo-emergency-stop',
            payload={'reason':body.reason}, operation=execute)

    class DiscordRollout(DiscordControl):
        pass

    @app.post('/internal/discord/demo-rollout')
    def demo_rollout(body:dict, user=Depends(authorized), session=Depends(db)):
        from services.demo_orders.guards import DemoGuardError
        from services.demo_orders.service import advance_rollout
        target = body.get('target')
        confirmed = body.get('confirmed') is True
        interaction_id = body.get('interaction_id','')
        reason = body.get('reason','rollout')
        if target == 'live' or body.get('allow_live_trading'):
            raise HTTPException(403, 'LIVE trading cannot be enabled')
        def execute():
            try:
                return advance_rollout(session, target, confirmed=confirmed)
            except DemoGuardError as exc:
                raise HTTPException(409, str(exc)) from None
        return discord_sandbox.idempotent(session, interaction_id=interaction_id,
            user_id=user, action='demo-rollout',
            payload={'target':target,'reason':reason}, operation=execute)

    @app.post('/internal/discord/resume')
    def resume(body:DiscordControl, user=Depends(authorized), session=Depends(db)):
        return control(body, user, session, False)

