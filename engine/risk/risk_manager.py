# engine/risk/risk_manager.py

def position_size(capital, confidence):
    base = capital * 0.01
    return int(base * confidence)


def compute_entry_stops(entry_price, atr, regime):

    if not atr or atr <= 0:
        atr = entry_price * 0.05

    if regime == "TREND":
        sl_mult, tp_mult = 1.4, 2.8   # slightly wider, controlled

    elif regime == "EXPANSION":
        sl_mult, tp_mult = 1.6, 3.0   # NOT 2.0 (too dangerous)

    else:  # RANGE
        sl_mult, tp_mult = 1.5, 2.5   # balanced (NOT 2.0)

    stop_distance = atr * sl_mult
    stop_loss = entry_price - stop_distance
    target    = entry_price + (stop_distance * tp_mult)
    stop_pct  = stop_distance / max(entry_price, 1)

    return stop_loss, target, stop_pct