# engine/risk/risk_manager.py
#
# Premium-space stop/target geometry for a LONG option (CE or PE — both bought).
#
# WHY THIS WAS RETUNED (2026-09-06):
#   The old geometry was `stop = clamp(0.5*ATR*0.9, 4, 10)` and `target = 3.5R`.
#   With BANKNIFTY spot ATR 20-60, `0.5*ATR*0.9` is 9-27, so the 10-pt ceiling
#   was hit on essentially every trade: the stop was a FIXED 10 premium points
#   and the target a fixed +35.  At delta ~0.5 that asks for +70 SPOT points
#   inside the 5-minute MAX_HOLD_SECONDS clock.
#
#   Measured on 17,480 BANKNIFTY 1m bars: +35/-10 premium with a 5-minute
#   timeout resolves win 5.4% / loss 50.7% / timeout 43.9%.  Breakeven WR for
#   1:3.5 after Rs66 round-trip cost is 27.1%, so the geometry lost money
#   structurally — independent of model quality.  The 3.5R target was hit ZERO
#   times in a full replay that ran the real profit_manager ladder alongside it;
#   every profitable exit came from the ladder, never from the target.
#
#   Replay with session guards, the 4-trade/day cap and the 300 s re-entry
#   cooldown (per trade EV):
#       stop mult 0.9 / cap 10 / thr 0.40  ->  n=43   EV -25.4   total -1,093
#       stop mult 1.5 / cap 20 / thr 0.55  ->  n=39   EV +85.5   total +3,336
#   The wider stop only pays WITH the higher threshold (mult 1.5 at thr 0.45
#   measured EV -78.5), so both must move together — see CHAMPION_THRESHOLD.
#
#   Both numbers are env-driven now so they can be re-swept without a code edit:
#       SL_ATR_MULT     stop = delta * spot_atr * SL_ATR_MULT   (default 1.5)
#       SL_MAX_PTS      premium stop ceiling                    (default 20.0)
#       SL_MIN_PTS      premium stop floor                      (default 4.0)
#       TARGET_R        target = stop * TARGET_R                (default 3.5)
#
# Worst case per trade at the new ceiling: 20 premium pts x 30 qty = Rs600
# for 1 lot (was Rs300).  MAX_LOTS_PER_TRADE and MAX_RISK_PER_TRADE_PCT still
# bound total exposure, and the allocator now sizes off this real stop distance.

import os

_SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.5"))
_SL_MAX_PTS  = float(os.getenv("SL_MAX_PTS",  "20.0"))
_SL_MIN_PTS  = float(os.getenv("SL_MIN_PTS",  "4.0"))
_TARGET_R    = float(os.getenv("TARGET_R",    "3.5"))


def position_size(capital, confidence):
    base = capital * 0.01
    return int(base * confidence)


def compute_entry_stops(entry_premium, atr, regime, delta=0.5):
    """
    Returns (stop_loss, target, stop_pct) — ALL in PREMIUM points.

    Args:
        entry_premium : option fill price in premium points (e.g. 120.5)
        atr           : SPOT ATR in index points (e.g. 45.0 for BANKNIFTY 1m).
                        Converted to premium space via `delta`.
        regime        : "TREND" | "EXPANSION" | "RANGE" (reserved; the ladder
                        in profit_manager does the regime-sensitive work)
        delta         : option delta (~0.5 ATM)

    IMPORTANT: `atr` must be the SPOT ATR. Passing a premium-space value here
    double-counts delta and halves the stop.
    """
    if not atr or atr <= 0:
        # No ATR available: fall back to a fraction of premium, not zero.
        atr = max(entry_premium, 1.0) * 0.10

    raw_sl_pts    = delta * atr * _SL_ATR_MULT
    stop_distance = max(min(raw_sl_pts, _SL_MAX_PTS), _SL_MIN_PTS)

    stop_loss = entry_premium - stop_distance
    target    = entry_premium + stop_distance * _TARGET_R
    stop_pct  = stop_distance / max(entry_premium, 1.0)

    return stop_loss, target, stop_pct


def premium_stop_pts(atr, delta=0.5):
    """
    The stop distance `compute_entry_stops` WOULD use, without needing a fill
    price. Lets the allocator size on the same premium risk the risk manager
    will actually place, instead of re-deriving it from spot ATR.
    """
    if not atr or atr <= 0:
        return _SL_MIN_PTS
    return max(min(delta * atr * _SL_ATR_MULT, _SL_MAX_PTS), _SL_MIN_PTS)
