# engine/risk/risk_manager.py

def position_size(capital, confidence):
    base = capital * 0.01
    return int(base * confidence)


def compute_entry_stops(entry_premium, atr, regime, delta=0.5):
    """
    Premium-space stops for a LONG option (CE or PE are both BOUGHT).

    A long option loses when its PREMIUM falls, regardless of CE/PE — so the
    stop is always BELOW the entry premium and the target ABOVE it. This is
    side-agnostic and unit-consistent with profit_manager / live LTP.

    `atr` is in SPOT points; it is converted to premium points via `delta`
    (ATM option ≈ 0.5). `entry_premium` is the option premium at entry
    (fill price live, simulated premium in backtest).
    """
    if not atr or atr <= 0:
        atr = max(entry_premium, 1.0) * 0.05

    if regime == "TREND":
        sl_mult, tp_mult = 1.5, 3.0    # was 1.2, 2.2 — give trend trades more room
    elif regime == "EXPANSION":
        sl_mult, tp_mult = 2.0, 3.5    # was 1.5, 2.5
    else:  # RANGE
        sl_mult, tp_mult = 2.0, 3.5    # was 1.0, 1.8 — CRITICAL FIX

    # Convert spot-ATR stop distance into premium points via delta.
    stop_distance = delta * atr * sl_mult
    # Never risk more than the premium itself; floor to a sane minimum.
    stop_distance = max(min(stop_distance, entry_premium * 0.95), entry_premium * 0.05)

    stop_loss = entry_premium - stop_distance
    target    = entry_premium + (stop_distance * tp_mult)
    stop_pct  = stop_distance / max(entry_premium, 1)

    return stop_loss, target, stop_pct