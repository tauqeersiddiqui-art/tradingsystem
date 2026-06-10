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
import time
import logging
from datetime import datetime, time as dtime

from ml.predictor_champion import ChampionPredictor
from ml.feature_config import build_live_features, _safe_build_live_features, FEATURE_COLUMNS
from ml.ml_intraday_learner import IntradayMLLearner
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx, VWAPAccumulator
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops

logger = logging.getLogger("live_engine")

# ── Market session constants ──────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)   # ORB window: 9:15 – 9:29 (15 candles)

# Zerodha instrument token for NIFTY 50 index (used in ORB reconstruction)
_NIFTY_INDEX_TOKEN = 256265
_DAY_CLASS_AT = dtime(9, 45)   # Day classifier locks after 9:44
_MARKET_CLOSE = dtime(15, 30)

# ── Session filter — no new entries during lunch chop ─────────────────
_LUNCH_START    = dtime(11,  0)
_LUNCH_END      = dtime(12, 30)   # was 14:00 — 12:30-14:00 window recovered

# ── Minimum expected PnL to accept a signal (capital safeguard) ───────
_MIN_EXPECTED_PNL = 150.0

# ── ML floors — per-side thresholds ──────────────────────────────────
# PE: 0.65 (backtest WR=58%, avg+174 at this level)
# CE: 0.78 — raised from 0.70. Model is CE-biased (outputs 0.80-0.96 all day
#     even on bear days). Higher floor forces only truly exceptional signals.
#     On RANGE regime days, CE_ML_FLOOR rises further to 0.85 (see STEP 5).
_MIN_ML_FLOOR    = 0.65   # PE floor
_CE_ML_FLOOR     = 0.78   # CE needs high confidence — model is CE-biased
_REENTRY_COOLDOWN = 180.0  # seconds to wait after any exit before next entry
_CE_ORB_ENABLED  = True   # CE allowed but gated by _CE_ML_FLOOR

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
        self.orb_ce_fired: bool = False   # one-shot per day per side
        self.orb_pe_fired: bool = False

        # ── VWAP accumulator (reset at session open) ──────────────────
        self._vwap = VWAPAccumulator()

        # ── Direction bias (+1 bullish, -1 bearish, 0 unclear) ────────
        self._direction_bias: int = 0

        # ── Day classifier ────────────────────────────────────────────
        self._day_clf = DayClassifier() if _DAY_CLASSIFIER_AVAILABLE else None
        self._day_classified: bool = False
        self._day_candles_30m: list = []   # raw dicts for classifier input
        self._prev_close: float | None = None

        # ── Intraday state ────────────────────────────────────────────
        self._open_price_set: bool = False

        # ── Re-entry cooldown ─────────────────────────────────────────
        self._last_exit_ts: float = 0.0   # epoch seconds of last trade exit
        _COOLDOWN_SECS = 180              # 3-minute cooldown after any exit

        # ── Per-minute dedup guards (learner + VWAP update once per minute) ──
        self._last_classify_minute: datetime | None = None
        self._last_vwap_minute: datetime | None = None

        # ── Dashboard state (updated every cycle for rich display) ────
        self._last_block_reason: str = "WARMING_UP"
        self._last_ce_prob: float    = 0.0
        self._last_pe_prob: float    = 0.0
        self._last_ce_adj: float     = 0.0
        self._last_pe_adj: float     = 0.0
        self._last_features: dict    = {}
        self._ml_history: list       = []   # rolling window for percentile scoring

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
            self.orb_done = True
            if self.orb_high is not None and self.orb_low is not None:
                logger.info(
                    f"[ORB LOCKED] High={self.orb_high:.2f}  Low={self.orb_low:.2f}"
                )
            else:
                logger.info("[ORB LOCKED] Engine started after 9:30 — no ORB data")

    # ══════════════════════════════════════════════════════════════════
    # ORB RECONSTRUCTION  (called once at startup when window is missed)
    # ══════════════════════════════════════════════════════════════════

    def reconstruct_orb_if_needed(self, broker, ts: datetime | None = None) -> None:
        """
        Fetch today's 9:15–9:29 NIFTY 1-minute candles from Zerodha and
        populate orb_high / orb_low so ORB breakout signals work even when
        the engine starts after the window has closed.

        Safe to call at any time:
        - No-op if before market open (9:15).
        - No-op if ORB was already built from live ticks.
        - If startup is *during* the window (9:15–9:29), seeds whatever
          candles are already complete and lets the live feed add the rest.
        - If startup is *after* the window (≥9:30), fetches the full range
          and locks orb_done so no further accumulation occurs.
        - On any API failure, logs a warning and leaves ORB empty rather
          than crashing — ML-only entries still work in that case.
        """
        if ts is None:
            ts = datetime.now()
        now = ts.time()

        # Nothing to do before the market opens
        if now < _MARKET_OPEN:
            return

        # ORB already populated by live ticks — nothing to reconstruct
        if self.orb_high is not None and self.orb_low is not None:
            return

        today        = ts.date()
        orb_start_dt = datetime.combine(today, _MARKET_OPEN)
        # Request up to 9:30 so the Zerodha API includes the full window;
        # we filter strictly to <9:30 below.
        orb_end_dt   = datetime.combine(today, _ORB_END)

        try:
            raw = broker.kite.historical_data(
                _NIFTY_INDEX_TOKEN, orb_start_dt, orb_end_dt,
                "minute", oi=False,
            )
        except Exception as exc:
            logger.warning(
                f"[ORB RECONSTRUCT] Zerodha API call failed: {exc} "
                "— ORB unavailable; ML-only entries still active."
            )
            if now >= _ORB_END:
                self.orb_done = True
            return

        if not raw:
            logger.warning(
                "[ORB RECONSTRUCT] API returned no candles for today's "
                "9:15–9:29 window — ORB unavailable."
            )
            if now >= _ORB_END:
                self.orb_done = True
            return

        # Strip timezone and filter strictly to 9:15:00–9:29:59
        orb_candles = []
        for c in raw:
            dt = c["date"]
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                try:
                    import pytz as _tz
                    dt = dt.astimezone(_tz.timezone("Asia/Kolkata")).replace(tzinfo=None)
                except Exception:
                    dt = dt.replace(tzinfo=None)
            t = dt.time() if hasattr(dt, "time") else None
            if t and _MARKET_OPEN <= t < _ORB_END:
                orb_candles.append(c)

        if not orb_candles:
            logger.warning(
                "[ORB RECONSTRUCT] No candles found in 9:15–9:29 range "
                "(market may have been closed or data unavailable) — ORB unavailable."
            )
            if now >= _ORB_END:
                self.orb_done = True
            return

        highs = [float(c["high"]) for c in orb_candles]
        lows  = [float(c["low"])  for c in orb_candles]
        self.orb_high = max(highs)
        self.orb_low  = min(lows)

        # Lock ORB only when the full window has passed; if we are still
        # inside the window the live update_orb() loop will keep adding.
        if now >= _ORB_END:
            self.orb_done = True

        logger.info(
            f"[ORB RECONSTRUCTED] High={self.orb_high:.2f}  Low={self.orb_low:.2f}"
            f"  (candles={len(orb_candles)}, window=9:15-9:29)"
        )

    # ══════════════════════════════════════════════════════════════════
    # DAY CLASSIFIER  (runs once at 9:45)
    # ══════════════════════════════════════════════════════════════════

    def _maybe_classify_day(self, candle: dict, ts: datetime):
        """
        Collect first-30-min candles, classify once at 9:45.
        Also feeds IntradayMLLearner.update_candle() and accumulates VWAP.
        """
        now = ts.time()

        # Deduplicate per-minute operations — engine_loop fires every second
        # but candles complete once per minute.  Without this guard, the learner
        # and VWAP would accumulate 60 identical entries per minute, inflating
        # first_30min_closes to ~1800 entries and making day-type detection wrong.
        _candle_minute = ts.replace(second=0, microsecond=0)
        _new_minute    = (_candle_minute != self._last_classify_minute)

        # Feed learner once per completed minute
        if _new_minute:
            self._last_classify_minute = _candle_minute
            self.learner.update_candle(
                close=candle["close"],
                high=candle["high"],
                low=candle["low"],
                ts=ts,
            )

        # Accumulate VWAP from market open — once per minute
        if _candle_minute != self._last_vwap_minute:
            self._last_vwap_minute = _candle_minute
            self._vwap.update(
                high=float(candle["high"]),
                low=float(candle["low"]),
                close=float(candle["close"]),
                volume=float(candle.get("volume", 0)),
            )

        # Collect pre-9:45 candles for day classifier — once per minute
        if not self._day_classified and now < _DAY_CLASS_AT and _new_minute:
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

    def build_features(self, df_window, ts=None) -> dict | None:
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

        signal = self._compute_signal_dict(closes, highs, lows, df_window)

        features = _safe_build_live_features(closes, opens, highs, lows, volumes, signal, ts=ts)

        missing = [f for f in FEATURE_COLUMNS if f not in features]
        if missing:
            logger.error(f"[FEATURES] Still missing after build: {missing}")
            return None

        return features

    def _compute_signal_dict(self, closes: list, highs: list, lows: list, df) -> dict:
        """
        Compute all signal indicators for feature_config.
        Includes direction stack: Supertrend, ADX, VWAP bias, EMA alignment.
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
        atr_val = max(atr_val, 0.5)

        trend_strength = (ema20 - ema50) / closes[-1] if closes[-1] != 0 else 0.0

        # ── Supertrend (10/3) over rolling window ─────────────────────
        h_arr = np.array(highs, dtype=float)
        l_arr = np.array(lows, dtype=float)
        c_arr = np.array(closes, dtype=float)

        st_dir_arr, st_line_arr = _compute_supertrend(h_arr, l_arr, c_arr, period=10, multiplier=3.0)
        last_st_dir  = int(st_dir_arr[-1])
        last_st_line = float(st_line_arr[-1])
        st_dist      = (closes[-1] - last_st_line) / closes[-1] if closes[-1] != 0 else 0.0

        # ── ADX (14) over rolling window ─────────────────────────────
        adx_arr, di_plus, di_minus = _compute_adx(h_arr, l_arr, c_arr, period=14)
        last_adx     = float(adx_arr[-1])
        last_di_plus = float(di_plus[-1])
        last_di_min  = float(di_minus[-1])

        # ── VWAP bias ─────────────────────────────────────────────────
        vwap_val     = self._vwap.value
        price_vs_vwap = (closes[-1] - vwap_val) / closes[-1] if (closes[-1] != 0 and vwap_val > 0) else 0.0

        # ── Update direction bias ─────────────────────────────────────
        # Paper trading: SuperTrend is the primary direction gate.
        # VWAP agreement is tracked but not required — re-enable strict
        # gate (both must agree) when switching to live capital.
        vwap_confirms = (last_st_dir == 1 and price_vs_vwap > 0) or \
                        (last_st_dir == -1 and price_vs_vwap < 0)
        if last_st_dir == 1:
            self._direction_bias = 1
        elif last_st_dir == -1:
            self._direction_bias = -1
        else:
            self._direction_bias = 0
        self._vwap_confirms = vwap_confirms  # stored for dashboard/logging

        return {
            "ema20":          ema20,
            "ema50":          ema50,
            "rsi_1m":         rsi_1m,
            "atr":            atr_val,
            "trend_strength": trend_strength,
            # Direction stack
            "supertrend_dir":  float(last_st_dir),
            "supertrend_dist": float(np.clip(st_dist, -0.05, 0.05)),
            "price_vs_vwap":   float(np.clip(price_vs_vwap, -0.05, 0.05)),
            "adx":             float(np.clip(last_adx, 0, 100)),
            "di_spread":       float(np.clip(last_di_plus - last_di_min, -60, 60)),
            "ema_alignment":   float(1.0 if ema20 > ema50 else -1.0),
        }

    # ══════════════════════════════════════════════════════════════════
    # ENTRY SIGNAL DETECTION
    # ══════════════════════════════════════════════════════════════════

    def check_entry(self, df_window, ts: datetime) -> dict | None:
        """
        Returns entry signal dict or None.
        Features + ML probs are ALWAYS computed (even when blocked) so the
        dashboard always shows live values regardless of session state.
        """
        import numpy as np
        now = ts.time()

        # ══ STEP 1: Always build features + compute ML probs ════════════
        # This runs unconditionally so the Telegram dashboard always shows
        # live CE/PE probabilities even during ORB build or lunch filter.
        features = self.build_features(df_window, ts)
        if features:
            self._last_features = features
            _ce_p = self.predictor.predict(features, "CE")
            _pe_p = self.predictor.predict(features, "PE")
            if _ce_p is not None:
                self._last_ce_prob = float(_ce_p)
                self._ml_history.append(float(_ce_p))
                if len(self._ml_history) > 500:
                    self._ml_history.pop(0)
            if _pe_p is not None:
                self._last_pe_prob = float(_pe_p)
            if _ce_p is not None and _pe_p is not None:
                _ce_a, _pe_a = self.learner.get_adjusted_ml_prob(_ce_p, _pe_p, "CE")
                self._last_ce_adj = float(_ce_a)
                self._last_pe_adj = float(_pe_a)

        # ══ STEP 2: Session gates ════════════════════════════════════════
        if now < _ORB_END:
            self._last_block_reason = "ORB_BUILD (9:15-9:30)"
            return None
        if now >= dtime(15, 15):
            self._last_block_reason = "MARKET_CLOSING (after 15:15)"
            return None
        # LUNCH_FILTER disabled — re-enable when switching to live capital
        # if _LUNCH_START <= now < _LUNCH_END:
        #     self._last_block_reason = "LUNCH_FILTER (11:00-12:30)"
        #     return None
        if not features:
            self._last_block_reason = "INSUFFICIENT_DATA (<26 candles)"
            return None

        # ── Re-entry cooldown ────────────────────────────────────────────
        _secs_since_exit = time.time() - self._last_exit_ts
        if self._last_exit_ts > 0 and _secs_since_exit < _REENTRY_COOLDOWN:
            _wait = int(_REENTRY_COOLDOWN - _secs_since_exit)
            self._last_block_reason = f"COOLDOWN ({_wait}s remaining)"
            return None

        # ══ STEP 3: Direction gate ═══════════════════════════════════════
        direction_bias = self._direction_bias
        vwap_confirms  = getattr(self, "_vwap_confirms", False)
        _dir_str = "BULL" if direction_bias == 1 else ("BEAR" if direction_bias == -1 else "NONE")
        _vwap_str = "VWAP✓" if vwap_confirms else "VWAP✗"
        logger.info(f"[DIRECTION] dir={_dir_str} ST={features.get('supertrend_dir',0):.0f} {_vwap_str} pvwap={features.get('price_vs_vwap',0):.4f} CE={self._last_ce_adj:.3f} PE={self._last_pe_adj:.3f}")
        if direction_bias == 0:
            self._last_block_reason = "NO_DIRECTION (ST=0)"
            return None

        # ══ STEP 4: Signal thresholds + ORB breakout ════════════════════
        orb_ok = bool(
            self._day_clf and self._day_classified and self._day_clf.should_trade_orb()
        )

        price      = df_window["close"].iloc[-1]
        threshold  = max(self.learner.get_ml_threshold(), _MIN_ML_FLOOR)
        # CE uses a higher independent floor — weaker CE signals skipped
        ce_floor   = max(threshold, _CE_ML_FLOOR)

        ce_breakout = (
            self.orb_done and self.orb_high is not None and
            price > self.orb_high and not self.orb_ce_fired
        )
        pe_breakout = (
            self.orb_done and self.orb_low is not None and
            price < self.orb_low and not self.orb_pe_fired
        )
        if ce_breakout: self.orb_ce_fired = True
        if pe_breakout: self.orb_pe_fired = True

        # Pull from cached adjusted probs (set in Step 1)
        ce_adj = self._last_ce_adj
        pe_adj = self._last_pe_adj

        # Hard direction gate — zero out opposite side
        if direction_bias != 1:
            ce_adj = 0.0
        if direction_bias != -1:
            pe_adj = 0.0

        logger.debug(
            f"[ML] CE={ce_adj:.3f}({self._last_ce_prob:.3f}) "
            f"PE={pe_adj:.3f}({self._last_pe_prob:.3f}) "
            f"thr={threshold:.3f} ORB_CE={ce_breakout} ORB_PE={pe_breakout}"
        )

        # ══ STEP 5: Side selection ═══════════════════════════════════════
        signal  = None
        dir_str = "BULL" if direction_bias == 1 else "BEAR"
        self._last_block_reason = (
            f"ML_BELOW_THR ({dir_str} | CE={ce_adj:.2f} PE={pe_adj:.2f} thr={threshold:.2f})"
        )

        # CE entry — requires BULL direction + VWAP confirmation.
        # VWAP✗ on a BULL ST signal = false bounce, not a real uptrend.
        # On RANGE days, pure ML_CE floor rises to 0.85 (model fires too freely on chop).
        if not _CE_ORB_ENABLED:
            ce_adj = 0.0
        else:
            # Block CE if VWAP does not confirm direction (price below VWAP)
            if direction_bias == 1 and not vwap_confirms:
                self._last_block_reason = "CE_VWAP_FAIL (BULL but price below VWAP)"
                ce_adj = 0.0
            else:
                # ── Higher-timeframe CE confirmation (30m EMA gate) ───────
                # 75% of CE losses entered when NIFTY was in a sustained downtrend
                # on longer timeframes despite a brief 1m SuperTrend=+1 signal.
                # Gate: require the 30-candle EMA (≈30m on 1m data) to be above
                # the 60-candle EMA. This confirms the bounce is part of a real
                # uptrend, not a dead-cat bounce inside a falling market.
                _htf_bullish = True  # default: pass if insufficient data
                _closes = df_window["close"].values
                if len(_closes) >= 60:
                    import numpy as np
                    _ema30 = float(np.mean(_closes[-30:]))   # simple mean as fast EMA proxy
                    _ema60 = float(np.mean(_closes[-60:]))
                    _htf_bullish = _ema30 > _ema60
                if not _htf_bullish:
                    self._last_block_reason = "CE_HTF_FAIL (30m EMA bearish vs 60m)"
                    ce_adj = 0.0
                else:
                    _pure_ml_ce = not ce_breakout
                    # On RANGE days, require extra-high CE confidence — model is noisy in chop
                    _regime_str = str(self.learner.get_day_type()).upper()
                    _is_range   = "RANGE" in _regime_str or _regime_str in ("UNKNOWN", "")
                    _ce_floor   = 0.85 if (_pure_ml_ce and _is_range) else _CE_ML_FLOOR
                    ce_thr = max(threshold - 0.03 if (ce_breakout and orb_ok) else threshold, _ce_floor)
                    if ce_adj >= ce_thr:
                        blocked, reason_block = self.learner.is_side_blocked("CE")
                        if blocked:
                            self._last_block_reason = f"CE_BLOCKED ({reason_block})"
                        else:
                            reason = "ML_CE" if _pure_ml_ce else "ORB+ML_CE"
                            self._last_block_reason = f"SIGNAL_FIRE ({reason})"
                            signal = {"side": "CE", "ml_prob": ce_adj,
                                      "features": features, "reason": reason}
                    else:
                        self._last_block_reason = (
                            f"CE_WEAK (ce_adj={ce_adj:.2f} < floor={ce_thr:.2f})"
                        )

        # PE (only if CE not triggered)
        if signal is None:
            pe_thr = threshold - 0.03 if (pe_breakout and orb_ok) else threshold
            if pe_adj >= pe_thr:
                blocked, reason_block = self.learner.is_side_blocked("PE")
                if blocked:
                    self._last_block_reason = f"PE_BLOCKED ({reason_block})"
                else:
                    reason = "ORB+ML_PE" if pe_breakout else "ML_PE"
                    self._last_block_reason = f"SIGNAL_FIRE ({reason})"
                    signal = {"side": "PE", "ml_prob": pe_adj,
                              "features": features, "reason": reason}

        if signal is None:
            return None

        # ══ STEP 6: Risk + expected PnL guard ═══════════════════════════
        atr_val  = features.get("atr", price * 0.01)
        day_type = self.learner.get_day_type()
        regime   = (
            "EXPANSION" if "VOLATILE" in day_type else
            "TREND"     if "TREND"    in day_type else
            "RANGE"
        )
        stop_loss, target, stop_pct = compute_entry_stops(price, atr_val, regime)
        signal.update(stop_loss=stop_loss, target=target,
                      stop_pct=stop_pct, regime=regime)

        lot_size     = getattr(getattr(self.ctx, "config", None), "LOT_SIZE", 65)
        exp_win      = (target - price) * lot_size
        exp_loss     = (price - stop_loss) * lot_size
        expected_pnl = signal["ml_prob"] * exp_win - (1 - signal["ml_prob"]) * exp_loss
        if expected_pnl < _MIN_EXPECTED_PNL:
            logger.info(
                f"[PNL GUARD] Expected Rs{expected_pnl:.0f} < Rs{_MIN_EXPECTED_PNL} — skipping"
            )
            self._last_block_reason = f"PNL_GUARD (exp=Rs{expected_pnl:.0f})"
            return None

        logger.info(
            f"[ENTRY SIGNAL] {signal['side']} | reason={signal['reason']} | "
            f"prob={signal['ml_prob']:.3f} | SL={stop_loss:.2f} | TP={target:.2f} | "
            f"ExpPnL=Rs{expected_pnl:.0f}"
        )
        return signal

    def _ml_percentile(self, prob: float) -> int:
        """Percentile rank of prob within today's ML history."""
        if not self._ml_history:
            return 0
        import numpy as np
        return int(np.sum(np.array(self._ml_history) <= prob) / len(self._ml_history) * 100)

    # ══════════════════════════════════════════════════════════════════
    # MARKET STATE  (snapshot for dashboard rendering)
    # ══════════════════════════════════════════════════════════════════

    def get_market_state(self, ts: datetime | None = None) -> dict:
        """
        Returns a snapshot of current market internals.
        Safe to call every cycle — never triggers any trading logic.
        """
        if ts is None:
            ts = datetime.now()
        now = ts.time()

        f = self._last_features or {}
        ema20 = f.get("ema20", 0.0)
        ema50 = f.get("ema50", 0.0)

        # Session label
        if now < _MARKET_OPEN:
            session = "PRE-MARKET"
        elif now < _ORB_END:
            session = "ORB BUILD (9:15-9:30)"
        elif _LUNCH_START <= now < _LUNCH_END:
            session = "LUNCH FILTER"
        elif now >= dtime(15, 15):
            session = "CLOSING"
        else:
            session = "ACTIVE"

        return {
            "ts":              ts,
            "session":         session,
            "ema20":           ema20,
            "ema50":           ema50,
            "ema_direction":   "UP" if ema20 > ema50 else "DOWN",
            "rsi_1m":          f.get("rsi_1m", 50.0),
            "atr":             f.get("atr", 0.0),
            "supertrend_dir":  int(f.get("supertrend_dir", 0)),
            "supertrend_dist": f.get("supertrend_dist", 0.0),
            "vwap":            self._vwap.value,
            "price_vs_vwap":   f.get("price_vs_vwap", 0.0),
            "adx":             f.get("adx", 0.0),
            "di_spread":       f.get("di_spread", 0.0),
            "direction_bias":  self._direction_bias,
            "orb_high":        self.orb_high,
            "orb_low":         self.orb_low,
            "orb_done":        self.orb_done,
            "ce_prob":         self._last_ce_prob,
            "pe_prob":         self._last_pe_prob,
            "ce_adj":          self._last_ce_adj,
            "pe_adj":          self._last_pe_adj,
            "ml_threshold":    max(self.learner.get_ml_threshold(), _MIN_ML_FLOOR),
            "ce_threshold":    max(self.learner.get_ml_threshold(), _CE_ML_FLOOR),
            "block_reason":    self._last_block_reason,
            # Scoring
            "ml_percentile":   self._ml_percentile(max(self._last_ce_adj, self._last_pe_adj)),
            "ml_score":        round(
                max(self._last_ce_adj, self._last_pe_adj) * 50
                + self._ml_percentile(max(self._last_ce_adj, self._last_pe_adj)) / 2,
                1
            ),
            "score_required":  40.0,   # ≈ 0.62*50 + median_percentile/2
        }

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
            target=position.get("target"),
        )

        # Update position dict in-place so caller persists new SL
        position["stop_loss"] = new_sl
        position["max_pnl"]   = new_max_pnl

        if pm_reason:
            return True, pm_reason

        # ── Time-based exit (max hold = 300s default) ─────────────────
        # Match backtest: only time-exit WEAK trades. Let runners breathe so
        # the trailing/drawdown logic (the main edge) can work.
        max_hold = int(os.getenv("MAX_HOLD_SECONDS", 300))
        if held_seconds > max_hold and new_max_pnl < 100:
            return True, "TIME_EXIT_WEAK"

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

        # ── Set open price in learner (once) + reset VWAP ────────────
        now = ts.time()
        if not self._open_price_set and now >= _MARKET_OPEN:
            self.learner.set_open_price(candle["close"])
            self._vwap.reset()           # fresh VWAP accumulator for new session
            self._direction_bias = 0     # unknown direction at session start
            self._open_price_set = True

        # ── Update ORB ────────────────────────────────────────────────
        self.update_orb(candle, ts)

        # ── Day classification candle feed ────────────────────────────
        self._maybe_classify_day(candle, ts)

        # ── Entry check ───────────────────────────────────────────────
        return self.check_entry(df_window, ts)
