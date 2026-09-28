"""Minimum-size market orders for a live connectivity check. Esses stays paper."""
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolByIdReq, ProtoOASymbolsListReq
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType
from services.ctrader.client import open_trading
from services.ctrader.types import CTraderUnavailable


def place_min_market(settings, redis_client, *, side: str):
    if side not in ('buy', 'sell'):
        raise ValueError('side must be buy or sell')
    with open_trading(settings, redis_client) as connection:
        listed = connection.request(ProtoOASymbolsListReq(
            ctidTraderAccountId=connection.account_id, includeArchivedSymbols=False,
        ))
        light = next((row for row in listed.symbol if row.symbolName == 'EURUSD'), None)
        if light is None:
            raise CTraderUnavailable('EURUSD is not available on this account')
        detail = connection.request(ProtoOASymbolByIdReq(
            ctidTraderAccountId=connection.account_id, symbolId=[light.symbolId],
        ))
        meta = next((row for row in detail.symbol if row.symbolId == light.symbolId), None)
        if meta is None or not meta.minVolume:
            raise CTraderUnavailable('EURUSD min volume unavailable')
        event = connection.send_market(symbol_id=light.symbolId, side=side, volume=int(meta.minVolume))
    error = (event.errorCode or None) if getattr(event, 'errorCode', None) else None
    if error:
        raise CTraderUnavailable(f'cTrader order error: {error}')
    order = event.order if event.HasField('order') else None
    position = event.position if event.HasField('position') else None
    exec_type = None
    if event.HasField('executionType'):
        exec_type = ProtoOAExecutionType.Name(event.executionType)
    return {
        'broker': True,
        'account_id': settings.ctrader_account_id,
        'symbol': 'EURUSD',
        'side': side,
        'volume': int(meta.minVolume),
        'execution_type': exec_type,
        'order_id': str(order.orderId) if order else None,
        'position_id': str(position.positionId) if position else None,
        'execution_enabled': False,
        'strategy_untouched': True,
    }
