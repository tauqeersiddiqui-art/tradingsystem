
def detect_regime(data):
    vol = data.get("volatility", 1)
    if vol > 2:
        return "EXPANSION"
    elif vol > 1:
        return "TREND"
    return "RANGE"
