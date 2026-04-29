
def predict(data):
    if data["price"] > data.get("orb_high", 0):
        return {"dir": "CE", "confidence": 0.7}
    if data["price"] < data.get("orb_low", 0):
        return {"dir": "PE", "confidence": 0.7}
    return None
