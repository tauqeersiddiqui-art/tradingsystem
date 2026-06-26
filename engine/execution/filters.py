# execution/filters.py

import logging
import time

logger = logging.getLogger("execution_filters")


def _positive_oi(row: dict, key: str) -> float:
    try:
        value = float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def has_oi_wall(option_chain, atm_strike, direction, *, max_age_seconds: float = 180.0):
    """
    Return True when a reliable OI wall sits directly in the trade path.

    Missing/stale chain data must not become a trade rejection.  Earlier logic
    averaged every nearby strike, including zero OI from unsubscribed/stale
    ticks; that made a single populated far strike look like a 2x "wall".
    """
    try:
        if not option_chain:
            return False

        now = time.time()
        live_rows = []
        for row in option_chain:
            if row.get("stale"):
                continue
            updated_at = row.get("updated_at")
            if updated_at is not None:
                try:
                    if now - float(updated_at) > max_age_seconds:
                        continue
                except (TypeError, ValueError):
                    continue
            if _positive_oi(row, "ce_oi") > 0 and _positive_oi(row, "pe_oi") > 0:
                live_rows.append(row)

        nearby = sorted(
            live_rows,
            key=lambda x: abs(float(x.get("strike", 0) or 0) - atm_strike)
        )[:5]

        # Need enough live strikes on both sides of ATM for the comparison to
        # be meaningful.  If not, let price/ML decide instead of blocking.
        below = [s for s in nearby if float(s.get("strike", 0) or 0) < atm_strike]
        above = [s for s in nearby if float(s.get("strike", 0) or 0) > atm_strike]
        if len(nearby) < 5 or not below or not above:
            logger.debug(
                "[OI WALL] insufficient live chain rows: nearby=%d below=%d above=%d",
                len(nearby), len(below), len(above),
            )
            return False

        if direction == "CE":
            path_rows = above
            oi_key = "ce_oi"
        elif direction == "PE":
            path_rows = below
            oi_key = "pe_oi"
        else:
            return False

        side_oi = [_positive_oi(s, oi_key) for s in nearby]
        path_oi = [_positive_oi(s, oi_key) for s in path_rows]
        side_oi = [v for v in side_oi if v > 0]
        path_oi = [v for v in path_oi if v > 0]
        if len(side_oi) < 5 or not path_oi:
            return False

        baseline = sum(side_oi) / len(side_oi)
        if baseline <= 0:
            return False

        for row in path_rows:
            strike = float(row.get("strike", 0) or 0)
            wall_oi = _positive_oi(row, oi_key)
            if wall_oi > baseline * 2:
                logger.info(
                    "[OI WALL] %s wall strike=%.0f oi=%.0f baseline=%.0f",
                    direction, strike, wall_oi, baseline,
                )
                return True

        return False

    except Exception as exc:
        logger.warning("[OI WALL] filter failed open: %s", exc)
        return False
