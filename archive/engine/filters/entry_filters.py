
def apply_filters(signal, data, regime):
    if not signal:
        return None

    score = 0
    if data.get("spread", 1) < 5:
        score += 1
    if data.get("liquidity", 1) > 0:
        score += 1
    if signal["confidence"] > 0.55:
        score += 1

    return signal if score >= 2 else None
