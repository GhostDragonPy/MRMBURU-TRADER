"""Explicit research interpretation of the supplied Esses transcripts, not a replica.

Every pivot has two closed candles to its right. No future bars or discretionary
changes of timeframe/range. Parameters and omissions are documented in ESSES_V1.md.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo
from core.contracts import Signal

NY = ZoneInfo('America/New_York')


@dataclass(frozen=True)
class Config:
    pivot_width: int = 2
    sweep_expiry: int = 5  # M1 bars
    min_rr: D = D('1.5')
    stop_buffer: D = D('0.0001')
    min_gap: D = D('0.00002')
    models: tuple = ('IFVG', 'CISD', 'BOS_FVG')


def pivots(bars, width=2):
    out = []
    for i in range(width, len(bars)-width):
        neighbours = bars[i-width:i] + bars[i+1:i+width+1]
        if all(bars[i].high > b.high for b in neighbours):
            out.append((i, 'high', bars[i].high, i+width))
        if all(bars[i].low < b.low for b in neighbours):
            out.append((i, 'low', bars[i].low, i+width))
    return out


def structure(bars, width=2):
    direction = None
    points = pivots(bars, width)
    for j in range(1, len(bars)):
        for kind, side in (('high', 'buy'), ('low', 'sell')):
            known = [p for p in points if p[1] == kind and p[3] < j]
            if not known:
                continue
            level = known[-1][2]
            crossed = bars[j-1].close <= level < bars[j].close if side == 'buy' else bars[j-1].close >= level > bars[j].close
            if crossed:
                direction = side
    return direction


def zones(bars, min_gap=D('0.00002')):
    """(formation index, direction, lower, upper, type). OB body at BOS origin."""
    out = []
    points = pivots(bars)
    for i in range(2, len(bars)):
        a, c = bars[i-2], bars[i]
        if c.low-a.high >= min_gap:
            out.append((i, 'buy', a.high, c.low, 'FVG'))
        if a.low-c.high >= min_gap:
            out.append((i, 'sell', c.high, a.low, 'FVG'))
        for side, kind in (('buy', 'high'), ('sell', 'low')):
            known = [p for p in points if p[1] == kind and p[3] < i]
            if not known:
                continue
            level = known[-1][2]
            crossed = bars[i-1].close <= level < c.close if side == 'buy' else bars[i-1].close >= level > c.close
            if not crossed:
                continue
            for k in range(i-1, max(-1, i-11), -1):
                b = bars[k]
                opposite = b.close < b.open if side == 'buy' else b.close > b.open
                if opposite:
                    lo, hi = sorted((b.open, b.close))
                    # A return after departure and before confirmation consumes it.
                    if not any(x.low <= hi and x.high >= lo for x in bars[k+2:i]):
                        out.append((i, side, lo, hi, 'OB'))
                    break
    return out


def cisd(bars, sweep_index, side):
    k = sweep_index
    # Sweep candle may itself be the opposite-colour reversal.
    directional = lambda b: b.close < b.open if side == 'buy' else b.close > b.open
    if not directional(bars[k]):
        k -= 1
    if k < 0 or not directional(bars[k]):
        return None
    while k > 0 and directional(bars[k-1]):
        k -= 1
    return bars[k].open


def evaluate(frames, now, config=Config()):
    """Return (Signal|None, audit). No live orders, I/O or broker calls."""
    def no(reason, **context):
        return None, dict(reason=reason, **context)
    for tf in ('D1', 'H4', 'H1', 'M15', 'M5', 'M1'):
        rows = frames.get(tf, [])
        if len(rows) < (3 if tf == 'D1' else 25):
            return no('INSUFFICIENT_HISTORY', timeframe=tf)
        if any(b.closed_at.tzinfo is None or b.closed_at > now or b.symbol != 'EURUSD' or b.timeframe != tf
               or b.low > min(b.open, b.close) or b.high < max(b.open, b.close) for b in rows):
            return no('INVALID_HISTORY', timeframe=tf)
        if any(a.closed_at >= b.closed_at for a, b in zip(rows, rows[1:])):
            return no('INVALID_HISTORY', timeframe=tf)
        minutes = {'D1':1440, 'H4':240, 'H1':60, 'M15':15, 'M5':5, 'M1':1}[tf]
        max_age = 4*86400 if tf == 'D1' else minutes*60+120
        if (now-rows[-1].closed_at).total_seconds() > max_age:
            return no('STALE_HISTORY', timeframe=tf)
    local = now.astimezone(NY)
    if local.weekday() >= 5 or not 570 <= local.hour*60+local.minute < 660:
        return no('OUTSIDE_NY_SESSION')
    bars = frames['M1']
    if any((b.closed_at-a.closed_at).total_seconds() != 60 for a,b in zip(bars[-21:], bars[-20:])):
        return no('M1_GAP')
    side = structure(frames['H1'], config.pivot_width)
    if side is None or structure(frames['M15'], config.pivot_width) != side:
        return no('BIAS_UNCLEAR')
    # Only levels known before the sweep qualify. Fresh M15 swings and previous day.
    wanted = 'low' if side == 'buy' else 'high'
    m15 = frames['M15']
    levels = [(p[2], m15[p[3]].closed_at) for p in pivots(m15) if p[1] == wanted]
    daily = frames['D1'][-1]
    levels.append((daily.low if side == 'buy' else daily.high, daily.closed_at))
    sweep = None
    for i in range(max(1, len(bars)-config.sweep_expiry-1), len(bars)):
        b = bars[i]
        for level, known_at in reversed(levels):
            if known_at > b.closed_at-timedelta(minutes=1):
                continue
            earlier = [x for x in bars[:i] if x.closed_at > known_at]
            if any(x.low < level if side == 'buy' else x.high > level for x in earlier):
                continue
            if (b.low < level < b.close if side == 'buy' else b.high > level > b.close):
                sweep = (i, level)
                break
    if sweep is None:
        return no('NO_FRESH_SWEEP', bias=side)
    i, level = sweep
    sb = bars[i]
    poi = None
    for tf in ('H1', 'M15'):
        history = [b for b in frames[tf] if b.closed_at <= sb.closed_at-timedelta(minutes=1)]
        for formed, direction, lo, hi, kind in reversed(zones(history, config.min_gap)):
            if direction != side or sb.high < lo or sb.low > hi:
                continue
            # First touch only, counted on M1 where available plus closed HTF bars.
            touched = any(b.low <= hi and b.high >= lo for b in history[formed+1:])
            touched |= any(b.low <= hi and b.high >= lo for b in bars[:i] if b.closed_at > history[formed].closed_at)
            if not touched:
                poi = dict(timeframe=tf, kind=kind, low=str(lo), high=str(hi), formed=history[formed].closed_at.isoformat())
                break
        if poi:
            break
    if not poi:
        return no('NO_FRESH_POI', bias=side)
    current, previous = bars[-1], bars[-2]
    crosses = lambda ref: previous.close <= ref < current.close if side == 'buy' else previous.close >= ref > current.close
    models = []
    # Opposing gap from the approach to this sweep; the latest close must invert it.
    anchor = cisd(bars, i, side)
    start = max(0, i-10)
    candidates = [z for z in zones(bars[:i+1], config.min_gap)
        if z[0] >= start and z[1] != side and z[4] == 'FVG'
        and not any(b.close > z[3] if side == 'buy' else b.close < z[2] for b in bars[z[0]+1:-1])]
    if len(candidates) == 1 and crosses(candidates[0][3] if side == 'buy' else candidates[0][2]):
        models.append('IFVG')
    if anchor is not None and crosses(anchor):
        models.append('CISD')
    # BOS then later FVG retest, never break and retrospective fill on same bar.
    ps = [p for p in pivots(bars[:i+1]) if p[1] == ('high' if side == 'buy' else 'low')]
    if ps:
        ref = ps[-1][2]
        breaks = [j for j in range(i, len(bars)-1)
            if (bars[j-1].close <= ref < bars[j].close if side == 'buy' else bars[j-1].close >= ref > bars[j].close)]
        if breaks:
            for z in zones(bars[:-1], config.min_gap):
                f, direction, lo, hi, kind = z
                if kind == 'FVG' and direction == side and i <= f <= breaks[-1]:
                    if current.low <= hi and current.high >= lo and (current.close > hi if side == 'buy' else current.close < lo):
                        if not any(b.low <= hi and b.high >= lo for b in bars[f+1:-1]):
                            models.append('BOS_FVG')
                            break
    models = [m for m in models if m in config.models]
    if not models:
        return no('WAITING_CONFIRMATION', bias=side, poi=poi)
    entry = current.close
    stop = min(b.low for b in bars[i:])-config.stop_buffer if side == 'buy' else max(b.high for b in bars[i:])+config.stop_buffer
    targets = []
    for p in pivots(m15):
        idx, kind, price, confirmed = p
        if kind != ('high' if side == 'buy' else 'low'):
            continue
        if not (price > entry if side == 'buy' else price < entry):
            continue
        if any(b.high >= price if side == 'buy' else b.low <= price for b in m15[confirmed+1:]):
            continue
        if any(b.high >= price if side == 'buy' else b.low <= price for b in bars if b.closed_at > m15[confirmed].closed_at):
            continue
        targets.append(price)
    if not targets:
        return no('NO_LIQUIDITY_TARGET', bias=side)
    target = min(targets) if side == 'buy' else max(targets)
    if abs(target-entry)/abs(entry-stop) < config.min_rr:
        return no('RR_BELOW_MINIMUM', bias=side)
    # A fresh opposing M15 FVG before the target is an obstacle, not permission
    # to assume price will pass through it.
    for f, direction, lo, hi, kind in zones(m15, config.min_gap):
        if direction == side or kind != 'FVG':
            continue
        if any(b.low <= hi and b.high >= lo for b in m15[f+1:]):
            continue
        if (entry < lo < target if side == 'buy' else target < hi < entry):
            return no('OPPOSING_FVG_BEFORE_TARGET')
    context = dict(bias=side, h4=structure(frames['H4']), models=models, poi=poi,
        sweep_level=str(level), sweep_at=sb.closed_at.isoformat(),
        setup_id=side+':'+sb.closed_at.isoformat()+':'+str(level),
        cisd_level=str(anchor) if anchor is not None else None,
        min_rr=str(config.min_rr), last_closed_bar=current.closed_at.isoformat())
    return Signal(symbol='EURUSD', side=side, entry=entry, stop_loss=stop,
        take_profit=target, quantity=D(1), value_per_price_unit=D(1), timeframe='M1',
        strategy_version='esses-research:1', created_at=now,
        reasons=tuple(['HTF_ALIGNMENT', 'SWEEP', 'POI']+models), context=context), context
