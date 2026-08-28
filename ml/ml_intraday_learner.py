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
        # Full-day tracking for periodic re-classification. The 9:45 lock is
        # based only on the first 30 min — a day that opens choppy but then
        # trends hard (e.g. BankNifty -400pts) stays labeled RANGE forever,
        # blocking every ML entry via RANGE_REGIME_SKIP. These accumulate the
        # whole day so we can re-run the classifier and upgrade RANGE -> TREND.
        self.day_closes   = []
        self.day_high     = 0.0
        self.day_low      = 9999999.0
        self._last_day_minute   = None   # dedup: one full-day sample per minute
        self._last_reclass_ts   = None   # throttle re-classification

        # Dedup guard: only accept one candle per minute in the 30-min window
        self._last_candle_minute = None

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
        Also accumulates full-day data for periodic re-classification, so a
        day that opens choppy but later trends hard (e.g. BankNifty -400pts)
        gets re-labeled from RANGE_DAY to TREND/GAP/VOLATILE.
        """
        candle_minute = ts.replace(second=0, microsecond=0)

        # ── Full-day accumulation (always, once per minute) ──────────────
        if candle_minute != self._last_day_minute:
            self._last_day_minute = candle_minute
            self.day_closes.append(close)
            self.day_high = max(self.day_high, high)
            self.day_low  = min(self.day_low, low)

        if self.day_type_locked:
            return

        if ts.hour == 9 and ts.minute < 45:
            if candle_minute != self._last_candle_minute:
                self._last_candle_minute = candle_minute
                self.first_30min_closes.append(close)
                self.first_30min_high = max(self.first_30min_high, high)
                self.first_30min_low  = min(self.first_30min_low, low)

        elif ts.hour == 9 and ts.minute >= 45 and not self.day_type_locked:
            self._detect_day_type()

    def maybe_reclassify(self, ts: datetime):
        """
        Re-run day-type detection on the full day's data so far.
        Only upgrades: RANGE → GAP/TREND/VOLATILE (never downgrades back to
        RANGE, which would re-block entries).  Fires at most once every 30 min
        on :00/:30 boundaries, starting at 10:00.
        """
        if not self.day_type_locked or ts.hour < 10:
            return
        if ts.minute not in (0, 30):
            return
        _minute = ts.hour * 60 + ts.minute
        if self._last_reclass_ts is not None and (_minute - self._last_reclass_ts) < 30:
            return
        self._last_reclass_ts = _minute

        if len(self.day_closes) < 5 or not self.open_price:
            return

        # Re-run same heuristic on full-day data
        closes = self.day_closes
        last   = closes[-1]
        rng    = self.day_high - self.day_low
        open_p = self.open_price
        move_pct = (last - open_p) / open_p if open_p > 0 else 0
        range_pct = rng / open_p if open_p > 0 else 0
        gap_size = abs(open_p - closes[0]) / open_p if open_p > 0 else 0

        # trending: split day so far in half
        mid = len(closes) // 2
        first_half = closes[:mid]
        second_half = closes[mid:]
        trending = (
            (min(second_half) > max(first_half)) or
            (max(second_half) < min(first_half))
        ) if len(closes) >= 6 else False

        # Same classification thresholds as _detect_day_type
        new_type = DAY_UNKNOWN
        if range_pct > 0.006:
            new_type = DAY_VOLATILE
        elif gap_size > 0.005:
            new_type = DAY_GAP
        elif trending and abs(move_pct) > 0.003:
            new_type = DAY_TREND
        else:
            new_type = DAY_RANGE

        # Upgrade-only: never downgrade a directional day back to RANGE.
        # RANK: UNKNOWN(0) < RANGE(1) < GAP(2) < TREND(3) < VOLATILE(4)
        _rank = {DAY_UNKNOWN:0, DAY_RANGE:1, DAY_GAP:2, DAY_TREND:3, DAY_VOLATILE:4}
        if _rank.get(new_type, 0) > _rank.get(self.day_type, 0):
            old = self.day_type
            self.day_type = new_type
            logger.info(
                f"[RECLASSIFY] {old} -> {new_type} | "
                f"range_pct={range_pct:.3f} move_pct={move_pct:.3f} "
                f"gap={gap_size:.3f} trending={trending}"
            )

    def backfill_first_30m(self, candles: list):
        """
        Late-start recovery: engine started after 09:45, so the normal
        update_candle() 30-min collection window was missed. Feed the
        first-30-min candles from historical data and classify now.
        """
        if self.day_type_locked or not candles:
            return
        first = candles[0]
        if self.open_price is None:
            self.open_price = float(first.get("close", 0))
        for c in candles:
            self.first_30min_closes.append(float(c.get("close", 0)))
            self.first_30min_high = max(self.first_30min_high, float(c.get("high", 0)))
            self.first_30min_low  = min(self.first_30min_low,  float(c.get("low", 0)))
            # Also seed full-day trackers so a mid-day restart can reclassify.
            self.day_closes.append(float(c.get("close", 0)))
            self.day_high = max(self.day_high, float(c.get("high", 0)))
            self.day_low  = min(self.day_low,  float(c.get("low", 0)))
        self._detect_day_type()

    def backfill_full_day(self, candles: list):
        """
        Late-start recovery after a mid-day restart: seed the full-day trackers
        with ALL of today's candles (9:15 -> now), not just the first 30 min,
        so periodic re-classification has the true day range/move to work with.
        Preserves the 9:45 lock if the 30-min window was already backfilled.
        """
        if not candles:
            return
        for c in candles:
            self.day_closes.append(float(c.get("close", 0)))
            self.day_high = max(self.day_high, float(c.get("high", 0)))
            self.day_low  = min(self.day_low,  float(c.get("low", 0)))
            if self.open_price is None:
                self.open_price = float(c.get("close", 0))
        logger.info(
            f"[BACKFILL_FULL_DAY] {len(candles)} candles | "
            f"day_high={self.day_high:.1f} day_low={self.day_low:.1f} "
            f"day_type={self.day_type}"
        )

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

        # Gap detection: compare today's open to the first close seen at session
        # start.  True gap vs yesterday requires prev_close which is not tracked
        # here; this approximation catches large opening gaps well enough.
        gap_size = abs(open_p - closes[0]) / open_p if (closes and open_p > 0) else 0

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

        if range_pct > 0.006:         # >0.6% range in 30 min = volatile (~144pts on 24000)
            self.day_type = DAY_VOLATILE
        elif gap_size > 0.005:        # >0.5% gap = gap day
            self.day_type = DAY_GAP
        elif trending and abs(move_pct) > 0.003:  # >0.3% directional (was 0.4% — too strict)
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
          VOLATILE_DAY: +0.04 (market unpredictable — be very selective)
          GAP_DAY:      +0.02 (wait for gap fill first)
          RANGE_DAY:    +0.03 (reversals are tricky)
          TREND_DAY:    +0.00 (follow the trend — base threshold is fine)
        """
        base = self.current_threshold
        day_adj = {
            DAY_VOLATILE: 0.04,
            DAY_GAP:      0.02,
            DAY_RANGE:    0.03,
            DAY_TREND:   -0.01,
            DAY_UNKNOWN:  0.02,
        }.get(self.day_type, 0.02)

        adaptive_threshold = base + day_adj

        # Keep thresholds realistic for intraday ML. The 0.45-0.56 clamp was
        # tuned for the OLD entry-quality label semantics (model output was
        # "favorable excursion prob", heavily CE-biased). The forward-direction
        # model outputs P(net >= DIRECTION_MOVE_PTS move in next 5 bars) —
        # well-calibrated, so it reads ~0.15-0.25 on quiet range days and only
        # rises toward 0.5+ when it detects an actual move setup. Env override
        # lets dry-run testing lower the floor so the new model can fire.
        _floor = float(os.getenv("ML_THRESHOLD_FLOOR", "0.45"))
        _ceil  = float(os.getenv("ML_THRESHOLD_CEIL", "0.56"))
        adaptive_threshold = max(_floor, min(adaptive_threshold, _ceil))

        return round(adaptive_threshold, 3)

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
                self.ce_multiplier = min(self.ce_multiplier + 0.03, 1.15)
            else:
                self.pe_multiplier = min(self.pe_multiplier + 0.04, 1.15)

            # Lower threshold (ML is working today)
            self.current_threshold = max(
                self.current_threshold - 0.01, self.base_threshold)

        else:
            self.consecutive_losses += 1
            self.consecutive_wins    = 0
            self.last_loss_side      = side

            # Reduce losing side's multiplier
            if side == "CE":
                self.ce_multiplier = max(self.ce_multiplier - 0.05, 0.75)
            else:
                self.pe_multiplier = max(self.pe_multiplier - 0.05, 0.75)

            # Raise threshold (ML was wrong — need higher confidence)
            # Cap at 0.60 to keep system usable
            self.current_threshold = min(
                self.current_threshold + 0.02, 0.56)

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
        if side == "CE" and self.ce_losses >= 4 and self.ce_wins == 0:
            return True, f"CE_LOSING_TODAY_{self.ce_losses}L_0W"
        if side == "PE" and self.pe_losses >= 4 and self.pe_wins == 0:
            return True, f"PE_LOSING_TODAY_{self.pe_losses}L_0W"

        # Block after 4 consecutive losses (was 2 — too aggressive, kills session)
        if self.consecutive_losses >= 4:
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
            if held_seconds > 180 and move <= -8:   # was 90s + -5pts; now 120s + wider tolerance
                return True, "RANGE_DAY_FAST_EXIT"

        # ── Trend day: give room — only exit on sustained adverse + ML disagreement ──
        if self.day_type == DAY_TREND:
            if held_seconds > 180 and move <= -10 and ml_prob < 0.45:
                return True, "TREND_DAY_ML_DISAGREES"

        # ── ML edge collapse: only after 90s (was 30s) — market noise not edge failure ──
        if held_seconds > 150 and ml_edge < 0.03 and move < -6:
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
    After 2+ consecutive losses, call an OpenAI-compatible LLM with today's
    context and return a concise suggestion string for Telegram.

    Config (env):
        LLM_API_KEY   — required; if missing this is a silent no-op
        LLM_BASE_URL  — OpenAI-compatible base, default https://api.openai.com/v1
        LLM_MODEL     — default gpt-4o-mini

    SAFETY: this is advisory only. The strongest action it can take is to
    REDUCE a side's multiplier (make the bot more selective). It can never
    increase size, loosen a stop, or force a trade — so it cannot increase loss.
    """
    try:
        import requests

        if os.getenv("AI_REVIEW_ENABLED", "1") != "1":
            learner.ai_review_pending = False
            return None

        if not learner.ai_review_pending:
            return None

        if time.time() - learner.last_ai_review_time < 300:   # max once per 5 min
            return None

        api_key  = os.getenv("LLM_API_KEY", "").strip()
        # Treat the placeholder key as "not configured" so it stays a no-op.
        if not api_key or api_key.startswith("freellmapi-xxx"):
            logger.info("[AI BRAIN] LLM_API_KEY not set — skipping AI review")
            learner.ai_review_pending = False
            return None
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model    = os.getenv("LLM_MODEL", "gpt-4o-mini")

        # Build trade summary for the model
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
            f"{base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "max_tokens": 200,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You are a concise, risk-first intraday options trading analyst."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=15,
        )

        if resp.status_code == 200:
            data = resp.json()
            ai_text = (
                data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            )
            if not ai_text:
                logger.warning("[AI BRAIN] Empty response content")
                return None

            learner.ai_review_pending  = False
            learner.last_ai_review_time = time.time()
            learner.ai_suggestions.append({
                "time": datetime.now().isoformat(),
                "suggestion": ai_text
            })

            # Extract NEXT_BIAS — only ever REDUCES the opposite side's
            # multiplier (more selective), never boosts or sizes up.
            for line in ai_text.splitlines():
                if line.strip().upper().startswith("NEXT_BIAS:"):
                    bias = line.split(":", 1)[1].strip().upper()
                    if bias.startswith("CE"):
                        learner.pe_multiplier = max(learner.pe_multiplier - 0.05, 0.75)
                    elif bias.startswith("PE"):
                        learner.ce_multiplier = max(learner.ce_multiplier - 0.05, 0.75)
                    else:  # WAIT → trim both, trade less
                        learner.ce_multiplier = max(learner.ce_multiplier - 0.05, 0.75)
                        learner.pe_multiplier = max(learner.pe_multiplier - 0.05, 0.75)

            logger.info(f"[AI BRAIN] review: {ai_text[:120]}")
            return ai_text

        logger.warning(f"[AI BRAIN] API error: {resp.status_code} {resp.text[:200]}")
        return None

    except Exception as e:
        logger.error(f"[AI BRAIN] exception: {e}")
        return None


# ── SINGLETON ─────────────────────────────────────────────────────────
# Import this in live_engine_v2.py:
#   from ml.ml_intraday_learner import intraday_learner, run_ai_brain_review

intraday_learner = IntradayMLLearner()
