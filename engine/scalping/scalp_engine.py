import time
import logging
from datetime import datetime, time as dtime, timedelta

from engine.execution.filters import compute_entry_quality, df_from_ticks
from engine.execution.cost_model import round_trip_cost

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
        self._sl_med_pts    = float(getattr(config, "SCALP_SL_MED_PTS", 5.0))
        self._sl_wide_pts   = float(getattr(config, "SCALP_SL_WIDE_PTS", 8.0))
        self._target_pts    = config.SCALP_TARGET_PTS
        self._max_hold      = config.SCALP_MAX_HOLD_SECONDS
        self._mom_window    = config.SCALP_MOMENTUM_WINDOW
        self._mom_thresh    = config.SCALP_MOMENTUM_THRESHOLD
        self._cooldown_secs = config.SCALP_COOLDOWN
        # HTF agreement required for ALL scalps (was safe-mode only). Weak
        # counter-trend entries were a top loss driver on Aug-18.
        self._require_htf   = bool(getattr(config, "SCALP_REQUIRE_HTF_AGREE", True))
        self._require_vwap  = bool(getattr(config, "SCALP_REQUIRE_VWAP_ALIGN", True))
        self._min_samples   = int(getattr(config, "SCALP_CONFIRM_MIN_SAMPLES", 6))
        self._tail_frac     = float(getattr(config, "SCALP_EXHAUST_TAIL_FRAC", 0.65))
        self._no_life_secs  = int(getattr(config, "SCALP_NO_LIFE_SECONDS", 35))
        self._be_pts        = float(getattr(config, "SCALP_BE_PTS", 2.0))
        self._max_move_pts  = float(getattr(config, "SCALP_MAX_MOVE_PTS", 25.0))
        self._last_exit_ts  = 0.0
        # ATR-adaptive SL (Aug-20): multipliers for SL = ATR * mult.
        # Falls back to fixed tiers when ATR is unavailable (<=0).
        self._atr_sl_strict_mult = float(getattr(config, "SCALP_ATR_SL_STRICT_MULT", 0.15))
        self._atr_sl_med_mult    = float(getattr(config, "SCALP_ATR_SL_MED_MULT", 0.25))
        self._atr_sl_wide_mult   = float(getattr(config, "SCALP_ATR_SL_WIDE_MULT", 0.40))
        # Open-volatility penalty (Aug-20)
        self._open_vol_window = int(getattr(config, "SCALP_OPEN_VOL_WINDOW_S", 900))
        self._open_vol_mult   = float(getattr(config, "SCALP_OPEN_VOL_SL_MULT", 1.5))
        # Entry-quality rejection counters (shared filter, Task #7)
        self._eq_rejections: dict = {}
        self._last_eq_reason = None
        # Task #14: round-trip cost for the EQ NOT_PROFITABLE rule — same
        # source as live_engine (round_trip_cost(LOT_SIZE, config)), computed
        # once since config is not stored on the instance.
        # SCALP uses SCALP_LOTS (default 2) * LOT_SIZE (30) = 60 qty
        _scalp_qty = getattr(config, "SCALP_LOTS", 2) * getattr(config, "LOT_SIZE", 30)
        self._eq_cost_rs = round_trip_cost(_scalp_qty, config)

        logger.info(
            f"[SCALP ENGINE] initialized | threshold={self._mom_thresh}pt "
            f"window={self._mom_window}s SL={self._sl_pts}pt(fixed) "
            f"ATR_SL: strict={self._atr_sl_strict_mult}x med={self._atr_sl_med_mult}x wide={self._atr_sl_wide_mult}x "
            f"target={self._target_pts}pt max_hold={self._max_hold}s "
            f"cooldown={self._cooldown_secs}s"
        )

    def check_entry(self, ltp_now: float, ltp_history, ts: datetime,
                    htf5: int = 0, safe_mode: bool = False, vwap_confirms: bool = False):
        """
        Returns {"side":"CE"|"PE", "reason":"SCALP_MOM", "move_pts": float} or None.
        ltp_history: deque of (datetime, float) pairs, oldest first.

        Entry CONFIRMATION (fixes buying the top of momentum spikes ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ the
        Aug-17 losses where SCALP entered at the extreme and stopped in <16s):
          1. STRUCTURE - recent half must extend the move (HH for CE / LL for PE)
          2. PULLBACK  - price must be off the extreme by 10-50% of the range
                         (no breakout chasing; deep give-back = failed move)
          3. HTF       - 5m SuperTrend must not oppose (htf5: -1/0/1, 0 = allow)

        SAFE_SCALP mode (ML engine silent for ML_INACTIVITY_MINUTES): stricter ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
          - HTF must AGREE, not merely not-oppose (CE needs htf5=+1, PE htf5=-1)
          - pullback band tightened to 15-40% of range
          - momentum bar raised 25% (no weak 12pt flutters when the smart
            engine is dead)
        """
        if ltp_now <= 0:
            return None

        now_time = ts.time()
        if not (_SCALP_START <= now_time < _SCALP_END):
            return None

        if time.time() - self._last_exit_ts < self._cooldown_secs:
            return None

        # Exhaustion cap (Aug-18): block entries when NIFTY has already moved
        # more than SCALP_MAX_MOVE_PTS in the window. Entries at 20-41pt moves
        # on Aug-18 had 0% win rate ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ chasing extended moves = guaranteed loss.
        _max_move = self._max_move_pts
        if len(ltp_history) >= 2:
            _cutoff_move = ts - timedelta(seconds=self._mom_window)
            _past_move = [(t, ltp) for t, ltp in ltp_history if t >= _cutoff_move]
            if _past_move and len(_past_move) >= 2:
                _earliest = _past_move[0][1]
                _total_move = abs(ltp_now - _earliest)
                if _total_move > _max_move:
                    return None

        if len(ltp_history) < 2:
            return None

        cutoff = ts - timedelta(seconds=self._mom_window)
        past = [(t, ltp) for t, ltp in ltp_history if t >= cutoff]
        if not past:
            return None

        earliest_ltp = past[0][1]
        move = ltp_now - earliest_ltp

        _bar = self._mom_thresh * (1.25 if safe_mode else 1.0)
        if move < _bar and move > -_bar:
            return None
        side = "CE" if move >= _bar else "PE"

        # ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Confirmation on the momentum window ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
        # Confirmation on the momentum window
        prices = [p for _, p in past]
        # MANDATORY: if we don't have enough ticks to confirm structure, skip.
        if len(prices) < self._min_samples:
            return None

        # 0. STALE SIGNAL (Aug-20): if the move happened >20s ago and
        #    price reversed back below 50% of the move, skip.
        _move_age_s = (ts - past[0][0]).total_seconds()
        if _move_age_s > 20:
            _remaining = abs(ltp_now - prices[0])
            if _remaining < self._mom_thresh * 0.5:
                return None

        half = len(prices) // 2
        first, second = prices[:half], prices[half:]
        h1, l1 = max(first), min(first)
        h2, l2 = max(second), min(second)
        rng = max(h2 - l2, 1e-9)

        # 1. STRUCTURE - continuation required
        if side == "CE" and h2 <= h1:
            return None
        if side == "PE" and l2 >= l1:
            return None

        # NEW: ENTER ON BREAKOUT/CONTINUATION - price breaks recent high/low with momentum
        # We require the current price to be beyond the recent range (h2 for CE, l2 for PE)
        # and the move to be significant (at least _mom_thresh)
        if side == "CE":
            # For CE, we need price to break above h2 (recent high) with enough momentum
            if ltp_now <= h2:
                return None
            # Ensure momentum is sufficient - we already checked this above with _bar
        else:  # PE
            # For PE, we need price to break below l2 (recent low) with enough momentum
            if ltp_now >= l2:
                return None

        # 3. EXHAUSTION - don't buy the tail of a fresh vertical spike.: don't buy the tail of a vertical spike.
        if len(prices) >= 4:
            q = max(1, len(prices) // 4)
            tail_move = abs(ltp_now - prices[-q])
            total_move = abs(ltp_now - prices[0])
            if total_move > 1e-9 and tail_move > self._tail_frac * total_move:
                return None

        # 3. HTF - 5m SuperTrend. With SCALP_REQUIRE_HTF_AGREE (default on) it
        #    must AGREE (+1 for CE, -1 for PE); neutral (0) blocks too. Only
        #    when explicitly disabled does neutral become allowed.
        if self._require_htf or safe_mode:
            if side == "CE" and htf5 != 1:
                return None
            if side == "PE" and htf5 != -1:
                return None
        else:
            if side == "CE" and htf5 == -1:
                return None
            if side == "PE" and htf5 == 1:
                return None

        # 3b. VWAP ALIGNMENT (Aug-25): require price on correct side of VWAP
        # CE needs price >= VWAP, PE needs price <= VWAP
        if self._require_vwap and not vwap_confirms:
            return None

        # 4. ENTRY QUALITY — rejection-first timing/quality gate shared with
        #    the ML path. The tick window is bucketed into synthetic candles
        #    (scalp has no 1m bars) so the same OHLC rules apply. Any
        #    rejection = no entry.
        eq = {"metrics": None}
        _eq_df = df_from_ticks(past)
        if _eq_df is not None:
            eq = compute_entry_quality(
                _eq_df, side, ltp_now, ts,
                {"breakout_ts": None, "orb_done": False},
                cost_rs=self._eq_cost_rs,
            )
            if not eq["accepted"]:
                self._eq_rejections[eq["reason"]] = \
                    self._eq_rejections.get(eq["reason"], 0) + 1
                self._last_eq_reason = eq["reason"]
                logger.debug(
                    f"[SCALP EQ] REJECT {eq['reason']} | {eq['metrics']}"
                )
                return None

        return {"side": side, "reason": "SCALP_MOM", "move_pts": round(move, 2),
                "entry_quality": eq.get("metrics")}

    def adaptive_sl_pts(self, side: str, move_pts: float,
                        htf5: int = 0, vwap_confirms: bool = False,
                        ml_active: bool = True, atr: float = 0.0, now=None):
        """
        Pick the initial scalp stop based on the system's movement conviction
        AND current volatility (ATR-adaptive since Aug-20).

        When ATR > 0, the stop is:
            SL = max(ATR * tier_mult, fixed_floor)
        where tier_mult comes from the conviction score (strict/med/wide).
        The fixed floor prevents micro-ATR environments from producing
        impossibly tight stops.

        When ATR is unavailable (<=0), falls back to the original fixed tiers.

        Returns stop distance in NIFTY spot points.
        """
        score = 0.0
        # Strength of the momentum burst itself (move_pts = NIFTY spot pts)
        if abs(move_pts) >= 30:
            score += 2.0
        elif abs(move_pts) >= 20:
            score += 1.0
        # HTF 5m SuperTrend agreement
        if (side == "CE" and htf5 == 1) or (side == "PE" and htf5 == -1):
            score += 2.0
        elif htf5 != 0:
            score += 0.5
        # VWAP agreement
        if vwap_confirms:
            score += 1.0
        # ML engine alive
        if ml_active:
            score += 1.0

        # -- ATR-adaptive SL (Aug-20) --
        if atr > 0:
            if score >= 4.5:
                atr_sl = max(atr * self._atr_sl_wide_mult, self._sl_pts)
            elif score >= 2.5:
                atr_sl = max(atr * self._atr_sl_med_mult, self._sl_pts)
            else:
                atr_sl = max(atr * self._atr_sl_strict_mult, self._sl_pts)
            tier = "WIDE" if score >= 4.5 else ("MED" if score >= 2.5 else "STRICT")
            # Open-volatility penalty: widen SL during first N seconds after ORB
            if now is not None and self._open_vol_window > 0:
                _orb_unlock = now.replace(hour=9, minute=30, second=0, microsecond=0)
                _elapsed = (now - _orb_unlock).total_seconds()
                if 0 <= _elapsed < self._open_vol_window:
                    atr_sl *= self._open_vol_mult
                    atr_sl = round(atr_sl, 2)
                    tier = "OPEN-" + tier
            return round(atr_sl, 2), tier

        # -- Fallback: fixed tiers (no ATR available) --
        # Open-volatility penalty for fallback tiers too
        if now is not None and self._open_vol_window > 0:
            _orb_unlock = now.replace(hour=9, minute=30, second=0, microsecond=0)
            _elapsed = (now - _orb_unlock).total_seconds()
            if 0 <= _elapsed < self._open_vol_window:
                _mult = self._open_vol_mult
                if score >= 4.5:
                    return round(self._sl_wide_pts * _mult, 2), "OPEN-WIDE"
                if score >= 2.5:
                    return round(self._sl_med_pts * _mult, 2), "OPEN-MED"
                return round(self._sl_pts * _mult, 2), "OPEN-STRICT"
        if score >= 4.5:
            return self._sl_wide_pts, "WIDE"
        if score >= 2.5:
            return self._sl_med_pts, "MED"
        return self._sl_pts, "STRICT"

    def check_exit(self, scalp_pos: dict, current_ltp: float, ts: datetime):
        """
        Returns (should_exit: bool, reason: str).
        LONG CE & PE both profit when premium RISES ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ identical exit logic.
        """
        entry = scalp_pos["entry"]

        # LONG CE & PE: stop when premium FALLS below the ACTIVE stop level.
        # Uses scalp_pos["stop_loss"] (set by master_runner ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ adaptive/staged)
        # instead of a hardcoded entry-SL so adaptive stops actually work.
        sl = scalp_pos.get("stop_loss", entry - self._sl_pts)
        if current_ltp <= sl:
            return True, "STOP"
        if current_ltp >= entry + self._target_pts:
            return True, "TARGET"

        held = (ts - scalp_pos["entry_ts"]).total_seconds()
        if held > self._max_hold:
            return True, "TIME_EXIT"

        # NO-LIFE EXIT (Aug-18): the trade never reached the breakeven zone
        # (+BE_PTS, i.e. be_triggered still False) within NO_LIFE seconds ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
        # the entry was wrong, so cut at market instead of running the full
        # stop. Today's data: losers sat dead 59-81s then lost 8pt; every
        # winner showed life within 9-34s, so live trades never hit this.
        if (not scalp_pos.get("be_triggered")
                and held > self._no_life_secs
                and current_ltp < entry + self._be_pts):
            return True, "NO_LIFE"

        return False, ""

    def on_exit(self):
        self._last_exit_ts = time.time()
