from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAssetListReq, ProtoOAGetPositionUnrealizedPnLReq, ProtoOAGetTrendbarsReq,
    ProtoOAReconcileReq, ProtoOASpotEvent, ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq, ProtoOASymbolsListReq, ProtoOATraderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOATradeSide, ProtoOATrendbarPeriod,
)
from services.ctrader import client as auth
from services.ctrader.types import (
    AccountSnapshot, CTraderAuthRequired, CTraderUnavailable, OhlcBar, Position,
    SymbolInfo, Tick, spread_bps,
)

PERIODS = {'M1': 1, 'M5': 5, 'M15': 15, 'M30': 30, 'H1': 60, 'H4': 240, 'D1': 1440}
PRICE_SCALE = Decimal('100000')


class LiveCTraderFeed:
    """Read-only cTrader demo Open API adapter. Never places orders."""

    def __init__(self, settings, account_id=None, redis_client=None):
        self.settings = settings
        self.account_id = int(account_id or settings.ctrader_account_id)
        self.redis_client = redis_client

    def _connection(self):
        return auth.open_demo(self.settings, self.redis_client)

    @staticmethod
    def _symbol_map(connection, account_id):
        response = connection.request(ProtoOASymbolsListReq(
            ctidTraderAccountId=account_id, includeArchivedSymbols=False,
        ))
        return {row.symbolName.upper(): row for row in response.symbol if row.symbolName}

    def _find_symbol(self, connection, name):
        row = self._symbol_map(connection, self.account_id).get(name.upper())
        if row is None:
            raise CTraderUnavailable(f'cTrader symbol not found: {name}')
        return row

    def symbols(self) -> list[SymbolInfo]:
        with self._connection() as connection:
            light = list(self._symbol_map(connection, self.account_id).values())
            detail = connection.request(ProtoOASymbolByIdReq(
                ctidTraderAccountId=self.account_id,
                symbolId=[row.symbolId for row in light],
            ))
        details = {row.symbolId: row for row in detail.symbol}
        return [SymbolInfo(
            name=row.symbolName,
            digits=details[row.symbolId].digits if row.symbolId in details else 5,
            pip_position=(details[row.symbolId].pipPosition
                          if row.symbolId in details else None),
            lot_size=(Decimal(details[row.symbolId].lotSize) / 100
                      if row.symbolId in details and details[row.symbolId].lotSize else None),
        ) for row in light]

    def tick(self, symbol: str) -> Tick:
        with self._connection() as connection:
            info = self._find_symbol(connection, symbol)
            connection.request(ProtoOASubscribeSpotsReq(
                ctidTraderAccountId=self.account_id,
                symbolId=[info.symbolId], subscribeToSpotTimestamp=True,
            ))
            event = connection.wait_for(
                ProtoOASpotEvent,
                lambda row: row.symbolId == info.symbolId and row.HasField('bid') and row.HasField('ask'),
            )
        bid = Decimal(event.bid) / PRICE_SCALE
        ask = Decimal(event.ask) / PRICE_SCALE
        as_of = (datetime.fromtimestamp(event.timestamp / 1000, tz=timezone.utc)
                 if event.HasField('timestamp') else datetime.now(timezone.utc))
        return Tick(symbol=info.symbolName, bid=bid, ask=ask,
                    spread_bps=spread_bps(bid, ask), as_of=as_of)

    def ohlc(self, symbol: str, timeframe: str, count: int = 100) -> list[OhlcBar]:
        period_name = timeframe.upper()
        if period_name not in PERIODS:
            raise CTraderUnavailable(f'Unsupported timeframe {timeframe}')
        count = max(1, min(int(count), 1000))
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            info = self._find_symbol(connection, symbol)
            response = connection.request(ProtoOAGetTrendbarsReq(
                ctidTraderAccountId=self.account_id, symbolId=info.symbolId,
                period=ProtoOATrendbarPeriod.Value(period_name),
                fromTimestamp=int((now - timedelta(minutes=PERIODS[period_name] * count * 2)).timestamp() * 1000),
                toTimestamp=int(now.timestamp() * 1000), count=count,
            ))
        bars = []
        for row in response.trendbar[-count:]:
            low = Decimal(row.low) / PRICE_SCALE
            bars.append(OhlcBar(
                symbol=info.symbolName, timeframe=period_name,
                open=low + Decimal(row.deltaOpen) / PRICE_SCALE,
                high=low + Decimal(row.deltaHigh) / PRICE_SCALE, low=low,
                close=low + Decimal(row.deltaClose) / PRICE_SCALE,
                volume=Decimal(row.volume),
                closed_at=datetime.fromtimestamp(row.utcTimestampInMinutes * 60, tz=timezone.utc),
            ))
        return bars

    def _positions(self, connection, names):
        reconcile = connection.request(ProtoOAReconcileReq(ctidTraderAccountId=self.account_id))
        pnl_response = connection.request(ProtoOAGetPositionUnrealizedPnLReq(
            ctidTraderAccountId=self.account_id,
        ))
        pnl_scale = Decimal(10) ** pnl_response.moneyDigits
        pnl = {row.positionId: Decimal(row.netUnrealizedPnL) / pnl_scale
               for row in pnl_response.positionUnrealizedPnL}
        out = []
        for row in reconcile.position:
            trade = row.tradeData
            side = 'buy' if trade.tradeSide == ProtoOATradeSide.BUY else 'sell'
            out.append(Position(
                symbol=names.get(trade.symbolId, str(trade.symbolId)), side=side,
                volume=Decimal(trade.volume) / 100, entry=Decimal(str(row.price)),
                unrealized=pnl.get(row.positionId, Decimal('0')),
                position_id=str(row.positionId),
            ))
        return out, sum((row.unrealized for row in out), Decimal('0'))

    def positions(self) -> list[Position]:
        with self._connection() as connection:
            symbol_rows = self._symbol_map(connection, self.account_id).values()
            names = {row.symbolId: row.symbolName for row in symbol_rows}
            positions, _ = self._positions(connection, names)
            return positions

    def account(self) -> AccountSnapshot:
        with self._connection() as connection:
            trader = connection.request(ProtoOATraderReq(
                ctidTraderAccountId=self.account_id,
            )).trader
            assets = connection.request(ProtoOAAssetListReq(
                ctidTraderAccountId=self.account_id,
            )).asset
            currency = next((row.name for row in assets if row.assetId == trader.depositAssetId), 'USD')
            symbol_rows = self._symbol_map(connection, self.account_id).values()
            names = {row.symbolId: row.symbolName for row in symbol_rows}
            positions, unrealized = self._positions(connection, names)
        digits = trader.moneyDigits if trader.HasField('moneyDigits') else 2
        balance = Decimal(trader.balance) / (Decimal(10) ** digits)
        return AccountSnapshot(
            balance=balance, equity=balance + unrealized, currency=currency[:3].upper(),
            as_of=datetime.now(timezone.utc), positions=tuple(positions),
        )


def feed_from_settings(settings, redis_client=None):
    if not auth.access_token(settings, redis_client):
        raise CTraderAuthRequired('cTrader access token missing; complete OAuth before using market data')
    return LiveCTraderFeed(settings, redis_client=redis_client)
