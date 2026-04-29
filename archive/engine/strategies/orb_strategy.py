
def orb(data):
    if data["price"] > data.get("orb_high", 0):
        return {"direction": "CE", "confidence": 0.7, "symbol": "NIFTY"}
    if data["price"] < data.get("orb_low", 0):
        return {"direction": "PE", "confidence": 0.7, "symbol": "NIFTY"}
    return None
