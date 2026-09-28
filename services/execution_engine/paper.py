from datetime import datetime, timezone
from decimal import Decimal
from services.execution_engine.gateway import ExecutionDisabled, ExecutionGateway
from services.ctrader.types import Tick


class PaperFill:
    def __init__(self, signal, tick: Tick, fill_price: Decimal):
        self.signal = signal
        self.tick = tick
        self.fill_price = fill_price
        self.filled_at = datetime.now(timezone.utc)
        self.broker = False
        self.venue = 'paper'


def fill_paper(signal, tick: Tick, *, slippage: Decimal = Decimal(0)) -> PaperFill:
    """Simulate a fill from cTrader bid/ask. Never sends an order to the broker."""
    ExecutionGateway().assert_no_live()
    price = tick.ask if signal.side == 'buy' else tick.bid
    direction = Decimal(1) if signal.side == 'buy' else Decimal(-1)
    return PaperFill(signal, tick, Decimal(price) + direction*slippage)

def close_paper(side: str, tick: Tick) -> Decimal:
    """Return the executable paper exit side without contacting a broker."""
    ExecutionGateway().assert_no_live()
    if side == 'buy':
        return Decimal(tick.bid)
    if side == 'sell':
        return Decimal(tick.ask)
    raise ValueError('Invalid paper position side')
