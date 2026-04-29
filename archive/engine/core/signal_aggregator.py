
from engine.strategies.orb_strategy import orb
from engine.strategies.ml_strategy import ml
from engine.strategies.momentum_strategy import momentum

def aggregate(data, regime):
    signals = []

    for fn in [orb, ml, momentum]:
        s = fn(data)
        if s:
            signals.append(s)

    if not signals:
        return None

    best = max(signals, key=lambda x: x["confidence"])
    return best
