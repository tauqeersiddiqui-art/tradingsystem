
def ml(data):
    p = data.get("ml_prob", 0.5)
    if p > 0.6:
        return {"direction": "CE", "confidence": p, "symbol": "NIFTY"}
    if p < 0.4:
        return {"direction": "PE", "confidence": 1-p, "symbol": "NIFTY"}
    return None
