"""Minimum-size market orders for a live connectivity check. Esses stays paper."""
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolByIdReq, ProtoOASymbolsListReq
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType
from services.ctrader.client import open_trading
from services.ctrader.types import CTraderUnavailable


def _summarize(event, *, side, volume, account_id, extra=None):
    error = (event.errorCode or None) if getattr(event, 'errorCode', None) else None
    if error:
        raise CTraderUnavailable(f'cTrader order error: {error}')
    order = event.order if event.HasField('order') else None
    position = event.position if event.HasField('position') else None
    payload = {
        'broker': True,
        'account_id': account_id,
        'symbol': 'EURUSD',
        'side': side,
        'volume': int(volume),
        'execution_type': ProtoOAExecutionType.Name(event.executionType)
            if event.HasField('executionType') else None,
        'order_id': str(order.orderId) if order else None,
        'position_id': str(position.positionId) if position else None,
        'execution_enabled': False,
        'strategy_untouched': True,
    }
    if extra:
        payload.update(extra)
    return payload


def _eurusd(connection):
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
    return light, meta


def place_min_market(settings, redis_client, *, side: str):
    if side not in ('buy', 'sell'):
        raise ValueError('side must be buy or sell')
    with open_trading(settings, redis_client) as connection:
        light, meta = _eurusd(connection)
        event = connection.send_market(symbol_id=light.symbolId, side=side, volume=int(meta.minVolume))
        return _summarize(event, side=side, volume=meta.minVolume,
                          account_id=settings.ctrader_account_id)


def roundtrip_min_market(settings, redis_client, *, side: str = 'buy'):
    if side not in ('buy', 'sell'):
        raise ValueError('side must be buy or sell')
    with open_trading(settings, redis_client) as connection:
        light, meta = _eurusd(connection)
        volume = int(meta.minVolume)
        opened = connection.send_market(symbol_id=light.symbolId, side=side, volume=volume)
        open_summary = _summarize(opened, side=side, volume=volume,
                                  account_id=settings.ctrader_account_id)
        if not open_summary['position_id']:
            raise CTraderUnavailable('Open did not return a position id')
        closed = connection.close_position(
            position_id=int(open_summary['position_id']), volume=volume)
        close_summary = _summarize(closed, side=side, volume=volume,
                                   account_id=settings.ctrader_account_id, extra={'closed': True})
    return {'opened': open_summary, 'closed': close_summary}
