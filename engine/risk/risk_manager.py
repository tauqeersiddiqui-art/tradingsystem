# engine/risk/risk_manager.py
#
# Redesigned for small capital (1-2 lot) profitability:
#   - Hard cap: never risk more than 10 premium pts per trade
#     = ₹650 at 1 lot (65 qty), ₹1300 at 2 lots (130 qty)
#   - 3.5R target gives strong positive expectancy at 55%+ WR
#   - Trailing in profit_manager handles the real exit, not fixed TP
#
# Math at 2 lots, 55% WR, 10-pt SL, 35-pt target:
#   Expected = 0.55 × (35×130) - 0.45 × (10×130)
#            = 0.55 × 4550   - 0.45 × 1300
#            = 2503 - 585 = +Rs1,917 per trade


def position_size(capital, confidence):
    base = capital * 0.01
    return int(base * confidence)


def compute_entry_stops(entry_premium, atr, regime, delta=0.5):
    """
    Tight institutional stops for a LONG option (CE or PE — both bought).

    Key design decisions (capital-first thinking):
    1. Stop capped at 10 premium pts — worst case ₹650 (1 lot) or ₹1300 (2 lots)
    2. 3.5R target is guidance only — trailing stop (profit_manager) is the real exit
    3. Tighter stop = more stops hit, but each loss is small → positive expectancy
    4. Do NOT widen stops to "give the trade room" — that destroys expectancy

    Args:
        entry_premium : fill price (premium points, e.g. 120.5)
        atr           : spot ATR in points (e.g. 45.0 for NIFTY 1m)
        regime        : "TREND" | "EXPANSION" | "RANGE"
        delta         : option delta (~0.5 for ATM)
    """
    if not atr or atr <= 0:
        atr = max(entry_premium, 1.0) * 0.10

    # Raw stop in premium space; tight multiplier (0.7) keeps risk low
    raw_sl_pts = delta * atr * 0.7

    # Hard limits: 3 pts floor (avoids noise stop-outs), 10 pts ceiling (capital protection)
    stop_distance = max(min(raw_sl_pts, 10.0), 3.0)

    stop_loss = entry_premium - stop_distance
    target    = entry_premium + stop_distance * 3.5   # 3.5R — rarely hit; trailing does the work
    stop_pct  = stop_distance / max(entry_premium, 1.0)

    return stop_loss, target, stop_pct
