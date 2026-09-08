# execution/filters.py
#
# OI-wall detection for entry gating.
# A "wall" = one strike's OI far above the average of the nearby strikes,
# on the side the market would have to chew through for the trade to work.
#
# Tunables (env-driven, defaults preserve the original 2.0x behaviour):
#   OI_WALL_MULTIPLIER   — wall = OI > avg * multiplier   (default 2.0)
#   OI_WALL_MAX_STRIKES  — only strikes within this many strike-steps of
#                          ATM can count as a wall (default 5 = all nearby)

import os

_WALL_MULT     = float(os.getenv("OI_WALL_MULTIPLIER", "2.0"))
_WALL_MAX_STEP = float(os.getenv("OI_WALL_MAX_STRIKES", "5"))


def has_oi_wall(option_chain, atm_strike, direction):

    try:

        if not option_chain:
            return False

        nearby = sorted(
            option_chain,
            key=lambda x: abs(x.get("strike", 0) - atm_strike)
        )[:5]

        avg_ce = sum(s.get("ce_oi", 0) for s in nearby) / max(len(nearby), 1)
        avg_pe = sum(s.get("pe_oi", 0) for s in nearby) / max(len(nearby), 1)

        for s in nearby:

            strike = s.get("strike", 0)
            ce_oi = s.get("ce_oi", 0)
            pe_oi = s.get("pe_oi", 0)

            # A wall 300+ points away is not defending THIS entry — only
            # consider strikes within OI_WALL_MAX_STRIKES steps of ATM.
            if _WALL_MAX_STEP < 5 and abs(strike - atm_strike) > _WALL_MAX_STEP * 100:
                continue

            if direction == "CE":

                if strike > atm_strike and ce_oi > avg_ce * _WALL_MULT:
                    return True

            elif direction == "PE":

                if strike < atm_strike and pe_oi > avg_pe * _WALL_MULT:
                    return True

        return False

    except Exception:
        return False