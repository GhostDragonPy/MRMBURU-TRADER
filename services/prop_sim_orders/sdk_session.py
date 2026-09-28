"""Prop-sim SDK factory. Uses the dedicated prop-sim OAuth token and LIVE host."""
from services.ctrader.prop_sim import PROP_SIM_CTID
from services.ctrader.types import CTraderAuthRequired
from services.demo_orders.barrier import TradingMessageBarrier
from services.demo_orders.guards import LIVE_HOST, DemoGuardError
from services.demo_orders.sdk_session import SdkDemoSession, TlsProtobufDriver


class SdkPropSimSessionFactory:
    def open(self, settings, *, barrier, budget=None, driver=None, redis_client=None):
        from services.ctrader.client import configured
        from services.ctrader import tokens as token_store
        if not configured(settings):
            raise CTraderAuthRequired('cTrader client id/secret are not set')
        token = token_store.load_prop_sim_access_token(redis_client) if redis_client is not None else None
        if not token:
            raise CTraderAuthRequired('Authorize prop-sim first (no access token)')
        market = token_store.load_access_token(redis_client) if redis_client is not None else None
        if market and market == token:
            raise DemoGuardError('PROP_SIM_TOKEN_NOT_INDEPENDENT')
        if driver is None:
            driver = TlsProtobufDriver(host=LIVE_HOST, allow_live=True)
        session = SdkDemoSession(
            account_id=PROP_SIM_CTID,
            client_id=settings.ctrader_client_id.get_secret_value(),
            client_secret=settings.ctrader_client_secret.get_secret_value(),
            access_token=token,
            barrier=barrier,
            budget=budget,
            driver=driver,
            identity='prop-sim',
        )
        session.authenticate()
        session.barrier.trading_permission = session.trading_permission
        return session

    def open_probe(self, settings, redis_client=None, *, driver=None, budget=None):
        barrier = TradingMessageBarrier(profile='probe', demo_execution_enabled=False)
        barrier.redis_key = 'prop-sim:barrier:new_orders'
        return self.open(settings, barrier=barrier, budget=budget, driver=driver,
                         redis_client=redis_client)
