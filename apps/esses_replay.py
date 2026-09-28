"""Offline replay of observed quotes and closed bars; never calls a broker.

JSONL records: {"tick": Tick JSON, "frames": {"M1": [OhlcBar JSON], ...},
"instrument": SymbolInfo JSON, "news": optional reviewed daily calendar}.
Use only snapshots genuinely available at each quote, with no future bars.
"""
import argparse
import json
from decimal import Decimal
from zoneinfo import ZoneInfo
from core.contracts import RiskPolicy, PropRules
from services.ctrader.types import Tick, SymbolInfo, OhlcBar
from services.strategy_engine.esses import evaluate
from services.pipeline.simulator import advance, initial
from services.pipeline.esses import SelectedSignal
from apps.esses_news import validate


def replay(records, allow_unknown_news=False):
    state = None
    rules=PropRules(timezone='America/New_York')
    policy=RiskPolicy(risk_per_trade='0.0025',max_trades_daily=2,max_positions=1,daily_loss_fraction='0.01')
    previous = None
    for raw in records:
        data=json.loads(raw)
        tick=Tick.model_validate(data['tick']); now=tick.as_of
        if previous is not None and now <= previous:
            raise ValueError('Replay quotes must be chronological and unique')
        previous=now
        frames={tf:[OhlcBar.model_validate(b) for b in rows] for tf,rows in data['frames'].items()}
        # Explicitly reject leaked future context rather than silently ignore it.
        if any(b.closed_at > now for rows in frames.values() for b in rows):
            raise ValueError('Future candle in replay snapshot')
        day=now.astimezone(ZoneInfo(rules.timezone)).date().isoformat()
        if state is None:
            state=initial(Decimal('100000'),now,day)
        news=validate(data['news']) if data.get('news') else None
        if news and news['date'] != day:
            raise ValueError('Replay calendar date mismatch')
        signal,audit=evaluate(frames,now)
        state,events=advance(state,tick=tick,bars=frames.get('M1',[]),
            instrument=SymbolInfo.model_validate(data['instrument']),now=now,policy=policy,
            rules=rules,enabled=True,killed=False,allow_unknown_news=allow_unknown_news,
            strategy=SelectedSignal(signal),timeframe='M1',esses=True,audit=audit,news=news)
        yield {'at':now.isoformat(),'events':events,'state':state,'analysis':audit}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('input')
    p.add_argument('--output',required=True)
    p.add_argument('--allow-unknown-news',action='store_true')
    a=p.parse_args()
    with open(a.input) as source, open(a.output,'x') as target:
        for row in replay(source,a.allow_unknown_news):
            target.write(json.dumps(row)+'\n')
    print('Replay complete. Quote gaps pause entries; results are simulated, not broker fills.')


if __name__=='__main__':
    main()
