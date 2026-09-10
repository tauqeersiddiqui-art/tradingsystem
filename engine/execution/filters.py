# execution/filters.py
#
# OI-wall detection for entry gating.
# A "wall" = one strike's OI far above the average of the nearby strikes,
# on the side the market would have to chew through for the trade to work.
#
# FIXES (2026-09-06):
#   1. The proximity guard was `if _WALL_MAX_STEP < 5 and abs(strike-atm) >
#      _WALL_MAX_STEP * 100`. Two defects: it silently DISABLED itself whenever
#      OI_WALL_MAX_STRIKES >= 5, and it hardcoded a 100-pt strike grid so the
#      same env value meant different distances on different instruments.
#      It now always applies and uses Config.STRIKE_STEP.
#   2. The candidate window was a hardcoded `[:5]` regardless of
#      OI_WALL_MAX_STRIKES, so the average that defines "far above" was taken
#      over strikes that could never be flagged. The window now follows the
#      setting: 2*max_step+1 strikes centred on ATM.
#   3. The ATM strike itself can never be a wall (strict > / <), which is why
#      feeding this function a phantom ATM mattered: with master_runner's old
#      `round(spot/50)*50`, BANKNIFTY's real ATM (a 100-pt strike) sat ABOVE
#      the phantom midpoint and its natural OI concentration read as an
#      overhead wall. 8 of ~10 signal minutes on 2026-09-04 died here.
#
# Tunables (env):
#   OI_WALL_MULTIPLIER   — wall = OI > window_avg * multiplier   (default 2.0)
#   OI_WALL_MAX_STRIKES  — only strikes within this many strike-steps of
#                          ATM can count as a wall (default 5)

import os

from engine.config.config import STRIKE_STEP

_WALL_MULT     = float(os.getenv("OI_WALL_MULTIPLIER", "2.0"))
_WALL_MAX_STEP = int(float(os.getenv("OI_WALL_MAX_STRIKES", "5")))


def has_oi_wall(option_chain, atm_strike, direction):
    """
    True if a same-direction OI wall sits between price and the target.

    Args:
        option_chain : list of {"strike", "ce_oi", "pe_oi"} dicts
        atm_strike   : ATM strike on the REAL grid (round(spot/STRIKE_STEP)*STRIKE_STEP)
        direction    : "CE" (wall = heavy CE OI above ATM) or
                       "PE" (wall = heavy PE OI below ATM)
    """
    try:
        if not option_chain:
            return False

        max_step  = max(_WALL_MAX_STEP, 1)
        max_dist  = max_step * STRIKE_STEP
        window_n  = 2 * max_step + 1

        # Candidate window = the strikes that could actually be flagged, so the
        # average is computed over the same population as the test.
        nearby = sorted(
            option_chain,
            key=lambda x: abs(x.get("strike", 0) - atm_strike)
        )[:window_n]
        nearby = [
            s for s in nearby
            if abs(s.get("strike", 0) - atm_strike) <= max_dist
        ]
        if not nearby:
            return False

        n = len(nearby)
        avg_ce = sum(s.get("ce_oi", 0) or 0 for s in nearby) / n
        avg_pe = sum(s.get("pe_oi", 0) or 0 for s in nearby) / n

        # No OI data (e.g. quote mode without MODE_FULL) — do not gate on it.
        if direction == "CE" and avg_ce <= 0:
            return False
        if direction == "PE" and avg_pe <= 0:
            return False

        for s in nearby:
            strike = s.get("strike", 0)
            ce_oi  = s.get("ce_oi", 0) or 0
            pe_oi  = s.get("pe_oi", 0) or 0

            if direction == "CE":
                if strike > atm_strike and ce_oi > avg_ce * _WALL_MULT:
                    return True
            elif direction == "PE":
                if strike < atm_strike and pe_oi > avg_pe * _WALL_MULT:
                    return True

        return False

    except Exception:
        return False
