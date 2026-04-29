# engine/live_engine.py
# FIXED v2 — Production Safe
#
# Fixes applied:
#   FIX-1 : Feature pipeline replaced with feature_config.build_live_features() (28 features)
#   FIX-2 : ORB logic now uses actual market timestamps (9:15–9:30), not cycle_count
#   FIX-3 : DayClassifier integrated at session start (9:45 lock)
#   FIX-4 : IntradayMLLearner.update_candle() wired for every candle
#   FIX-5 : ML threshold sourced from learner.get_ml_threshold() (adaptive, not hardcoded)
#   FIX-6 : PE signal detection added (was CE-only)
#   FIX-7 : check_exit() now delegates to profit_manager.manage_position()

import os
import logging
from datetime import datetime, time as dtime

from ml.predictor_champion import ChampionPredictor
from ml.feature_config import build_live_features, _safe_build_live_features, FEATURE_COLUMNS
from ml.ml_intraday_learner import IntradayMLLearner
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops

logger = logging.getLogger("live_engine")

# ── Market session constants ──────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)   # ORB window: 9:15 – 9:29 (15 candles)
_DAY_CLASS_AT = dtime(9, 45)   # Day classifier locks after 9:44
_MARKET_CLOSE = dtime(15, 30)

# ── Try importing DayClassifier (model may not exist yet) ─────────────
try:
    from ml.day_classifier import DayClassifier
    _DAY_CLASSIFIER_AVAILABLE = True
except Exception as _e:
    logger.warning(f"DayClassifier unavailable: {_e}")
    _DAY_CLASSIFIER_AVAILABLE = False


class LiveEngine:
    """
    Central decision engine.
    Called every loop cycle with the latest rolling candle window.
    Owns: ORB tracking, feature building, ML prediction, exit logic.
    """

    def __init__(self, ctx):
        self.ctx = ctx

        # ── ML Predictor ──────────────────────────────────────────────
        self.predictor = ChampionPredictor()

        # ── Learner reference (from context, built in master_runner) ──
        self.learner: IntradayMLLearner = ctx.ml_learner

        # ── ORB state ─────────────────────────────────────────────────
        self.orb_high: float | None = None
        self.orb_low:  float | None = None
        self.orb_done: bool = False

        # ── Day classifier ────────────────────────────────────────────
        self._day_clf = DayClassifier() if _DAY_CLASSIFIER_AVAILABLE else None
        self._day_classified: bool = False
        self._day_candles_30m: list = []   # raw dicts for classifier input
        self._prev_close: float | None = None

        # ── Intraday state ────────────────────────────────────────────
        self._open_price_set: bool = False

        logger.info("[LiveEngine] Initialized")

    # ══════════════════════════════════════════════════════════════════
    # ORB BUILDER  (timestamp-based, NOT cycle_count)
    # ══════════════════════════════════════════════════════════════════

    def update_orb(self, candle: dict, ts: datetime):
        """
        Feed one OHLC candle dict: {open, high, low, close, volume, ts}
        ORB window: 9:15:00 → 9:29:59  (exactly 15 one-minute candles)
        """
        now = ts.time()

        if self.orb_done:
            return

        if _MARKET_OPEN <= now < _ORB_END:
            # Accumulate opening range
            if self.orb_high is None:
                self.orb_high = candle["high"]
                self.orb_low  = candle["low"]
            else:
                self.orb_high = max(self.orb_high, candle["high"])
                self.orb_low  = min(self.orb_low,  candle["low"])

        elif now >= _ORB_END and not self.orb_done:
            # Lock ORB after 9:30
            self.orb_done = True
            logger.info(
                f"[ORB LOCKED] High={self.orb_high:.2f}  Low={self.orb_low:.2f}"
            )

    # ══════════════════════════════════════════════════════════════════
    # DAY CLASSIFIER  (runs once at 9:45)
    # ══════════════════════════════════════════════════════════════════

    def _maybe_classify_day(self, candle: dict, ts: datetime):
        """
        Collect first-30-min candles, classify once at 9:45.
        Also feeds IntradayMLLearner.update_candle().
        """
        now = ts.time()

        # Feed learner every candle (adaptive threshold)
        self.learner.update_candle(
            close=candle["close"],
            high=candle["high"],
            low=candle["low"],
            ts=ts
        )

        # Collect pre-9:45 candles for day classifier
        if not self._day_classified and now < _DAY_CLASS_AT:
            self._day_candles_30m.append({
                "open":   candle["open"],
                "high":   candle["high"],
                "low":    candle["low"],
                "close":  candle["close"],
                "volume": candle.get("volume", 0),
            })

        # Classify once at 9:45
        if not self._day_classified and now >= _DAY_CLASS_AT:
            self._day_classified = True
            if self._day_clf and len(self._day_candles_30m) >= 10:
                import pandas as pd
                df_30 = pd.DataFrame(self._day_candles_30m)
                day_type = self._day_clf.classify(df_30, self._prev_close)
                logger.info(
                    f"[DAY CLASSIFIER] {day_type} "
                    f"(confidence={self._day_clf.confidence:.2f})"
                )
            else:
                logger.warning("[DAY CLASSIFIER] Skipped (insufficient candles or model missing)")

    # ══════════════════════════════════════════════════════════════════
    # FEATURE BUILDING  (all 28 features via feature_config)
    # ══════════════════════════════════════════════════════════════════

    def build_features(self, df_window) -> dict | None:
        """
        df_window: pandas DataFrame with columns [open, high, low, close, volume]
                   — last N rows from rolling candle buffer, sorted ascending.

        Returns full 28-feature dict or None if insufficient data.
        """
        if df_window is None or len(df_window) < 26:
            return None

        closes  = df_window["close"].tolist()
        opens   = df_window["open"].tolist()
        highs   = df_window["high"].tolist()
        lows    = df_window["low"].tolist()
        volumes = df_window["volume"].tolist() if "volume" in df_window.columns else [0] * len(closes)

        # ── Build signal dict for feature_config ─────────────────────
        # feature_config.build_live_features() requires a `signal` dict
        # with pre-computed EMA/RSI/ATR values as inputs.
        # We compute them here from the candle buffer.

        signal = self._compute_signal_dict(closes, highs, lows, df_window)

        features = _safe_build_live_features(closes, opens, highs, lows, volumes, signal)

        # Validate all 28 features present
        missing = [f for f in FEATURE_COLUMNS if f not in features]
        if missing:
            logger.error(f"[FEATURES] Still missing after build: {missing}")
            return None

        return features

    def _compute_signal_dict(self, closes: list, highs: list, lows: list, df) -> dict:
        """
        Compute EMAs, RSI, ATR, trend_strength for feature_config.
        These must match what the model was trained on exactly.
        """
        import numpy as np

        n = len(closes)

        # EMA20 / EMA50
        def ema(series, span):
            alpha = 2 / (span + 1)
            val = series[0]
            for p in series[1:]:
                val = p * alpha + val * (1 - alpha)
            return val

        ema20 = ema(closes[-min(n, 60):], 20)
        ema50 = ema(closes[-min(n, 100):], 50) if n >= 50 else ema20

        # RSI-14
        if n >= 15:
            gains, losses = [], []
            for i in range(-14, 0):
                diff = closes[i] - closes[i - 1]
                (gains if diff > 0 else losses).append(abs(diff))
            avg_g = np.mean(gains) if gains else 1e-6
            avg_l = np.mean(losses) if losses else 1e-6
            rsi_1m = 100 - (100 / (1 + avg_g / avg_l))
        else:
            rsi_1m = 50.0

        # ATR-14 (Wilder)
        if n >= 15:
            h = np.array(highs[-14:])
            l = np.array(lows[-14:])
            c = np.array(closes[-14:])
            tr = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
                  for i in range(1, 14)]
            atr_val = float(np.mean(tr))
        else:
            atr_val = abs(closes[-1] - closes[-2]) * 14 ** 0.5

        trend_strength = (ema20 - ema50) / closes[-1] if closes[-1] != 0 else 0.0

        return {
            "ema20":          ema20,
            "ema50":          ema50,
            "rsi_1m":         rsi_1m,
            "atr":            max(atr_val, 0.5),
            "trend_strength": trend_strength,
        }

    # ══════════════════════════════════════════════════════════════════
    # ENTRY SIGNAL DETECTION
    # ══════════════════════════════════════════════════════════════════

    def check_entry(self, df_window, ts: datetime) -> dict | None:
        """
        Returns entry signal dict or None.

        Signal dict keys:
            side      : "CE" or "PE"
            ml_prob   : float
            features  : dict (28 features)
            reason    : str
            stop_loss : float
            target    : float
        """
        now = ts.time()

        # ── Time gate ─────────────────────────────────────────────────
        if now < _ORB_END:
            return None   # never trade during ORB construction window
        if now >= dtime(15, 15):
            return None   # no new entries after 15:15

        # ── Day type gate ─────────────────────────────────────────────
        if self._day_clf and self._day_classified:
            if not self._day_clf.should_trade_orb():
                day = self._day_clf.day_type
                logger.debug(f"[GATE] Day type={day} — ORB trading blocked")
                return None

        features = self.build_features(df_window)
        if not features:
            return None

        price = df_window["close"].iloc[-1]

        # ── Adaptive ML threshold from learner ────────────────────────
        threshold = self.learner.get_ml_threshold()

        # ── ORB breakout flags ────────────────────────────────────────
        ce_breakout = (
            self.orb_done and
            self.orb_high is not None and
            price > self.orb_high
        )
        pe_breakout = (
            self.orb_done and
            self.orb_low is not None and
            price < self.orb_low
        )

        # ── ML predictions ────────────────────────────────────────────
        ce_prob = self.predictor.predict(features, "CE")
        pe_prob = self.predictor.predict(features, "PE")

        if ce_prob is None or pe_prob is None:
            logger.warning("[ML] Predictor returned None — skipping cycle")
            return None

        # ── Apply intraday learner adjustments ────────────────────────
        ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(ce_prob, pe_prob, "CE")

        logger.debug(
            f"[ML] CE={ce_adj:.3f}({ce_prob:.3f}) PE={pe_adj:.3f}({pe_prob:.3f}) "
            f"thr={threshold:.3f} ORB_CE={ce_breakout} ORB_PE={pe_breakout}"
        )

        # ── Side selection ────────────────────────────────────────────
        signal = None

        # CE check — ORB relaxes threshold by 0.03
        ce_thr = threshold - 0.03 if ce_breakout else threshold
        if ce_adj >= ce_thr:
            blocked, reason_block = self.learner.is_side_blocked("CE")
            if not blocked:
                reason = "ORB+ML_CE" if ce_breakout else "ML_CE"
                signal = {
                    "side":    "CE",
                    "ml_prob": ce_adj,
                    "features": features,
                    "reason":  reason,
                }

        # PE check (only if CE not triggered — one trade at a time)
        if signal is None:
            pe_thr = threshold - 0.03 if pe_breakout else threshold
            if pe_adj >= pe_thr:
                blocked, reason_block = self.learner.is_side_blocked("PE")
                if not blocked:
                    reason = "ORB+ML_PE" if pe_breakout else "ML_PE"
                    signal = {
                        "side":    "PE",
                        "ml_prob": pe_adj,
                        "features": features,
                        "reason":  reason,
                    }

        if signal is None:
            return None

        # ── Compute entry stops via risk_manager ──────────────────────
        atr_val = features.get("atr", price * 0.01)

        # Derive regime from learner day type
        day_type = self.learner.get_day_type()
        regime = (
            "EXPANSION" if "VOLATILE" in day_type else
            "TREND"     if "TREND" in day_type else
            "RANGE"
        )

        stop_loss, target, stop_pct = compute_entry_stops(price, atr_val, regime)

        signal["stop_loss"] = stop_loss
        signal["target"]    = target
        signal["stop_pct"]  = stop_pct
        signal["regime"]    = regime

        logger.info(
            f"[ENTRY SIGNAL] {signal['side']} | reason={signal['reason']} | "
            f"prob={signal['ml_prob']:.3f} | SL={stop_loss:.2f} | TP={target:.2f}"
        )

        return signal

    # ══════════════════════════════════════════════════════════════════
    # EXIT LOGIC  (delegates to profit_manager)
    # ══════════════════════════════════════════════════════════════════

    def check_exit(
        self,
        position: dict,
        ltp: float,
        held_seconds: float
    ) -> tuple[bool, str]:
        """
        Returns (should_exit: bool, reason: str)

        Also checks:
        - profit_manager trailing/target logic
        - time-based exit
        - learner early-exit signal
        """
        entry     = position["entry"]
        lot_size  = position.get("lot_size", 1)
        stop_loss = position.get("stop_loss", entry * 0.90)
        max_pnl   = position.get("max_pnl", 0.0)
        ml_prob   = position.get("ml_prob", 0.5)

        # ── profit_manager handles trailing + lock system ─────────────
        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=entry,
            ltp=ltp,
            lot_size=lot_size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
        )

        # Update position dict in-place so caller persists new SL
        position["stop_loss"] = new_sl
        position["max_pnl"]   = new_max_pnl

        if pm_reason:
            return True, pm_reason

        # ── Time-based exit (max hold = 300s default) ─────────────────
        max_hold = int(os.getenv("MAX_HOLD_SECONDS", 300))
        if held_seconds > max_hold:
            return True, "TIME_EXIT"

        # ── Learner early exit check ──────────────────────────────────
        ml_edge = ml_prob - 0.5   # simple edge proxy
        early, e_reason = self.learner.should_exit_early(
            ltp=ltp,
            entry_price=entry,
            held_seconds=held_seconds,
            ml_prob=ml_prob,
            ml_edge=ml_edge,
        )
        if early:
            return True, e_reason

        return False, ""

    # ══════════════════════════════════════════════════════════════════
    # MAIN STEP  (called every engine loop cycle)
    # ══════════════════════════════════════════════════════════════════

    def step(self, market_data: dict, ts: datetime | None = None) -> dict | None:
        """
        market_data must contain:
            candle   : latest completed OHLC dict {open,high,low,close,volume,ts}
            df_window: pandas DataFrame of rolling candles (ascending, ≥26 rows)

        Returns entry signal dict or None.
        """
        if ts is None:
            ts = datetime.now()

        candle    = market_data.get("candle")
        df_window = market_data.get("df_window")

        if candle is None or df_window is None:
            return None

        # ── Set open price in learner (once) ──────────────────────────
        now = ts.time()
        if not self._open_price_set and now >= _MARKET_OPEN:
            self.learner.set_open_price(candle["close"])
            self._open_price_set = True

        # ── Update ORB ────────────────────────────────────────────────
        self.update_orb(candle, ts)

        # ── Day classification candle feed ────────────────────────────
        self._maybe_classify_day(candle, ts)

        # ── Entry check ───────────────────────────────────────────────
        return self.check_entry(df_window, ts)
