
from engine.models.alpha.ml_model import predict as ml
from engine.models.alpha.orb_model import predict as orb

def select_strategy(data):
    signals = [ml(data), orb(data)]
    signals = [s for s in signals if s]

    if not signals:
        return None

    return max(signals, key=lambda x: x["confidence"])
