from datetime import datetime, timezone
from decimal import Decimal
from services.ctrader import client as auth
from services.ctrader.types import (
    AccountSnapshot, CTraderAuthRequired, CTraderUnavailable, OhlcBar, Position,
    SymbolInfo, Tick, spread_bps,
)

PERIODS = {
    'M1': 'M1', 'M5': 'M5', 'M15': 'M15', 'M30': 'M30',
    'H1': 'H1', 'H4': 'H4', 'D1': 'D1',
}


class LiveCTraderFeed:
    """Read-only cTrader Open API adapter. Never places orders."""

    def __init__(self, settings, account_id=None, redis_client=None):
        self.settings = settings
        self.account_id = account_id or settings.ctrader_account_id
        self.redis_client = redis_client

    def _get(self, path, params=None):
        return auth.api_get(self.settings, path, params, redis_client=self.redis_client)

    def _require_account(self):
        if self.account_id:
            return self.account_id
        rows = self._get('/tradingaccounts')
        items = rows if isinstance(rows, list) else rows.get('data') or rows.get('accounts') or []
        if not items:
            raise CTraderUnavailable('No cTrader trading accounts returned')
        first = items[0]
        self.account_id = str(first.get('accountId') or first.get('id') or first.get('accountNumber'))
        return self.account_id

    def symbols(self) -> list[SymbolInfo]:
        account = self._require_account()
        rows = self._get(f'/tradingaccounts/{account}/symbols')
        items = rows if isinstance(rows, list) else rows.get('data') or rows.get('symbols') or []
        out = []
        for row in items:
            name = row.get('symbolName') or row.get('name') or row.get('symbol')
            if not name:
                continue
            out.append(SymbolInfo(
                name=name,
                digits=int(row.get('digits') or 5),
                pip_position=row.get('pipPosition'),
                lot_size=str(row.get('lotSize') or row.get('minVolume') or '1'),
            ))
        return out

    def tick(self, symbol: str) -> Tick:
        account = self._require_account()
        row = self._get(f'/tradingaccounts/{account}/symbols/{symbol}/tick')
        data = row.get('data', row)
        bid = Decimal(str(data.get('bid') or data.get('bestBid') or data.get('bidPrice')))
        ask = Decimal(str(data.get('ask') or data.get('bestAsk') or data.get('askPrice')))
        return Tick(symbol=symbol, bid=bid, ask=ask, spread_bps=spread_bps(bid, ask),
                    as_of=datetime.now(timezone.utc))

    def ohlc(self, symbol: str, timeframe: str, count: int = 100) -> list[OhlcBar]:
        account = self._require_account()
        period = PERIODS.get(timeframe.upper())
        if period is None:
            raise CTraderUnavailable(f'Unsupported timeframe {timeframe}')
        rows = self._get(f'/tradingaccounts/{account}/symbols/{symbol}/trendbars',
                            {'period': period, 'count': count})
        items = rows if isinstance(rows, list) else rows.get('data') or rows.get('trendbars') or []
        bars = []
        for row in items[-count:]:
            closed = row.get('timestamp') or row.get('utcTimestamp') or row.get('time')
            if isinstance(closed, (int, float)):
                closed_at = datetime.fromtimestamp(float(closed)/ (1000 if closed > 10**12 else 1), tz=timezone.utc)
            else:
                closed_at = datetime.fromisoformat(str(closed).replace('Z', '+00:00'))
            bars.append(OhlcBar(
                symbol=symbol, timeframe=timeframe,
                open=str(row.get('open')), high=str(row.get('high')),
                low=str(row.get('low')), close=str(row.get('close')),
                volume=str(row.get('volume') or 0), closed_at=closed_at,
            ))
        return bars

    def positions(self) -> list[Position]:
        account = self._require_account()
        rows = self._get(f'/tradingaccounts/{account}/positions')
        items = rows if isinstance(rows, list) else rows.get('data') or rows.get('positions') or []
        out = []
        for row in items:
            side = (row.get('tradeSide') or row.get('side') or 'buy').lower()
            if side in {'buy', 'long'}:
                side = 'buy'
            elif side in {'sell', 'short'}:
                side = 'sell'
            else:
                continue
            out.append(Position(
                symbol=row.get('symbolName') or row.get('symbol'),
                side=side,
                volume=str(row.get('volume') or row.get('lotSize') or 1),
                entry=str(row.get('entryPrice') or row.get('price') or 0),
                unrealized=Decimal(str(row.get('unrealizedPnL') or row.get('swap') or 0)),
                position_id=str(row.get('positionId') or row.get('id') or ''),
            ))
        return out

    def account(self) -> AccountSnapshot:
        account = self._require_account()
        row = self._get(f'/tradingaccounts/{account}')
        data = row.get('data', row)
        now = datetime.now(timezone.utc)
        return AccountSnapshot(
            balance=str(data.get('balance') or data.get('deposit') or 0),
            equity=str(data.get('equity') or data.get('balance') or 0),
            currency=(data.get('depositCurrency') or data.get('currency') or 'USD')[:3].upper(),
            as_of=now,
            positions=tuple(self.positions()),
        )


def feed_from_settings(settings, redis_client=None):
    if not auth.access_token(settings, redis_client):
        raise CTraderAuthRequired('cTrader access token missing; complete OAuth before using market data')
    return LiveCTraderFeed(settings, redis_client=redis_client)
