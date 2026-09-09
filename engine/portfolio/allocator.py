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

import numpy as np

logger = logging.getLogger("allocator")


class CapitalAllocator:

    def __init__(self, config):
        self.config = config
        self.max_risk_pct = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.01"))
        self.max_lots = int(os.getenv("MAX_LOTS_PER_TRADE", "10"))

        # ── Vol-scaling + confidence sizing (feat(allocator), default OFF) ──
        # ENABLE_VOL_SCALING=1 turns on two de-risking multipliers applied on
        # top of the base risk-sized lots:
        #   vol_adj        = clip(TARGET_ATR_PTS / realized_atr, 0.25, 2.0)
        #                    shrink size when realized vol is high
        #   confidence_adj = clip(edge_fraction * (2*p - 1), 0.0, max_frac)
        #                    only deploy size proportional to calibrated edge
        self.vol_scaling   = os.getenv("ENABLE_VOL_SCALING", "0") == "1"
        self.target_atr    = float(os.getenv("TARGET_ATR_PTS", "20.0"))
        self.atr_period    = int(os.getenv("ATR_PERIOD", "14"))
        self.vol_min       = float(os.getenv("VOL_ADJ_MIN", "0.25"))
        self.vol_max       = float(os.getenv("VOL_ADJ_MAX", "2.0"))
        self.edge_fraction = float(os.getenv("CONF_EDGE_FRACTION", "0.3"))
        self.conf_max      = float(os.getenv("CONF_MAX_FRAC", "0.02"))  # 2% of equity

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

    # ── vol + confidence scaling helpers (feature-flagged) ─────────────
    def realized_atr(self, candles) -> float:
        """
        Wilder ATR on the last `atr_period` candles (numpy, no deps).
        `candles` is an iterable of dicts with keys high/low/close
        (candle-buffer rows) OR a pandas DataFrame. Returns NaN if short.
        """
        if candles is None or len(candles) == 0:
            return float("nan")
        try:  # pandas DataFrame
            high = candles["high"].to_numpy(dtype=float)
            low  = candles["low"].to_numpy(dtype=float)
            close = candles["close"].to_numpy(dtype=float)
        except (KeyError, AttributeError, TypeError):
            high  = np.array([c["high"] for c in candles], dtype=float)
            low   = np.array([c["low"] for c in candles], dtype=float)
            close = np.array([c["close"] for c in candles], dtype=float)
        if len(close) < self.atr_period:
            return float("nan")
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
        # Wilder RMA (EMA with alpha=1/period)
        a = 1.0 / self.atr_period
        rma = np.empty(len(tr)); rma[0] = tr[0]
        for i in range(1, len(tr)):
            rma[i] = (tr[i] - rma[i - 1]) * a + rma[i - 1]
        return float(rma[-1])

    def _vol_adjustment(self, candles) -> float:
        """clip(TARGET_ATR / realized_atr, 0.25, 2.0); neutral (1.0) if no data."""
        atr = self.realized_atr(candles)
        if not np.isfinite(atr) or atr <= 1e-6:
            return 1.0
        return float(np.clip(self.target_atr / atr, self.vol_min, self.vol_max))

    def _confidence_adjustment(self, calibrated_p) -> float:
        """clip(edge_fraction * (2p - 1), 0.0, max_frac). Neutral when no p."""
        if calibrated_p is None or not np.isfinite(calibrated_p):
            return 1.0
        return float(np.clip(self.edge_fraction * (2.0 * calibrated_p - 1.0),
                             0.0, self.conf_max))

    def size_position(self, capital, ml_prob, atr, price, lot_size,
                      current_pnl, spread_pct=0, stop_pts=None,
                      candles=None, calibrated_p=None):
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
            candles      : recent OHLC candles for realized-ATR vol scaling
                           (only used when ENABLE_VOL_SCALING=1)
            calibrated_p : calibrated probability for confidence sizing
                           (only used when ENABLE_VOL_SCALING=1)
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

        # ── Vol + confidence scaling (feat(allocator), default OFF) ───
        # size = base_risk_size * vol_adj * confidence_adj
        # vol_adj: shrink on high realized vol, allow more on quiet tape.
        # confidence_adj: only deploy size proportional to calibrated edge.
        if self.vol_scaling:
            vol_adj = self._vol_adjustment(candles)
            conf_adj = self._confidence_adjustment(
                calibrated_p if calibrated_p is not None else ml_prob
            )
            scaled = lots * vol_adj * conf_adj
            logger.info(
                f"[ALLOC] vol-scaling: vol_adj={vol_adj:.2f} "
                f"conf_adj={conf_adj:.3f} -> {lots} -> {scaled:.2f} lots"
            )
            lots = max(0, min(self.max_lots, int(scaled)))

        logger.info(
            f"[ALLOC] prob={ml_prob:.3f}>={min_prob:.3f} | risk=Rs{risk_capital:.0f} "
            f"| stop={risk_pts:.1f}pt x {lot_size} = Rs{risk_per_lot:.0f}/lot "
            f"-> {lots} lot(s)"
        )
        return lots
