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
        self._min_samples   = int(getattr(config, "SCALP_CONFIRM_MIN_SAMPLES", 6))
        self._tail_frac     = float(getattr(config, "SCALP_EXHAUST_TAIL_FRAC", 0.65))
        self._no_life_secs  = int(getattr(config, "SCALP_NO_LIFE_SECONDS", 35))
        self._be_pts        = float(getattr(config, "SCALP_BE_PTS", 2.0))
        self._max_move_pts  = float(getattr(config, "SCALP_MAX_MOVE_PTS", 25.0))
        self._last_exit_ts  = 0.0

        logger.info(
            f"[SCALP ENGINE] initialized | threshold={self._mom_thresh}pt "
            f"window={self._mom_window}s SL={self._sl_pts}pt "
            f"target={self._target_pts}pt max_hold={self._max_hold}s "
            f"cooldown={self._cooldown_secs}s"
        )

    def check_entry(self, ltp_now: float, ltp_history, ts: datetime,
                    htf5: int = 0, safe_mode: bool = False):
        """
        Returns {"side":"CE"|"PE", "reason":"SCALP_MOM", "move_pts": float} or None.
        ltp_history: deque of (datetime, float) pairs, oldest first.

        Entry CONFIRMATION (fixes buying the top of momentum spikes — the
        Aug-17 losses where SCALP entered at the extreme and stopped in <16s):
          1. STRUCTURE - recent half must extend the move (HH for CE / LL for PE)
          2. PULLBACK  - price must be off the extreme by 10-50% of the range
                         (no breakout chasing; deep give-back = failed move)
          3. HTF       - 5m SuperTrend must not oppose (htf5: -1/0/1, 0 = allow)

        SAFE_SCALP mode (ML engine silent for ML_INACTIVITY_MINUTES): stricter —
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
        # on Aug-18 had 0% win rate � chasing extended moves = guaranteed loss.
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

        # ── Confirmation on the momentum window ────────────────────────
        prices = [p for _, p in past]
        # MANDATORY: if we don't have enough ticks to confirm structure, we
        # don't enter. (Old code skipped the whole confirmation when the
        # window had <6 samples — a hole that let sparse-data spike-tails
        # through.) Aug-18 fix.
        if len(prices) < self._min_samples:
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

        # 2. PULLBACK - 10-50% off the extreme (15-40% in safe mode),
        #    no chasing, no deep give-back
        pullback = (h2 - ltp_now) if side == "CE" else (ltp_now - l2)
        _pb_lo, _pb_hi = (0.15, 0.40) if safe_mode else (0.10, 0.50)
        if not (_pb_lo * rng <= pullback <= _pb_hi * rng):
            return None
        # structure must not have broken: still inside the range
        if side == "CE" and ltp_now < l2:
            return None
        if side == "PE" and ltp_now > h2:
            return None

        # 3. EXHAUSTION - don't buy the tail of a fresh vertical spike.
        #    If the last quarter of the window carries most of the total
        #    move, the burst is still exploding (one-minute vertical
        #    candles that close at their extreme, e.g. 11:50 +33pt, 12:07
        #    +20pt on Aug-18) and reverses instantly. A real trend
        #    develops across the whole window.
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

        return {"side": side, "reason": "SCALP_MOM", "move_pts": round(move, 2)}

        return None

    def adaptive_sl_pts(self, side: str, move_pts: float,
                        htf5: int = 0, vwap_confirms: bool = False,
                        ml_active: bool = True) -> float:
        """
        Pick the initial scalp stop based on the system's movement conviction.

        Idea (Aug-18): if the system does NOT expect follow-through (weak move,
        no HTF/VWAP support, ML silent) keep a STRICT stop so a bad trade is
        cut immediately. If the system DOES expect movement (strong burst +
        HTF agrees + VWAP confirms + ML alive) widen the stop so a real move
        gets room instead of being stopped by bid/ask noise in the first
        seconds (today's 8s/9s/10s exits were noise stops on 3pt).

        Returns stop distance in premium points: strict / med / wide.
        """
        score = 0.0
        # Strength of the momentum burst itself (move_pts = NIFTY spot pts)
        if abs(move_pts) >= 30:
            score += 2.0
        elif abs(move_pts) >= 20:
            score += 1.0
        # HTF 5m SuperTrend agreement is the strongest "movement" signal
        if (side == "CE" and htf5 == 1) or (side == "PE" and htf5 == -1):
            score += 2.0
        elif htf5 != 0:
            score += 0.5
        # VWAP agreement
        if vwap_confirms:
            score += 1.0
        # ML engine alive = smarter engine also sees the move
        if ml_active:
            score += 1.0

        if score >= 4.5:
            return self._sl_wide_pts
        if score >= 2.5:
            return self._sl_med_pts
        return self._sl_pts

    def check_exit(self, scalp_pos: dict, current_ltp: float, ts: datetime):
        """
        Returns (should_exit: bool, reason: str).
        LONG CE & PE both profit when premium RISES → identical exit logic.
        """
        entry = scalp_pos["entry"]

        # LONG CE & PE: stop when premium FALLS below the ACTIVE stop level.
        # Uses scalp_pos["stop_loss"] (set by master_runner — adaptive/staged)
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
        # (+BE_PTS, i.e. be_triggered still False) within NO_LIFE seconds —
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
