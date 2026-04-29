
def predict(data):
    prob = data.get("ml_prob", 0.5)
    if prob > 0.6:
        return {"dir": "CE", "confidence": prob}
    elif prob < 0.4:
        return {"dir": "PE", "confidence": 1-prob}
    return None
