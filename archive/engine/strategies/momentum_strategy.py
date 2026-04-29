
def momentum(data):
    if data.get("momentum", 0) > 0:
        return {"direction": "CE", "confidence": 0.6, "symbol": "NIFTY"}
    if data.get("momentum", 0) < 0:
        return {"direction": "PE", "confidence": 0.6, "symbol": "NIFTY"}
    return None
