# engine/portfolio/allocator.py
#
# Position sizing — the LAST gate before an order goes out.
#
# HISTORY / WHY THIS FILE WAS REWRITTEN (2026-09-06):
#   The old code opened with `if ml_prob < 0.55: return 0`.  That number was
#   hardcoded here while the live decision threshold lives in
#   Config.CHAMPION_THRESHOLD (0.40-0.42).  The deployed ensemble's maximum
#   output on 2026-09-04 was CE 0.524 / PE 0.120, so the 0.55 floor was
#   UNREACHABLE: every signal that survived all six upstream entry gates was
#   silently rejected here (13x ALLOC_ZERO that session, 0 trades all day).
#   A sizing function must never carry its own conviction threshold — that
#   decision belongs to the predictor + Config, in one place.
#
#   Second bug: `raw_lots = risk_capital / (atr * lot_size)` mixed dimensions.
#   `atr` is SPOT ATR in index points (~20-30 for BANKNIFTY) but the risk being
#   sized is the option's PREMIUM stop distance (4-10 pts).  Sizing on spot ATR
#   understated lots by ~2-3x and, with `max(1, ...)`, collapsed to exactly
#   1 lot every time — the risk_pct setting had no effect at all.
#   Sizing now uses the real premium stop distance when the caller supplies it.

import logging
import os

logger = logging.getLogger("allocator")


class CapitalAllocator:

    def __init__(self, config):
        self.config = config
        self.max_risk_pct = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.01"))
        self.max_lots = int(os.getenv("MAX_LOTS_PER_TRADE", "10"))

    # ── threshold: single source of truth ─────────────────────────────
    def _min_prob(self) -> float:
        """
        Conviction floor = the SAME threshold the predictor gated on.
        Read from the live Config instance (env-driven), never hardcoded.
        """
        try:
            return float(getattr(self.config, "CHAMPION_THRESHOLD", 0.40))
        except Exception:
            return 0.40

    def size_position(self, capital, ml_prob, atr, price, lot_size,
                      current_pnl, spread_pct=0, stop_pts=None):
        """
        Returns LOTS (not units).  0 means "do not trade".

        Args:
            capital      : account capital in Rs
            ml_prob      : model probability for the chosen side
            atr          : SPOT ATR in index points (fallback only)
            price        : option premium at entry
            lot_size     : units per lot (BANKNIFTY = 30)
            current_pnl  : running day PnL (reserved for future de-risking)
            spread_pct   : bid-ask spread as a fraction of premium
            stop_pts     : PREMIUM stop distance in points. When supplied this
                           is the true per-unit risk and drives sizing.
        """
        min_prob = self._min_prob()

        if ml_prob is None or ml_prob < min_prob:
            logger.info(
                f"[ALLOC] reject: ml_prob={ml_prob} < threshold={min_prob:.3f}"
            )
            return 0

        if spread_pct and spread_pct > 0.06:
            logger.info(f"[ALLOC] reject: spread_pct={spread_pct:.3f} > 0.06")
            return 0

        lot_size = int(lot_size) if lot_size and lot_size > 0 else int(
            getattr(self.config, "LOT_SIZE", 30)
        )

        risk_capital = capital * self.max_risk_pct

        # Per-unit risk in PREMIUM points — this is what actually gets lost
        # when the stop fills, so it is what capital must be sized against.
        risk_pts = stop_pts if (stop_pts and stop_pts > 0) else None
        if risk_pts is None:
            # Fallback when the caller has no stop yet: approximate the
            # premium stop the risk manager would produce (delta ~0.5 on spot
            # ATR, clamped to the same 4-10pt band), never raw spot ATR.
            spot_atr = atr if (atr and atr > 0) else max(price, 1.0) * 0.01
            risk_pts = min(max(0.5 * spot_atr * 0.9, 4.0), 10.0)

        risk_per_lot = risk_pts * lot_size
        if risk_per_lot <= 0:
            return 0

        lots = int(risk_capital / risk_per_lot)
        lots = max(1, min(self.max_lots, lots))

        logger.info(
            f"[ALLOC] prob={ml_prob:.3f}>={min_prob:.3f} | risk=Rs{risk_capital:.0f} "
            f"| stop={risk_pts:.1f}pt x {lot_size} = Rs{risk_per_lot:.0f}/lot "
            f"-> {lots} lot(s)"
        )
        return lots
