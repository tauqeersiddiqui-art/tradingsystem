# engine/risk/risk_manager.py

def position_size(capital, confidence):
    base = capital * 0.01
    return int(base * confidence)


def compute_entry_stops(entry_price, atr, regime):

    if not atr or atr <= 0:
        atr = entry_price * 0.05

    if regime == "TREND":
        sl_mult, tp_mult = 1.5, 3.0    # was 1.2, 2.2 — give trend trades more room
    elif regime == "EXPANSION":
        sl_mult, tp_mult = 2.0, 3.5    # was 1.5, 2.5
    else:  # RANGE
        sl_mult, tp_mult = 2.0, 3.5    # was 1.0, 1.8 — CRITICAL FIX

    stop_distance = atr * sl_mult
    stop_loss = entry_price - stop_distance
    target    = entry_price + (stop_distance * tp_mult)
    stop_pct  = stop_distance / max(entry_price, 1)

    return stop_loss, target, stop_pct