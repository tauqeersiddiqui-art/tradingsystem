import time
import logging
from datetime import datetime, time as dtime, timedelta

logger = logging.getLogger("scalp")

_SCALP_START = dtime(9, 30)
_SCALP_END   = dtime(15, 10)


class ScalpEngine:
    """
    Momentum scalper: fires CE/PE when NIFTY spot moves >= threshold in N seconds.
    Runs only when the main ML position is flat. Fixed 1-lot sizing always.
    """

    def __init__(self, config):
        self._sl_pts        = config.SCALP_SL_PTS
        self._target_pts    = config.SCALP_TARGET_PTS
        self._max_hold      = config.SCALP_MAX_HOLD_SECONDS
        self._mom_window    = config.SCALP_MOMENTUM_WINDOW
        self._mom_thresh    = config.SCALP_MOMENTUM_THRESHOLD
        self._cooldown_secs = config.SCALP_COOLDOWN
        # Start with a full cooldown so first scalp cannot fire in the first N seconds
        # after startup.  Prevents immediately acting on stale pre-restart momentum.
        self._last_exit_ts  = time.time()

        logger.info(
            f"[SCALP ENGINE] initialized | threshold={self._mom_thresh}pt "
            f"window={self._mom_window}s SL={self._sl_pts}pt "
            f"target={self._target_pts}pt max_hold={self._max_hold}s "
            f"cooldown={self._cooldown_secs}s"
        )

    def check_entry(self, ltp_now: float, ltp_history, ts: datetime):
        """
        Returns {"side":"CE"|"PE", "reason":"SCALP_MOM", "move_pts": float} or None.
        ltp_history: deque of (datetime, float) pairs, oldest first.
        """
        if ltp_now <= 0:
            return None

        now_time = ts.time()
        if not (_SCALP_START <= now_time < _SCALP_END):
            return None

        if time.time() - self._last_exit_ts < self._cooldown_secs:
            return None

        if len(ltp_history) < 2:
            return None

        cutoff = ts - timedelta(seconds=self._mom_window)
        past = [(t, ltp) for t, ltp in ltp_history if cutoff <= t < ts]
        if not past:
            return None

        earliest_ltp = past[0][1]
        move = ltp_now - earliest_ltp

        # ── Fresh-extreme gate (2026-06-30) ──────────────────────────────
        # ROOT CAUSE of scalp losses: 23% of trades were MFE=0 "instant
        # reversals" — entered AFTER the spot spike had already peaked, bought
        # the inflated option premium at the local top, then gapped through the
        # stop on the mean-revert (−363 avg). These are LATE entries into an
        # EXHAUSTED move; raising the cumulative threshold only buys a bigger,
        # later spike and makes it worse.
        #
        # Fix: only fire when price is STILL making a fresh extreme in the trade
        # direction — i.e. momentum is still extending (a real breakout), not
        # already rolling over. For CE the current tick must be at/near the
        # window HIGH; for PE at/near the window LOW. A move that cleared the
        # threshold earlier but has since faded off its extreme is rejected.
        prices  = [ltp for _, ltp in past] + [ltp_now]
        win_hi  = max(prices)
        win_lo  = min(prices)
        # Tolerance: allow entry within this many points of the extreme so a
        # 1-tick micro-wobble at the high doesn't block a genuine breakout.
        _eps = getattr(self, "_fresh_extreme_tol", 2.0)

        if move >= self._mom_thresh:
            if ltp_now < win_hi - _eps:
                logger.info(
                    f"[SCALP SKIP] CE move=+{move:.1f}pt but faded off high "
                    f"(now={ltp_now:.1f} hi={win_hi:.1f}) — exhausted, not fresh"
                )
                return None
            return {"side": "CE", "reason": "SCALP_MOM", "move_pts": round(move, 2)}
        if move <= -self._mom_thresh:
            if ltp_now > win_lo + _eps:
                logger.info(
                    f"[SCALP SKIP] PE move={move:.1f}pt but faded off low "
                    f"(now={ltp_now:.1f} lo={win_lo:.1f}) — exhausted, not fresh"
                )
                return None
            return {"side": "PE", "reason": "SCALP_MOM", "move_pts": round(move, 2)}

        return None

    def check_exit(self, scalp_pos: dict, current_ltp: float, ts: datetime):
        """
        Returns (should_exit: bool, reason: str).
        """
        entry = scalp_pos["entry"]

        if current_ltp <= entry - self._sl_pts:
            return True, "STOP"

        if current_ltp >= entry + self._target_pts:
            return True, "TARGET"

        held = (ts - scalp_pos["entry_ts"]).total_seconds()
        if held > self._max_hold:
            return True, "TIME_EXIT"

        return False, ""

    def on_exit(self):
        self._last_exit_ts = time.time()
