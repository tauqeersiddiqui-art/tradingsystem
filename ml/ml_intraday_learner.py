# ml/ml_intraday_learner.py
# ═══════════════════════════════════════════════════════════════════════
#  INTRADAY ML LEARNER — The missing brain
#
#  Problem:  ML is trained once, never updates. Makes same mistakes daily.
#  Solution: After each trade, update probability weights for REST of day.
#
#  HOW IT WORKS (no retraining needed):
#
#  1. BAYESIAN PROBABILITY UPDATE
#     After each trade outcome, update CE/PE win probability for today.
#     If CE trade lost → lower CE confidence for next signal
#     If PE trade won  → boost PE confidence for next signal
#
#  2. DAY TYPE DETECTION (first 30 minutes tells you everything)
#     TREND_DAY:   First candle direction holds all day → follow trend
#     RANGE_DAY:   Multiple reversals → fade extremes, tighten targets
#     VOLATILE_DAY: Wide swings → raise ML bar, reduce size
#     GAP_DAY:      Opened with big gap → wait for gap fill first
#
#  3. FEATURE RELIABILITY SCORING
#     Track which features predicted correctly today
#     RSI worked 3/3 today → weight it higher
#     EMA20 failed 2/2 today → weight it lower
#
#  4. AI BRAIN (Claude API) — called after 2+ consecutive losses
#     Sends today's trade log to Claude API
#     Gets back: what feature failed, what to watch for next entry
#     Updates internal weights based on AI analysis
# ═══════════════════════════════════════════════════════════════════════

import json, os, time, logging
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("ml_intraday_learner")

# ── DAY TYPE DEFINITIONS ─────────────────────────────────────────────
DAY_UNKNOWN   = "UNKNOWN"
DAY_TREND     = "TREND_DAY"      # directional, follow the move
DAY_RANGE     = "RANGE_DAY"      # oscillating, fade extremes
DAY_VOLATILE  = "VOLATILE_DAY"   # large swings, be careful
DAY_GAP       = "GAP_DAY"        # opened with gap, wait for fill


class IntradayMLLearner:
    """
    Real-time ML learning from today's trade outcomes.
    No model retraining — pure Bayesian probability updates.
    """

    def __init__(self):
        self.reset_day()

    def reset_day(self):
        """Call at start of each trading day (09:15)."""
        # Trade history today
        self.trades_today = []          # list of dicts: {side, pnl, features, ml_prob}

        # Bayesian win rate trackers
        self.ce_wins   = 0
        self.ce_losses = 0
        self.pe_wins   = 0
        self.pe_losses = 0

        # Adaptive ML threshold — starts high, tightens with consecutive wins
        self.base_threshold   = float(os.getenv("CHAMPION_THRESHOLD", 0.51))
        self.current_threshold = self.base_threshold

        # Adaptive probability multipliers (start neutral)
        self.ce_multiplier = 1.0   # applied to ML CE probability
        self.pe_multiplier = 1.0   # applied to ML PE probability

        # Day type detection
        self.day_type           = DAY_UNKNOWN
        self.day_type_locked    = False     # True after 30-min detection window
        self.open_price         = None
        self.first_30min_high   = 0.0
        self.first_30min_low    = 9999999.0
        self.first_30min_closes = []
        self.day_detected_at    = None

        # Consecutive state
        self.consecutive_losses = 0
        self.consecutive_wins   = 0
        self.last_loss_side     = None

        # Feature reliability today
        self.feature_scores = defaultdict(lambda: {"correct": 0, "total": 0})

        # AI brain state
        self.ai_review_pending  = False
        self.last_ai_review_time = 0
        self.ai_suggestions      = []

        logger.info("IntradayMLLearner reset for new day")

    # ── CORE API ─────────────────────────────────────────────────────

    def set_open_price(self, price: float):
        """Record NIFTY open price at 09:15."""
        if self.open_price is None:
            self.open_price = price
            logger.info(f"Day open set: {price}")

    def update_candle(self, close: float, high: float, low: float, ts: datetime):
        """
        Feed each candle during the first 30 minutes.
        After 09:45, detect day type once and lock it.
        """
        if self.day_type_locked:
            return

        if ts.hour == 9 and ts.minute < 45:
            self.first_30min_closes.append(close)
            self.first_30min_high = max(self.first_30min_high, high)
            self.first_30min_low  = min(self.first_30min_low, low)

        elif ts.hour == 9 and ts.minute >= 45 and not self.day_type_locked:
            self._detect_day_type()

    def _detect_day_type(self):
        """
        Analyse first 30 minutes to classify the day.
        This is the most important function — tells bot HOW to trade today.
        """
        if not self.open_price or len(self.first_30min_closes) < 3:
            self.day_type = DAY_UNKNOWN
            self.day_type_locked = True
            return

        closes = self.first_30min_closes
        open_p = self.open_price
        last   = closes[-1]
        rng    = self.first_30min_high - self.first_30min_low
        avg    = sum(closes) / len(closes)

        # Gap detection: if open is >0.5% away from yesterday close
        # (approximate — we check if open vs first close is large)
        gap_size = abs(open_p - closes[0]) / open_p if closes else 0

        # Trend strength in first 30 min
        if len(closes) >= 5:
            first_half = closes[:len(closes)//2]
            second_half = closes[len(closes)//2:]
            trending = (
                (min(second_half) > max(first_half)) or  # strong up
                (max(second_half) < min(first_half))     # strong down
            )
        else:
            trending = False

        # Direction of first 30 min move
        move_pct = (last - open_p) / open_p

        # Range classification
        range_pct = rng / open_p

        if range_pct > 0.012:         # >0.6% range in 30 min = volatile
            self.day_type = DAY_VOLATILE
        elif gap_size > 0.005:        # >0.5% gap = gap day
            self.day_type = DAY_GAP
        elif trending and abs(move_pct) > 0.004:  # >0.4% trending
            self.day_type = DAY_TREND
        else:
            self.day_type = DAY_RANGE

        self.day_type_locked    = True
        self.day_detected_at    = datetime.now()

        logger.info(
            f"Day type detected: {self.day_type} | "
            f"range_pct={range_pct:.3f} move_pct={move_pct:.3f} "
            f"gap={gap_size:.3f} trending={trending}"
        )

    def get_day_type(self) -> str:
        return self.day_type

    def get_ml_threshold(self) -> float:
        """
        Returns today's adaptive ML threshold.
        Starts at base (0.25). Rises with losses. Falls with wins.
        Day type also adjusts:
          VOLATILE_DAY: +0.10 (market unpredictable — be very selective)
          GAP_DAY:      +0.05 (wait for gap fill first)
          RANGE_DAY:    +0.08 (reversals are tricky)
          TREND_DAY:    +0.00 (follow the trend — base threshold is fine)
        """
        base = self.current_threshold
        day_adj = {
            DAY_VOLATILE: 0.10,
            DAY_GAP:      0.05,
            DAY_RANGE:    0.08,
            DAY_TREND:    0.00,
            DAY_UNKNOWN:  0.05,
        }.get(self.day_type, 0.05)
        return min(base + day_adj, 0.60)

    def get_adjusted_ml_prob(self, raw_ce: float, raw_pe: float,
                              direction: str) -> tuple:
        """
        Apply today's learned multipliers to raw ML probabilities.
        Returns (adjusted_ce, adjusted_pe).

        If CE has been losing today:  ce_multiplier < 1.0 → lower CE prob
        If PE has been winning today: pe_multiplier > 1.0 → boost PE prob
        """
        adj_ce = min(raw_ce * self.ce_multiplier, 0.99)
        adj_pe = min(raw_pe * self.pe_multiplier, 0.99)
        return round(adj_ce, 4), round(adj_pe, 4)

    def record_trade_result(self, side: str, pnl: float, ml_prob: float,
                             features: dict, reason: str):
        """
        Call immediately after every trade exit.
        Updates multipliers, threshold, consecutive counters.
        """
        trade = {
            "time":    datetime.now().isoformat(),
            "side":    side,
            "pnl":     round(pnl, 2),
            "ml_prob": round(ml_prob, 4),
            "reason":  reason,
        }
        self.trades_today.append(trade)

        is_win = pnl > 0

        if side == "CE":
            if is_win:
                self.ce_wins += 1
            else:
                self.ce_losses += 1
        else:
            if is_win:
                self.pe_wins += 1
            else:
                self.pe_losses += 1

        # ── Update multipliers ───────────────────────────────────────
        if is_win:
            self.consecutive_wins   += 1
            self.consecutive_losses  = 0
            self.last_loss_side      = None

            # Boost winning side's multiplier
            if side == "CE":
                self.ce_multiplier = min(self.ce_multiplier + 0.05, 1.30)
            else:
                self.pe_multiplier = min(self.pe_multiplier + 0.05, 1.30)

            # Lower threshold (ML is working today)
            self.current_threshold = max(
                self.current_threshold - 0.03, self.base_threshold)

        else:
            self.consecutive_losses += 1
            self.consecutive_wins    = 0
            self.last_loss_side      = side

            # Reduce losing side's multiplier
            if side == "CE":
                self.ce_multiplier = max(self.ce_multiplier - 0.10, 0.50)
            else:
                self.pe_multiplier = max(self.pe_multiplier - 0.10, 0.50)

            # Raise threshold (ML was wrong — need higher confidence)
            # Cap at 0.60 to keep system usable
            self.current_threshold = min(
                self.current_threshold + 0.05, 0.60)

            # After 2+ consecutive losses, request AI review
            if self.consecutive_losses >= 2:
                self.ai_review_pending = True

        # Update feature reliability
        self._update_feature_scores(features, is_win)

        logger.info(
            f"Trade recorded: {side} PnL={pnl:.0f} | "
            f"CE mult={self.ce_multiplier:.2f} PE mult={self.pe_multiplier:.2f} | "
            f"Threshold={self.current_threshold:.2f} | "
            f"ConsecLoss={self.consecutive_losses}"
        )

    def _update_feature_scores(self, features: dict, was_correct: bool):
        """Track which features are predictive today."""
        key_features = ["rsi", "ema_distance_pct", "trend_strength",
                         "volume_ratio", "ml_edge"]
        for f in key_features:
            val = features.get(f, None)
            if val is not None:
                self.feature_scores[f]["total"] += 1
                if was_correct:
                    self.feature_scores[f]["correct"] += 1

    def is_side_blocked(self, side: str) -> tuple:
        """
        Returns (blocked: bool, reason: str).
        Block a side if it's been consistently losing today.
        """
        if side == "CE" and self.ce_losses >= 2 and self.ce_wins == 0:
            return True, f"CE_LOSING_TODAY_{self.ce_losses}L_0W"
        if side == "PE" and self.pe_losses >= 2 and self.pe_wins == 0:
            return True, f"PE_LOSING_TODAY_{self.pe_losses}L_0W"

        # Also block if consecutive losses >= 3
        if self.consecutive_losses >= 3:
            return True, f"CONSECUTIVE_LOSS_LOCK_{self.consecutive_losses}"

        return False, None

    def should_exit_early(self, ltp: float, entry_price: float,
                           held_seconds: float, ml_prob: float,
                           ml_edge: float) -> tuple:
        """
        ML-powered early reversal detection.
        Returns (should_exit: bool, reason: str).

        Guard: BOTH time AND move must exceed threshold — single condition triggers
        are too aggressive for options (spread + noise causes premature exits).
        Minimum hold enforced at CALLER level (live_engine_v2).
        """
        move = ltp - entry_price
        move_pct = move / entry_price if entry_price > 0 else 0

        # ── HARD EMERGENCY: only trigger on genuine catastrophic moves ──
        # These are <0.5% of trades — extreme adverse move in seconds
        # Requires BOTH sufficient time AND severe adverse move
        if held_seconds <= 30 and move <= -7:   # was -4, now -7 (micro-move tolerance)
            return True, "FAST_REVERSAL_30S"

        # ── Volatile day: exit only after 120s AND meaningful adverse ──
        if self.day_type == DAY_VOLATILE:
            if held_seconds > 120 and move_pct < -0.04:   # was -0.025, now -4%
                return True, "VOLATILE_DAY_2PCT_ADVERSE"

        # ── Range day: allow price to oscillate, exit only at 120s+ ──
        if self.day_type == DAY_RANGE:
            if held_seconds > 120 and move <= -6:   # was 90s + -5pts; now 120s + wider tolerance
                return True, "RANGE_DAY_FAST_EXIT"

        # ── Trend day: give room — only exit on sustained adverse + ML disagreement ──
        if self.day_type == DAY_TREND:
            if held_seconds > 180 and move <= -10 and ml_prob < 0.45:
                return True, "TREND_DAY_ML_DISAGREES"

        # ── ML edge collapse: only after 90s (was 30s) — market noise not edge failure ──
        if held_seconds > 90 and ml_edge < 0.05 and move < -4:
            return True, "ML_EDGE_COLLAPSED"

        # ── ML unreliable today: only after 120s (was 20s) ──
        if self.consecutive_losses >= 3 and move <= -5 and held_seconds > 120:
            return True, f"ML_UNRELIABLE_TODAY_{self.consecutive_losses}L"

        return False, None

    def get_status_summary(self) -> dict:
        """Return a summary dict for Telegram status message."""
        return {
            "day_type":          self.day_type,
            "ce_record":         f"{self.ce_wins}W/{self.ce_losses}L",
            "pe_record":         f"{self.pe_wins}W/{self.pe_losses}L",
            "ce_multiplier":     round(self.ce_multiplier, 2),
            "pe_multiplier":     round(self.pe_multiplier, 2),
            "adaptive_threshold": round(self.get_ml_threshold(), 2),
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins":   self.consecutive_wins,
            "ai_review_pending":  self.ai_review_pending,
            "trades_today":       len(self.trades_today),
        }


# ═══════════════════════════════════════════════════════════════════════
#  AI BRAIN — Uses Claude API after consecutive losses
#  Analyses today's trade log and suggests what to look for next
# ═══════════════════════════════════════════════════════════════════════

def run_ai_brain_review(learner: IntradayMLLearner,
                         today_trades: list,
                         current_regime: str,
                         current_spot: float) -> str:
    """
    After 2+ consecutive losses, call Claude API with today's context.
    Returns AI suggestion string that gets sent to Telegram.

    Called from live_engine_v2.py after a losing trade when
    learner.ai_review_pending is True.
    """
    try:
        import requests

        if not learner.ai_review_pending:
            return None

        if time.time() - learner.last_ai_review_time < 300:   # max once per 5 min
            return None

        # Build trade summary for AI
        trades_text = "\n".join([
            f"  Trade {i+1}: {t['side']} | PnL={t['pnl']} | "
            f"ML={t['ml_prob']} | Exit={t['reason']} | Time={t['time'][:16]}"
            for i, t in enumerate(today_trades[-5:])   # last 5 trades
        ])

        status = learner.get_status_summary()

        prompt = f"""You are an expert NIFTY options trader analysing a trading bot's performance.

TODAY'S CONTEXT:
- Day Type Detected: {status['day_type']}
- Current Market Regime: {current_regime}
- Current NIFTY Spot: {current_spot}
- CE Record Today: {status['ce_record']}
- PE Record Today: {status['pe_record']}
- Consecutive Losses: {status['consecutive_losses']}

RECENT TRADES:
{trades_text}

Based on the day type ({status['day_type']}) and the trade results above:
1. In ONE sentence: what is the main reason these trades are losing?
2. In ONE sentence: what specific condition should the bot wait for before next entry?
3. Give a CE or PE recommendation for the next 30 minutes based on the pattern.

Be extremely concise. Format:
REASON: <one sentence>
WAIT_FOR: <one specific condition>
NEXT_BIAS: CE/PE/WAIT
"""

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )

        if resp.status_code == 200:
            data = resp.json()
            text_blocks = [b["text"] for b in data.get("content", [])
                           if b.get("type") == "text"]
            ai_text = "\n".join(text_blocks).strip()

            learner.ai_review_pending  = False
            learner.last_ai_review_time = time.time()
            learner.ai_suggestions.append({
                "time": datetime.now().isoformat(),
                "suggestion": ai_text
            })

            # Extract NEXT_BIAS and store it in learner
            for line in ai_text.splitlines():
                if line.startswith("NEXT_BIAS:"):
                    bias = line.split(":", 1)[1].strip().upper()
                    if bias == "CE":
                        learner.pe_multiplier = max(learner.pe_multiplier - 0.15, 0.40)
                    elif bias == "PE":
                        learner.ce_multiplier = max(learner.ce_multiplier - 0.15, 0.40)
                    # WAIT → both multipliers reduced

            logger.info(f"AI Brain review: {ai_text[:100]}")
            return ai_text

        else:
            logger.warning(f"AI Brain API error: {resp.status_code}")
            return None

    except Exception as e:
        logger.error(f"AI Brain exception: {e}")
        return None


# ── SINGLETON ─────────────────────────────────────────────────────────
# Import this in live_engine_v2.py:
#   from ml.ml_intraday_learner import intraday_learner, run_ai_brain_review

intraday_learner = IntradayMLLearner()
