"""Central gate for cTrader DEMO trading payloads. Not a scattered worker check."""
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAmendPositionSLTPReq, ProtoOAClosePositionReq, ProtoOANewOrderReq,
)
from services.demo_orders.guards import DemoGuardError

NEW_ORDER = ProtoOANewOrderReq().payloadType
CLOSE_POSITION = ProtoOAClosePositionReq().payloadType
AMEND_SLTP = ProtoOAAmendPositionSLTPReq().payloadType
TRADING_PAYLOADS = frozenset({NEW_ORDER, CLOSE_POSITION, AMEND_SLTP})
REDIS_KEY = 'demo:barrier:new_orders'


class TradingMessageBarrier:
    def __init__(self, *, profile, demo_execution_enabled=False, trading_permission='UNVERIFIED',
                 redis_client=None, canary_consumed=False):
        if profile not in ('probe', 'disabled', 'shadow', 'canary', 'enabled'):
            raise DemoGuardError('Invalid barrier profile')
        self.profile = profile
        self.demo_execution_enabled = bool(demo_execution_enabled)
        self.trading_permission = trading_permission
        self.redis = redis_client
        self._canary_consumed = bool(canary_consumed)
        self.denied = []
        self.allowed_trading = []

    def _persistent_consumed(self):
        if self._canary_consumed:
            return True
        if self.redis is None:
            return False
        try:
            return bool(self.redis.get(REDIS_KEY))
        except Exception as exc:
            raise DemoGuardError('REDIS_UNAVAILABLE') from exc

    def _mark_consumed(self):
        self._canary_consumed = True
        if self.redis is not None:
            self.redis.set(REDIS_KEY, '1')

    def authorize(self, payload_type):
        if payload_type not in TRADING_PAYLOADS:
            return True
        if self.profile in ('probe', 'disabled', 'shadow') or not self.demo_execution_enabled:
            self.denied.append(payload_type)
            raise DemoGuardError('TRADING_MESSAGE_BLOCKED')
        if self.trading_permission != 'VERIFIED':
            self.denied.append(payload_type)
            raise DemoGuardError('TRADING_PERMISSION_UNVERIFIED')
        if self.profile == 'canary':
            if payload_type != NEW_ORDER:
                self.denied.append(payload_type)
                raise DemoGuardError('TRADING_MESSAGE_BLOCKED')
            if self._persistent_consumed():
                self.denied.append(payload_type)
                raise DemoGuardError('CANARY_ORDER_CAP')
            self._mark_consumed()
        self.allowed_trading.append(payload_type)
        return True

    @classmethod
    def for_rollout(cls, settings, demo, *, redis_client=None, trading_permission='UNVERIFIED'):
        rollout = getattr(demo, 'rollout', 'disabled') if demo is not None else 'disabled'
        return cls(
            profile=rollout if settings.trading_mode == 'demo-orders' else 'disabled',
            demo_execution_enabled=bool(getattr(settings, 'demo_execution_enabled', False)),
            trading_permission=trading_permission,
            redis_client=redis_client,
            canary_consumed=bool(getattr(demo, 'canary_consumed', False)) if demo is not None else False,
        )
