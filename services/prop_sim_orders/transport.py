"""Official cTrader prop-sim protobuf transport on live.ctraderapi.com."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from threading import Lock
from time import sleep

from services.ctrader.prop_sim import PROP_SIM_BROKER, PROP_SIM_CTID, PROP_SIM_TRADER_LOGIN
from services.demo_orders.guards import DEMO_HOST, DemoGuardError
from services.demo_orders.transport import LABEL, client_order_id, sanitize
from services.prop_sim_orders.guards import PROP_SIM_HOST, require_prop_sim_identity, validate_prop_sim_runtime

D = Decimal


def normalize_volume(units, instrument):
    step = D(str(instrument['step_volume']))
    minimum = D(str(instrument['min_volume']))
    maximum = D(str(instrument['max_volume']))
    qty = (D(str(units)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if qty < minimum:
        raise DemoGuardError('volume below minimum')
    if qty > maximum:
        qty = maximum
    return int(qty)


class UnconfiguredPropSimTransport:
    def __init__(self, settings):
        self.account_id = settings.prop_sim_ctrader_account_id
        self.trader_login = settings.prop_sim_trader_login
        self.broker = PROP_SIM_BROKER
        self.is_live = True
        self.host = PROP_SIM_HOST
        self.environment = 'live'
        self.scope = 'trading'
        self.name = 'unconfigured'

    def snapshot(self):
        raise DemoGuardError('PROP SIM transport is not activated')

    def submit_market(self, order):
        raise DemoGuardError('PROP SIM transport is not activated')

    def reconcile(self, signal_id):
        return None

    def close_position(self, position_id):
        raise DemoGuardError('PROP SIM transport is not activated')

    def close(self):
        return None


class OfficialPropSimTransport:
    """SDK protobuf session bound to live.ctraderapi.com for the FTMO Free Trial tuple."""

    def __init__(self, settings, *, session, token_scope='trading', budget=None):
        validate_prop_sim_runtime(settings)
        self.settings = settings
        self.session = session
        self.scope = token_scope
        self.budget = budget
        self.account_id = int(PROP_SIM_CTID)
        self.trader_login = PROP_SIM_TRADER_LOGIN
        self.broker = PROP_SIM_BROKER
        self.host = getattr(session, 'host', PROP_SIM_HOST)
        self.environment = getattr(session, 'environment', 'live')
        self.is_live = True
        self._ready = False
        self._locks = {}
        self._lock_table = Lock()
        self._results = {}
        self._instrument = None
        self._sent_new_orders = 0
        self._pause = sleep
        require_prop_sim_identity(
            account_id=self.account_id, trader_login=self.trader_login, broker=self.broker,
            is_live=True, host=self.host, environment=self.environment, scope=self.scope,
        )
        if self.host == DEMO_HOST or 'demo' in str(self.host).lower():
            raise DemoGuardError('LIVE host required for prop-sim')
        if getattr(settings, 'allow_live_trading', False) is not False:
            raise DemoGuardError('LIVE trading flag rejected')
        if getattr(settings, 'execution_enabled', False) is not False:
            raise DemoGuardError('EXECUTION_ENABLED must remain false')

    def _gate_lock(self, key):
        with self._lock_table:
            lock = self._locks.get(key)
            if lock is None:
                lock = Lock()
                self._locks[key] = lock
            return lock

    def connect(self):
        if self.budget is None:
            raise DemoGuardError('RequestBudget is required')
        listed = self.session.authenticate(
            client_id=self.settings.ctrader_client_id.get_secret_value()
            if getattr(self.settings, 'ctrader_client_id', None) else 'unused',
            client_secret=self.settings.ctrader_client_secret.get_secret_value()
            if getattr(self.settings, 'ctrader_client_secret', None) else 'unused',
            access_token='[injected]',
            account_id=self.account_id,
            scope=self.scope,
        )
        accounts = listed.get('accounts') or []
        if listed.get('permission_scope') != 'trading':
            raise DemoGuardError('Token scope accounts rejected')
        match = next((row for row in accounts if int(row['ctidTraderAccountId']) == self.account_id), None)
        if match is None:
            raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
        if match.get('isLive') is not True:
            raise DemoGuardError('PROP_SIM_TUPLE_MISMATCH')
        other_live = [
            row for row in accounts
            if row.get('isLive') is True and str(row.get('ctidTraderAccountId')) != PROP_SIM_CTID
        ]
        if other_live:
            raise DemoGuardError('OTHER_LIVE_ACCOUNT_BLOCKED')
        self.is_live = True
        self._ready = True
        return listed

    def _ensure(self):
        if self.budget is None:
            raise DemoGuardError('RequestBudget is required')
        if getattr(self.session, 'account_auth', False):
            self._ready = True
            return
        if not self._ready:
            self.connect()

    def snapshot(self):
        self._ensure()
        snap = self.session.snapshot(self.account_id)
        instrument = snap.get('instrument') or self._load_instrument()
        self._instrument = instrument
        return {
            'balance': str(snap.get('balance', '0')),
            'equity': str(snap.get('equity', '0')),
            'open_positions': int(snap.get('open_positions', 0)),
            'open_orders': int(snap.get('open_orders', 0)),
            'day_start_balance': str(snap.get('day_start_balance', snap.get('balance', '0'))),
            'initial_balance': str(snap.get('initial_balance', snap.get('balance', '0'))),
            'instrument': instrument,
        }

    def _load_instrument(self):
        meta = self.session.symbol('EURUSD', self.account_id)
        self._instrument = {
            'symbol_id': int(meta['symbol_id']),
            'min_volume': str(meta['min_volume']),
            'max_volume': str(meta['max_volume']),
            'step_volume': str(meta['step_volume']),
            'lot_size': str(meta.get('lot_size') or meta['min_volume']),
            'digits': int(meta.get('digits') or 5),
            'pip_position': int(meta.get('pip_position') or 4),
        }
        return self._instrument

    def submit_market(self, order):
        self._ensure()
        if order.get('symbol') != 'EURUSD':
            raise DemoGuardError('Only EURUSD is allowed')
        if not order.get('stop_loss') or not order.get('take_profit'):
            raise DemoGuardError('SL/TP required')
        cid = client_order_id(order['signal_id'])
        with self._gate_lock(cid):
            if cid in self._results:
                return self._results[cid]
            recovered = self.session.find_by_client_order_id(self.account_id, cid)
            if recovered:
                result = self._confirmed(recovered, order, cid)
                self._results[cid] = result
                return result
            instrument = self._instrument or self._load_instrument()
            volume = normalize_volume(order['volume'], instrument)
            label = str(order.get('label') or LABEL)
            if not label.startswith(LABEL):
                raise DemoGuardError('Refusing unlabeled DEMO order')
            request = {
                'symbol_id': instrument['symbol_id'],
                'side': order['side'],
                'volume': volume,
                'stop_loss': str(order['stop_loss']),
                'take_profit': str(order['take_profit']),
                'label': label[:50],
                'client_order_id': str(order.get('client_order_id') or cid)[:50],
                'comment': label[:50],
            }
            try:
                self._sent_new_orders += 1
                raw = self.session.new_order(self.account_id, request)
            except TimeoutError:
                recovered = self.session.find_by_client_order_id(self.account_id, cid)
                if recovered:
                    result = self._confirmed(recovered, order, cid)
                    self._results[cid] = result
                    return result
                raise
            result = self._confirmed(raw, order, cid)
            self._results[cid] = result
            return result

    def _confirmed(self, raw, order, cid):
        payload = sanitize({
            'order_id': str(raw.get('order_id') or ''),
            'position_id': str(raw.get('position_id') or ''),
            'fill_price': str(raw.get('fill_price') or ''),
            'stop_loss': str(raw.get('stop_loss') or order['stop_loss']),
            'take_profit': str(raw.get('take_profit') or order['take_profit']),
            'status': raw.get('status') or 'ORDER_FILLED',
            'client_order_id': cid,
            'label': raw.get('label') or LABEL,
        })
        sl_ok = bool(raw.get('sl_confirmed')) and bool(payload['stop_loss'])
        tp_ok = bool(raw.get('tp_confirmed', True)) and bool(payload['take_profit'])
        if payload['position_id'] and not sl_ok:
            sl_ok, payload = self._ensure_protection(payload)
            tp_ok = sl_ok and bool(payload.get('take_profit'))
        payload['sl_confirmed'] = sl_ok and tp_ok
        payload['tp_confirmed'] = tp_ok
        return payload

    def _ensure_protection(self, payload):
        """Reconcile before the single allowed Amend. Never send a second NewOrder."""
        live = None
        for attempt in range(3):
            live = self.session.position(self.account_id, payload['position_id'])
            if live and live.get('stop_loss') and live.get('take_profit'):
                payload['stop_loss'] = str(live['stop_loss'])
                payload['take_profit'] = str(live['take_profit'])
                return True, payload
            if live:
                break
            if attempt < 2:
                self._pause(0.4)
        try:
            amended = self.session.amend_sl_tp(
                self.account_id, payload['position_id'], payload['stop_loss'], payload['take_profit'])
        except (DemoGuardError, TimeoutError):
            live = self.session.position(self.account_id, payload['position_id'])
            if live and live.get('stop_loss') and live.get('take_profit'):
                payload['stop_loss'] = str(live['stop_loss'])
                payload['take_profit'] = str(live['take_profit'])
                return True, payload
            raise
        ok = bool(amended.get('sl_confirmed'))
        payload['stop_loss'] = str(amended.get('stop_loss') or payload['stop_loss'])
        payload['take_profit'] = str(amended.get('take_profit') or payload['take_profit'])
        return ok, payload

    def reconcile(self, signal_id):
        self._ensure()
        cid = client_order_id(signal_id)
        found = self.session.find_by_client_order_id(self.account_id, cid)
        if not found:
            return None
        return sanitize({
            'order_id': str(found.get('order_id') or ''),
            'position_id': str(found.get('position_id') or ''),
            'fill_price': str(found.get('fill_price') or ''),
            'stop_loss': str(found.get('stop_loss') or ''),
            'take_profit': str(found.get('take_profit') or ''),
            'sl_confirmed': bool(found.get('sl_confirmed')),
            'status': found.get('status') or 'ORDER_FILLED',
            'client_order_id': cid,
        })

    def close_position(self, position_id):
        self._ensure()
        owned = self.session.position(self.account_id, position_id)
        if owned is None or not str(owned.get('label') or '').startswith(LABEL):
            raise DemoGuardError('Refusing to manage a foreign position')
        closed = self.session.close_position(self.account_id, position_id, owned.get('volume'))
        return sanitize({'closed': str(position_id), 'status': closed.get('status')})

    def close(self):
        self.session.close()
        self._ready = False
