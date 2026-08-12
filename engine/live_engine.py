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
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx, VWAPAccumulator, atr_wilder as _atr_wilder, rsi_wilder as _rsi_wilder
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops
from engine.intelligence.phase55_filter import (
    Phase55FilterConfig,
    evaluate_phase55_filter,
    infer_regime_from_features,
)
from engine.intelligence.phase55_telemetry import (
    Phase55Telemetry,
    empty_phase55_telemetry_snapshot,
)

logger = logging.getLogger("live_engine")

# ── Market session constants ──────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)   # ORB window: 9:15 – 9:29 (15 candles)

# Zerodha instrument token for BANKNIFTY index (used in ORB reconstruction)
_BANKNIFTY_INDEX_TOKEN = 260105
_DAY_CLASS_AT = dtime(9, 45)   # Day classifier locks after 9:44
_MARKET_CLOSE = dtime(15, 30)

# ── Session filter — no new entries during lunch chop ─────────────────
_LUNCH_START    = dtime(11,  0)
_LUNCH_END      = dtime(12, 30)   # was 14:00 — 12:30-14:00 window recovered

# ── Minimum expected PnL to accept a signal (capital safeguard) ───────
_MIN_EXPECTED_PNL = 150.0

# ── ML floors — per-side thresholds ──────────────────────────────────
# FIX 2026-07-03: Old thresholds (0.78 CE / 0.65 PE) were tuned for the
# SATURATED model (outputs 0.80–0.96 all day).  trainer_v3 de-saturation
# produces honest probabilities in the 0.40–0.75 range.  The Platt
# calibrator squash bug (now fixed in predictor_champion.py) was hiding
# this mismatch.  Use the model's own saved thresholds (0.72 CE / 0.64 PE)
# as the floors so signals can actually fire on de-saturated outputs.
_MIN_ML_FLOOR      = 0.55   # PE floor (model threshold=0.64; learner adaptive adds ~0.02)
_CE_ML_FLOOR       = 0.65   # CE floor (model threshold=0.72; learner adaptive adds ~0.02)
_CE_RANGE_DAY_FLOOR = float(os.getenv("CE_ML_RANGE_FLOOR", "0.75"))  # stricter CE floor on RANGE days
# Edge margin for predict-first path
_ML_EDGE_MARGIN    = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
# Re-entry cooldown is config-driven (Config.REENTRY_COOLDOWN, default 300s);
# see LiveEngine.__init__ self._reentry_cooldown.

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
        # ORB reconstruction fault-tolerance (Phase 7 hardening).
        # Status: NONE (never attempted) / VALID / RETRYING / FAILED / UNAVAILABLE.
        self.orb_status: str = "NONE"
        self.orb_reconstruct_attempts: int = 0
        self.orb_last_error: str = ""
        _orb_cfg = getattr(ctx, "config", None)
        self._orb_retries: int = max(1, int(getattr(_orb_cfg, "ORB_RECONSTRUCT_RETRIES", 3)))
        self._orb_backoff_base: float = max(1.0, float(getattr(_orb_cfg, "ORB_RECONSTRUCT_BACKOFF", 5.0)))
        self._orb_min_candles: int = max(1, int(getattr(_orb_cfg, "ORB_MIN_CANDLES", 10)))

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
        _cfg = getattr(ctx, "config", None)
        # Config-driven (defaults match config.py). Raised 180->300 so a
        # stopped option is not re-entered while still reversing.
        self._reentry_cooldown: float = float(getattr(_cfg, "REENTRY_COOLDOWN", 300))
        self._phase55_config = Phase55FilterConfig.from_config(_cfg)
        self._phase55_telemetry = (
            Phase55Telemetry.from_config(_cfg)
            if bool(getattr(_cfg, "ENABLE_PHASE55_TELEMETRY", True))
            else None
        )
        # Lunch filter OFF for dry-run; flip LUNCH_FILTER_ENABLED=1 for live.
        self._lunch_enabled: bool = bool(getattr(_cfg, "LUNCH_FILTER_ENABLED", False))
        # Higher-timeframe (5m) SuperTrend direction — anti-noise entry gate.
        self._htf5_dir: int = 0
        # ── PREDICT-FIRST direction selection ─────────────────────────
        # ML models CHOOSE the direction (argmax of ce/pe probability) and structure
        # (5m trend + VWAP) only CONFIRMS it — instead of 1m SuperTrend choosing
        # and ML rubber-stamping. Requires the v3 directional models (both sides
        # trained on all bars).
        self._predict_first: bool = True
        # Minimum gap between the two sides' probs to claim a directional
        # edge. If |ce - pe| < margin, conviction is too low → no trade.
        self._ml_edge_margin: float = float(os.getenv("ML_EDGE_MARGIN", "0.15"))

        # ── Per-minute dedup guards (learner + VWAP update once per minute) ──
        self._last_classify_minute: datetime | None = None
        self._last_vwap_minute: datetime | None = None

        # ── Dashboard state (updated every cycle for rich display) ────
        self._last_block_reason: str = "WARMING_UP"
        self._last_ce_prob: float    = 0.0
        self._last_pe_prob: float    = 0.0
        self._last_ce_adj: float     = 0.0
        self._last_pe_adj: float     = 0.0
        self._last_predict_error: str = ""
        self._last_features: dict    = {}
        self._last_phase55_decision: dict = {
            "enabled": self._phase55_config.enabled,
            "filter_used": "none",
            "trade_blocked": False,
            "direction": "",
            "telemetry_id": "",
            "shadow_attached": False,
            "reason": "disabled" if not self._phase55_config.enabled else "",
            "recommendation": "phase55_disabled" if not self._phase55_config.enabled else "",
        }
        self._ml_history: list       = []   # rolling window for percentile scoring

        # F3 — block analytics (reset daily)
        self._block_counts: dict     = {}
        self._block_date              = None
        self._last_counted_key: str  = ""
        # F4 — ML edge |CE_adj - PE_adj|
        self._last_ml_edge: float    = 0.0

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

        Fault-tolerant (Phase 7):
        - exponential backoff retries on every transient failure
        - validates candle count, timestamps and window boundaries
        - rejects malformed candle dicts (never fabricates values)
        - sets orb_status to VALID / RETRYING / FAILED / UNAVAILABLE so the
          dashboard and telemetry can observe ORB health
        - On final failure (exhausted retries) ORB remains empty and entry
          is safely blocked from ORB-style breakouts; ML-only entries still work.

        Safe to call at any time:
        - No-op if before market open (9:15).
        - No-op if ORB was already built from live ticks.
        - If startup is *during* the window (9:15–9:29), seeds whatever
          candles are already complete and lets the live feed add the rest.
        - If startup is *after* the window (≥9:30), fetches the full range
          and locks orb_done so no further accumulation occurs.
        """
        if ts is None:
            ts = datetime.now()
        now = ts.time()

        # Nothing to do before the market opens
        if now < _MARKET_OPEN:
            self.orb_status = "NONE"
            return

        # ORB already populated by live ticks — nothing to reconstruct
        if self.orb_high is not None and self.orb_low is not None:
            self.orb_status = "VALID"
            return

        today        = ts.date()
        orb_start_dt = datetime.combine(today, _MARKET_OPEN)
        # Request up to 9:30 so the Zerodha API includes the full window;
        # we filter strictly to <9:30 below.
        orb_end_dt   = datetime.combine(today, _ORB_END)

        # On a non-trading day (weekend/holiday) there is nothing to fetch;
        # skip straight to UNAVAILABLE instead of retrying pointlessly.
        if today.weekday() >= 5:
            self.orb_status = "UNAVAILABLE"
            self.orb_last_error = "non-trading day (weekend)"
            logger.info("[ORB RECONSTRUCT] Weekend — ORB unavailable, skipping retries")
            return

        max_attempts = self._orb_retries
        for attempt in range(1, max_attempts + 1):
            self.orb_reconstruct_attempts = attempt
            raw = None
            exc = None
            try:
                raw = broker.kite.historical_data(
                    _BANKNIFTY_INDEX_TOKEN, orb_start_dt, orb_end_dt,
                    "minute", oi=False,
                )
            except Exception as e:  # noqa: BLE001 — broker/network failures are logged & retried
                exc = e

            if exc is not None:
                self.orb_status = "RETRYING" if attempt < max_attempts else "FAILED"
                self.orb_last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"[ORB RECONSTRUCT] attempt {attempt}/{max_attempts} "
                    f"API call failed: {exc}"
                )
                if attempt < max_attempts:
                    import time as _time
                    _delay = self._orb_backoff_base * (2 ** (attempt - 1))
                    logger.info(f"[ORB RECONSTRUCT] retrying in {_delay:.0f}s")
                    _time.sleep(_delay)
                else:
                    logger.error(
                        f"[ORB RECONSTRUCT] FAILED after {max_attempts} attempts "
                        f"({self.orb_last_error}) — ORB unavailable; "
                        f"ORB breakouts remain blocked."
                    )
                continue

            # ── Validate the raw response ──────────────────────────────
            if not raw:
                self.orb_last_error = "empty response"
                logger.warning(
                    f"[ORB RECONSTRUCT] attempt {attempt}/{max_attempts} "
                    "API returned no candles — ORB unavailable."
                )
                self.orb_status = "RETRYING" if attempt < max_attempts else "FAILED"
                if attempt < max_attempts:
                    import time as _time
                    _delay = self._orb_backoff_base * (2 ** (attempt - 1))
                    _time.sleep(_delay)
                    continue
                if now >= _ORB_END:
                    self.orb_done = True
                return

            # Strip timezone, validate fields and filter strictly to
            # 9:15:00–9:29:59 of *today*.
            orb_candles = []
            for c in raw:
                try:
                    cdate    = c["date"]
                    c_high   = float(c["high"])
                    c_low    = float(c["low"])
                    c_open   = float(c.get("open", c_high))
                    c_close  = float(c.get("close", c_high))
                except Exception as _bad:  # malformed candle — reject, never fabricate
                    self.orb_last_error = f"malformed candle: {_bad}"
                    logger.warning(
                        f"[ORB RECONSTRUCT] Rejecting malformed candle: {_bad}"
                    )
                    continue

                if c_high < c_low or c_open <= 0 or c_close <= 0:
                    self.orb_last_error = "invalid OHLC (high<low or non-positive)"
                    logger.warning(
                        "[ORB RECONSTRUCT] Rejecting invalid OHLC candle "
                        f"(high={c_high} low={c_low})"
                    )
                    continue

                if hasattr(cdate, "tzinfo") and cdate.tzinfo is not None:
                    try:
                        import pytz as _tz
                        cdate = cdate.astimezone(_tz.timezone("Asia/Kolkata")).replace(tzinfo=None)
                    except Exception:
                        cdate = cdate.replace(tzinfo=None)
                d = cdate.date() if hasattr(cdate, "date") else today
                t = cdate.time() if hasattr(cdate, "time") else None
                # Strict trading-day boundary: only *today's* candles qualify.
                if d == today and t and _MARKET_OPEN <= t < _ORB_END:
                    orb_candles.append(c)

            expected_end = (datetime.combine(today, _ORB_END) - datetime.combine(today, _MARKET_OPEN)).total_seconds() / 60.0

            # A partial window (still accumulating) is acceptable only while
            # we are inside the window; once past 9:30 the fetched set must
            # cover the full 15-candle window (with tolerance for an
            # incomplete first candle at 9:15).
            too_little = len(orb_candles) < self._orb_min_candles
            if too_little and now >= _ORB_END:
                self.orb_last_error = (
                    f"incomplete ORB window: got {len(orb_candles)} "
                    f"of ~{int(expected_end):.0f} candles (min {self._orb_min_candles})"
                )
                logger.warning(
                    f"[ORB RECONSTRUCT] {self.orb_last_error} — ORB unavailable."
                )
                self.orb_status = "RETRYING" if attempt < max_attempts else "FAILED"
                if attempt < max_attempts:
                    import time as _time
                    _delay = self._orb_backoff_base * (2 ** (attempt - 1))
                    _time.sleep(_delay)
                    continue
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

            self.orb_status = "VALID"
            self.orb_last_error = ""
            logger.info(
                f"[ORB RECONSTRUCTED] High={self.orb_high:.2f}  Low={self.orb_low:.2f}"
                f"  (candles={len(orb_candles)}, window=9:15-9:29, "
                f"attempts={self.orb_reconstruct_attempts})"
            )
            return

        # Reached only if all attempts failed with exceptions.
        self.orb_status = "FAILED"
        if now >= _ORB_END:
            self.orb_done = True

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

        # EMA20 / EMA50 — Wilder-style seed from oldest available bar.
        # Use as many bars as possible (no arbitrary cap) so convergence
        # matches training as closely as the live window allows.
        def ema(series, span):
            alpha = 2 / (span + 1)
            val = series[0]
            for p in series[1:]:
                val = p * alpha + val * (1 - alpha)
            return val

        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50) if n >= 50 else ema(closes, 20)

        # RSI-14 — Wilder recursive smoothing (matches training _compute_rsi)
        c_arr_rsi = np.array(closes, dtype=float)
        rsi_arr   = _rsi_wilder(c_arr_rsi, period=14)
        rsi_1m    = float(rsi_arr[-1]) if n >= 14 else 50.0

        # ATR-14 — Wilder RMA over full window (matches training atr_wilder)
        h_full = np.array(highs, dtype=float)
        l_full = np.array(lows,  dtype=float)
        c_full = np.array(closes, dtype=float)
        atr_arr = _atr_wilder(h_full, l_full, c_full, period=14)
        atr_val = float(atr_arr[-1]) if n >= 14 else abs(closes[-1] - closes[-2]) * 14 ** 0.5
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

        # ── Higher-timeframe (5m) SuperTrend — anti-noise confirmation ──
        # The 1m SuperTrend flips on noise; entering on a fresh 1m flip is
        # what drives "trade goes negative immediately". Require the 5m trend
        # not to OPPOSE the trade. 0 = insufficient data (gate passes).
        try:
            self._htf5_dir = self._htf_supertrend_dir(df, tf=5)
        except Exception:
            self._htf5_dir = 0

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

    def _htf_supertrend_dir(self, df, tf: int = 5) -> int:
        """
        Build tf-minute candles from the 1-minute window and return the last
        SuperTrend(10,3) direction (+1 up / -1 down / 0 if insufficient data).
        Used as a slow-trend confirmation so entries don't fire on 1m noise.
        """
        if df is None or len(df) < tf * 12:
            return 0
        import numpy as np
        h = df["high"].values.astype(float)
        l = df["low"].values.astype(float)
        c = df["close"].values.astype(float)
        usable = (len(c) // tf) * tf
        if usable < tf * 12:
            return 0
        h = h[-usable:].reshape(-1, tf).max(axis=1)
        l = l[-usable:].reshape(-1, tf).min(axis=1)
        c = c[-usable:].reshape(-1, tf)[:, -1]
        if len(c) < 12:
            return 0
        st_dir, _ = _compute_supertrend(h, l, c, period=10, multiplier=3.0)
        return int(st_dir[-1])

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
            if _ce_p is None or _pe_p is None:
                self._last_ce_prob = 0.0
                self._last_pe_prob = 0.0
                self._last_ce_adj = 0.0
                self._last_pe_adj = 0.0
                self._last_ml_edge = 0.0
                self._last_predict_error = (
                    f"CE={'None' if _ce_p is None else _ce_p} "
                    f"PE={'None' if _pe_p is None else _pe_p}"
                )
                self._count_block("PREDICT_FAILED")
                self._last_block_reason = f"PREDICT_FAILED ({self._last_predict_error})"
                return None
            self._last_predict_error = ""
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
                # F4 — live ML edge
                self._last_ml_edge = round(abs(self._last_ce_adj - self._last_pe_adj), 3)

        # ══ STEP 2: Session gates ════════════════════════════════════════
        if now < _ORB_END:
            self._last_block_reason = "ORB_BUILD (9:15-9:30)"
            return None
        if now >= dtime(15, 15):
            self._last_block_reason = "MARKET_CLOSING (after 15:15)"
            return None
        # LUNCH_FILTER — OFF in dry-run testing. Set LUNCH_FILTER_ENABLED=1
        # (config) when switching to live capital.
        if self._lunch_enabled and _LUNCH_START <= now < _LUNCH_END:
            self._last_block_reason = "LUNCH_FILTER (11:00-12:30)"
            return None
        if not features:
            self._last_block_reason = "INSUFFICIENT_DATA (<26 candles)"
            return None

# ── Re-entry cooldown ────────────────────────────────────────────
        _secs_since_exit = time.time() - self._last_exit_ts
        if self._last_exit_ts > 0 and _secs_since_exit < self._reentry_cooldown:
            _wait = int(self._reentry_cooldown - _secs_since_exit)
            self._count_block("COOLDOWN")
            self._last_block_reason = f"COOLDOWN ({_wait}s remaining)"
            return None

        # ══ PREDICT-FIRST PATH (canonical) ════════════════════════════════
        # ML chooses direction (argmax of ce_adj/pe_adj); structure confirms.
        # This replaces the legacy "1m SuperTrend decides, ML rubber-stamps" path.
        # ── Decision-intelligence inputs (all fail-safe / optional) ──
        global_market_state = None
        conf_mult = 1.0
        try:
            gm = getattr(self.ctx, "global_market", None)
            if gm is not None:
                global_market_state = gm.get_state()
        except Exception as e:
            logger.debug(f"[DECISION] global_market fetch failed: {e}")
        try:
            st = getattr(self.ctx, "strategy_tracker", None)
            if st is not None:
                conf_mult = st.get_confidence_adjustment("ML")
        except Exception as e:
            logger.debug(f"[DECISION] strategy_tracker fetch failed: {e}")
        return self._check_entry_predict_first(
            df_window, features, ts,
            global_market_state=global_market_state,
            ml_confidence_adjustment=conf_mult,
        )

    def evaluate_phase55_candidate(
        self,
        *,
        side: str,
        confidence: float,
        symbol: str = "BANKNIFTY",
        features: dict | None = None,
        timestamp: datetime | None = None,
        source: str = "ML",
    ) -> dict:
        """Evaluate Phase55 for any final trade candidate.

        Used by the main ML path and by scalp before order placement so both
        entry systems respect the same deployment filter.
        """
        ts = timestamp or datetime.now()
        side = str(side or "").upper()
        features = features or self._last_features or {}
        phase55_regime = infer_regime_from_features(features)
        phase55_decision = evaluate_phase55_filter(
            market_features=features,
            ml_predictions={
                "CE": self._last_ce_adj,
                "PE": self._last_pe_adj,
                "ce_prob": self._last_ce_prob,
                "pe_prob": self._last_pe_prob,
            },
            current_regime=phase55_regime,
            confidence_scores={
                "side_confidence": confidence,
                "confidence": confidence,
                "ce_confidence": self._last_ce_adj,
                "pe_confidence": self._last_pe_adj,
                "ce_quality_confidence": self._last_ce_adj if side == "CE" else confidence,
                "pe_directional_confidence": self._last_pe_adj if side == "PE" else confidence,
                "directional_confidence": confidence,
            },
            direction=side,
            config=self._phase55_config,
            symbol=symbol,
            timestamp=ts,
        )
        applied_filters = phase55_decision.get("applied_filters", [])
        phase55_allow = bool(phase55_decision.get("allow_trade", True))
        phase55_confidence = float(confidence or 0.0)
        phase55_ml_probability = (
            float(self._last_ce_prob or 0.0) if side == "CE"
            else float(self._last_pe_prob or 0.0)
        )
        phase55_telemetry_id = ""
        if self._phase55_telemetry is not None:
            phase55_telemetry_id = self._phase55_telemetry.record_decision(
                timestamp=ts,
                symbol=symbol or "BANKNIFTY",
                direction=side,
                regime=phase55_regime,
                confidence=phase55_confidence,
                ml_probability=phase55_ml_probability,
                recommendation=phase55_decision.get("recommendation", ""),
                allow_trade=phase55_allow,
                blocking_reason=phase55_decision.get("blocking_reason", ""),
                applied_filters=applied_filters,
            )
        self._last_phase55_decision = {
            "enabled": self._phase55_config.enabled,
            "filter_used": ", ".join(applied_filters) if applied_filters else "none",
            "trade_blocked": not phase55_allow,
            "direction": side,
            "telemetry_id": phase55_telemetry_id,
            "shadow_attached": False,
            "reason": phase55_decision.get("blocking_reason", ""),
            "recommendation": phase55_decision.get("recommendation", ""),
            "source": source,
        }
        if not phase55_allow:
            reason = str(phase55_decision.get("blocking_reason", "PHASE55_BLOCK"))
            logger.info(
                "[PHASE55 BLOCK] "
                f"timestamp={ts.isoformat()} "
                f"symbol={symbol or 'UNKNOWN'} "
                f"direction={side} "
                f"confidence={phase55_confidence:.4f} "
                f"regime={phase55_regime} "
                f"blocking_rule={reason} "
                f"source={source} "
                "original_decision=ALLOW "
                "final_decision=BLOCK"
            )
            self._count_block("PHASE55")
            self._last_block_reason = f"PHASE55_BLOCK ({reason})"

        out = dict(self._last_phase55_decision)
        out.update(
            {
                "allow_trade": phase55_allow,
                "applied_filters": list(applied_filters),
                "blocking_reason": phase55_decision.get("blocking_reason", ""),
                "regime": phase55_regime,
            }
        )
        return out

    def _finalize_signal(self, signal: dict, features: dict, price: float):
        """
        STEP 6 — attach risk stops + expected-PnL guard, then return the
        signal (or None if it fails the guard). Shared by both the legacy
        direction-gate path and the predict-first path.
        """
        side = str(signal.get("side", "")).upper()
        phase55_state = self.evaluate_phase55_candidate(
            side=side,
            confidence=float(signal.get("ml_prob", 0.0) or 0.0),
            symbol=signal.get("symbol", "BANKNIFTY"),
            features=features,
            timestamp=datetime.now(),
            source="ML",
        )
        phase55_telemetry_id = str(phase55_state.get("telemetry_id", "") or "")
        if not bool(phase55_state.get("allow_trade", True)):
            return None

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

        lot_size     = getattr(getattr(self.ctx, "config", None), "LOT_SIZE", 30)
        exp_win      = (target - price) * lot_size
        exp_loss     = (price - stop_loss) * lot_size
        expected_pnl = signal["ml_prob"] * exp_win - (1 - signal["ml_prob"]) * exp_loss
        if expected_pnl < _MIN_EXPECTED_PNL:
            logger.info(
                f"[PNL GUARD] Expected Rs{expected_pnl:.0f} < Rs{_MIN_EXPECTED_PNL} — skipping"
            )
            self._count_block("PNL_GUARD")
            self._last_block_reason = f"PNL_GUARD (exp=Rs{expected_pnl:.0f})"
            return None

        if phase55_telemetry_id:
            signal["_phase55_telemetry_id"] = phase55_telemetry_id
            signal["_phase55_decision"] = dict(self._last_phase55_decision)

        logger.info(
            f"[ENTRY SIGNAL] {signal['side']} | reason={signal['reason']} | "
            f"prob={signal['ml_prob']:.3f} | SL={stop_loss:.2f} | TP={target:.2f} | "
            f"ExpPnL=Rs{expected_pnl:.0f}"
        )
        return signal

    def _check_entry_predict_first(self, df_window, features, ts,
                                   global_market_state=None,
                                   ml_confidence_adjustment: float = 1.0):
        """
        PREDICT-FIRST decision: the ML models choose the direction, structure
        confirms it. This is the fix for "trade goes negative on entry" —
        we no longer let the whippy 1m SuperTrend pick the side.

        Order of operations:
          1. PREDICT — direction = argmax(ce_adj, pe_adj).
          2. EDGE    — require |ce_adj - pe_adj| >= margin (clear conviction).
          3. THRESHOLD — chosen side must clear its calibrated threshold.
          4. CONFIRM — 5m SuperTrend must AGREE (not just 'not oppose'), and
                       VWAP side must agree. This is the AI+ML+structure
                       confirmation the trade direction is real.
          5. DECISION-INTELLIGENCE — weighted FINAL_SCORE gate (ML/ORB/Global/
                       Volatility). Fail-open: skipped when layer absent.
          6. learner side-block + finalize (risk/PnL guard).

        All new inputs (global_market_state, ml_confidence_adjustment) are
        optional and fail-safe — None/1.0 keep prior behavior untouched.
        """
        price  = df_window["close"].iloc[-1]
        ce_adj = self._last_ce_adj
        pe_adj = self._last_pe_adj
        htf5   = self._htf5_dir
        pvwap  = features.get("price_vs_vwap", 0.0)

        # Per-side thresholds: max of the model's calibrated threshold and the
        # learner's adaptive threshold (rises after losses).
        learn_thr = self.learner.get_ml_threshold()
        ce_thr = max(getattr(self.predictor, "ce_threshold", 0.5), learn_thr, _CE_ML_FLOOR)
        pe_thr = max(getattr(self.predictor, "pe_threshold", 0.5), learn_thr, _MIN_ML_FLOOR)

        # 1. PREDICT direction
        side   = "CE" if ce_adj >= pe_adj else "PE"
        prob   = ce_adj if side == "CE" else pe_adj
        thr    = ce_thr if side == "CE" else pe_thr
        other  = pe_adj if side == "CE" else ce_adj

        logger.info(
            f"[PREDICT-FIRST] CE={ce_adj:.3f}(raw={self._last_ce_prob:.3f}) "
            f"PE={pe_adj:.3f}(raw={self._last_pe_prob:.3f}) -> {side} "
            f"thr={thr:.2f} 5m={htf5} pvwap={pvwap:.4f}"
        )

        # 2. EDGE — need clear directional conviction
        if abs(ce_adj - pe_adj) < self._ml_edge_margin:
            self._count_block("NO_EDGE")
            self._last_block_reason = (
                f"NO_EDGE (|CE-PE|={abs(ce_adj-pe_adj):.2f} < {self._ml_edge_margin})"
            )
            return None

        # 3. THRESHOLD
        if prob < thr:
            self._count_block("ML_BELOW_THR")
            self._last_block_reason = f"ML_BELOW_THR ({side} {prob:.2f} < {thr:.2f})"
            return None

        # 3b. CE RANGE-DAY HARD FLOOR — pure-ML CE on a RANGE day needs higher conviction
        # (shadow data: ml_95 block would have saved -288 and -396 today)
        if side == "CE":
            _regime_now = str(self.learner.get_day_type()).upper()
            _is_range_now = "RANGE" in _regime_now or _regime_now in ("UNKNOWN", "")
            _ce_hard_floor = float(os.getenv("CE_ML_HARD_FLOOR", "0.78"))
            if _is_range_now and prob < _ce_hard_floor:
                self._count_block("CE_RANGE_HARD_FLOOR")
                self._last_block_reason = (
                    f"CE_RANGE_HARD_FLOOR (prob={prob:.3f} < {_ce_hard_floor:.2f}, RANGE day)"
                )
                logger.info(
                    f"[BLOCK] CE_RANGE_HARD_FLOOR — prob={prob:.3f} < {_ce_hard_floor:.2f} on RANGE day, skipped"
                )
                return None

        # 4. CONFIRM — 5m trend must AGREE (htf5==0 = insufficient data → allow)
        if side == "CE" and htf5 == -1:
            self._count_block("HTF5_OPPOSES")
            self._last_block_reason = f"CE_HTF5_OPPOSES (5m=DOWN, prob={prob:.2f})"
            return None
        if side == "PE" and htf5 == 1:
            self._count_block("HTF5_OPPOSES")
            self._last_block_reason = f"PE_HTF5_OPPOSES (5m=UP, prob={prob:.2f})"
            return None

        # 4b. CONFIRM — VWAP side must agree (within tolerance). CE above VWAP,
        # PE below. Tolerance ≈ 0.15% so a marginal cross isn't over-blocked.
        _VWAP_TOL = 0.0015
        if side == "CE" and pvwap < -_VWAP_TOL:
            self._count_block("VWAP_FAIL")
            self._last_block_reason = f"CE_VWAP_FAIL (pvwap={pvwap:.4f} below VWAP)"
            return None
        if side == "PE" and pvwap > _VWAP_TOL:
            self._count_block("VWAP_FAIL")
            self._last_block_reason = f"PE_VWAP_FAIL (pvwap={pvwap:.4f} above VWAP)"
            return None

        # 5. learner side-block (consecutive-loss / losing-side lock)
        blocked, reason_block = self.learner.is_side_blocked(side)
        if blocked:
            self._count_block("ML_BLOCKED")
            self._last_block_reason = f"{side}_BLOCKED ({reason_block})"
            return None

        # 5b. DECISION-INTELLIGENCE — weighted FINAL_SCORE gate.
        #     FINAL_SCORE = ML*0.5 + ORB*0.2 + GLOBAL*0.2 + VOL*0.1.
        #     Optional & fail-open: layer absent or throws → trade proceeds.
        decision_score = None
        di = getattr(self.ctx, "decision_intelligence", None)
        if di is not None:
            try:
                # Real ORB breakout state (same test as legacy path)
                _ce_brk = (self.orb_done and self.orb_high is not None and
                           price > self.orb_high and not self.orb_ce_fired)
                _pe_brk = (self.orb_done and self.orb_low is not None and
                           price < self.orb_low and not self.orb_pe_fired)
                _vol = "NORMAL"
                if global_market_state is not None:
                    _vol = getattr(global_market_state, "volatility", "NORMAL")
                # Strategy confidence adjustment caps ML contribution (streak guard)
                _ml_adj = max(0.0, min(1.0, prob * ml_confidence_adjustment))
                decision_score = di.evaluate(
                    ml_probability=_ml_adj,
                    side=side,
                    global_market_state=global_market_state,
                    volatility_state=_vol,
                    orb_breakout=(_ce_brk or _pe_brk),
                    orb_direction=1 if side == "CE" else -1,
                )
                if decision_score.decision == "SKIP":
                    self._count_block("DECISION_SKIP")
                    self._last_block_reason = (
                        f"DECISION_SKIP (score={decision_score.final_score:.2f} "
                        f"< thr={decision_score.threshold:.2f} | "
                        f"{decision_score.skip_reason})"
                    )
                    logger.info(
                        f"[DECISION] {side} SKIP — FINAL_SCORE={decision_score.final_score:.3f} "
                        f"thr={decision_score.threshold:.3f} "
                        f"reason={decision_score.skip_reason}"
                    )
                    return None
                logger.info(
                    f"[DECISION] {side} ALLOW — FINAL_SCORE={decision_score.final_score:.3f} "
                    f"thr={decision_score.threshold:.3f}"
                )
            except Exception as e:
                logger.debug(f"[DECISION] scoring failed (fail-open): {e}")
                decision_score = None
        self._last_decision_score = decision_score

        self._last_block_reason = f"SIGNAL_FIRE (ML_{side})"
        signal = {"side": side, "ml_prob": prob,
                  "features": features, "reason": f"ML_{side}"}
        if decision_score is not None:
            signal["decision_score"] = decision_score
        return self._finalize_signal(signal, features, price)

    def _ml_percentile(self, prob: float) -> int:
        """Percentile rank of prob within today's ML history."""
        if not self._ml_history:
            return 0
        import numpy as np
        return int(np.sum(np.array(self._ml_history) <= prob) / len(self._ml_history) * 100)

    # F3 ─────────────────────────────────────────────────────────────
    def _count_block(self, key: str) -> None:
        """
        Increment block counter for `key`.
        Deduplicates transitions: only counts when the key changes from the
        previous call, so a sustained 3-minute COOLDOWN counts as 1, not 180.
        Resets all counts at midnight.
        """
        from datetime import date as _date
        today = _date.today()
        if today != self._block_date:
            self._block_counts    = {}
            self._block_date      = today
            self._last_counted_key = ""
        if key != self._last_counted_key:
            self._block_counts[key]  = self._block_counts.get(key, 0) + 1
            self._last_counted_key   = key

    def record_block(self, key: str) -> None:
        """Public — called from master_runner for OI_WALL, PAUSED, etc."""
        self._count_block(key)

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

        learn_thr = self.learner.get_ml_threshold()
        ce_thr = max(getattr(self.predictor, "ce_threshold", 0.5), learn_thr, _CE_ML_FLOOR)
        pe_thr = max(getattr(self.predictor, "pe_threshold", 0.5), learn_thr, _MIN_ML_FLOOR)
        phase55_state = dict(self._last_phase55_decision)
        phase55_state.update(self.get_phase55_telemetry_snapshot())

        return {
            "ts":              ts,
            "session":         session,
            "ema20":           ema20,
            "ema50":           ema50,
            "ema_direction":   "UP" if ema20 > ema50 else "DOWN",
            "rsi_1m":          f.get("rsi_1m", f.get("rsi", 50.0)),
            "rsi":             f.get("rsi", f.get("rsi_1m", 50.0)),
            "atr":             f.get("atr", 0.0),
            "supertrend_dir":  int(f.get("supertrend_dir", 0)),
            "htf5_dir":         int(getattr(self, "_htf5_dir", 0)),
            "supertrend_dist": f.get("supertrend_dist", 0.0),
            "vwap":            self._vwap.value,
            "price_vs_vwap":   f.get("price_vs_vwap", 0.0),
            "adx":             f.get("adx", 0.0),
            "di_spread":       f.get("di_spread", 0.0),
            "direction_bias":  self._direction_bias,
            "orb_high":        self.orb_high,
            "orb_low":         self.orb_low,
            "orb_done":        self.orb_done,
            "orb_status":      getattr(self, "orb_status", "NONE"),
            "orb_reconstruct_attempts": getattr(self, "orb_reconstruct_attempts", 0),
            "orb_last_error":  getattr(self, "orb_last_error", ""),
            "ce_prob":         self._last_ce_prob,
            "pe_prob":         self._last_pe_prob,
            "htf_bullish":     getattr(self, "_last_htf_bullish", True),
            "ce_adj":          self._last_ce_adj,
            "pe_adj":          self._last_pe_adj,
            "ml_threshold":    max(learn_thr, _MIN_ML_FLOOR),
            "ce_threshold":    ce_thr,
            "pe_threshold":    pe_thr,
            "block_reason":    self._last_block_reason,
            # Scoring
            "ml_percentile":   self._ml_percentile(max(self._last_ce_adj, self._last_pe_adj)),
            "ml_score":        round(
                max(self._last_ce_adj, self._last_pe_adj) * 50
                + self._ml_percentile(max(self._last_ce_adj, self._last_pe_adj)) / 2,
                1
            ),
            "score_required":  40.0,   # ≈ 0.62*50 + median_percentile/2
            # F3 — block analytics
            "block_counts":    dict(self._block_counts),
            # F4 — ML edge
            "ml_edge":         self._last_ml_edge,
            "phase55":         phase55_state,
        }

    # ══════════════════════════════════════════════════════════════════
    # EXIT LOGIC  (delegates to profit_manager)
    # ══════════════════════════════════════════════════════════════════

    def phase55_telemetry_enabled(self) -> bool:
        return self._phase55_telemetry is not None

    def get_phase55_telemetry_snapshot(self) -> dict:
        if self._phase55_telemetry is None:
            return empty_phase55_telemetry_snapshot(enabled=False)
        return self._phase55_telemetry.snapshot()

    def get_last_phase55_decision(self) -> dict:
        return dict(self._last_phase55_decision)

    def attach_phase55_block_shadow(
        self,
        *,
        symbol: str,
        entry_price: float,
        quantity: int,
        timestamp: datetime | None = None,
    ) -> bool:
        if self._phase55_telemetry is None:
            return False
        decision = self._last_phase55_decision
        decision_id = decision.get("telemetry_id")
        if not decision.get("trade_blocked") or not decision_id or decision.get("shadow_attached"):
            return False

        atr_val = (self._last_features or {}).get("atr", float(entry_price or 0.0) * 0.01)
        day_type = self.learner.get_day_type()
        regime = (
            "EXPANSION" if "VOLATILE" in day_type else
            "TREND" if "TREND" in day_type else
            "RANGE"
        )
        stop_loss, target, _stop_pct = compute_entry_stops(float(entry_price), atr_val, regime)
        max_hold = int(getattr(getattr(self.ctx, "config", None), "MAX_HOLD_SECONDS", 300))
        attached = self._phase55_telemetry.attach_shadow_entry(
            decision_id,
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            target=target,
            timestamp=timestamp,
            max_hold_seconds=max_hold,
        )
        if attached:
            decision["shadow_attached"] = True
            decision["symbol"] = symbol
        return attached

    def update_phase55_actual_entry(self, position: dict, timestamp: datetime | None = None) -> None:
        if self._phase55_telemetry is None or not position:
            return
        self._phase55_telemetry.update_actual_entry(
            position.get("_phase55_telemetry_id"),
            symbol=position.get("symbol", ""),
            entry_price=position.get("entry", 0.0),
            quantity=position.get("qty", 0),
            timestamp=timestamp,
        )

    def open_phase55_shadow_symbols(self) -> list[str]:
        if self._phase55_telemetry is None:
            return []
        return self._phase55_telemetry.open_shadow_symbols()

    def observe_phase55_shadow_price(
        self,
        *,
        symbol: str,
        price: float,
        timestamp: datetime | None = None,
    ) -> None:
        if self._phase55_telemetry is None:
            return
        self._phase55_telemetry.observe_shadow_price(
            symbol=symbol,
            price=price,
            timestamp=timestamp,
        )

    def record_phase55_actual_outcome(
        self,
        position: dict,
        *,
        pnl: float,
        exit_reason: str,
        timestamp: datetime | None = None,
    ) -> None:
        if self._phase55_telemetry is None or not position:
            return
        self._phase55_telemetry.record_actual_outcome(
            position.get("_phase55_telemetry_id"),
            symbol=position.get("symbol", ""),
            direction=position.get("side", ""),
            pnl=pnl,
            exit_reason=exit_reason,
            timestamp=timestamp,
        )

    def generate_phase55_eod_report(self, timestamp: datetime | None = None) -> dict | None:
        if self._phase55_telemetry is None:
            return None
        return self._phase55_telemetry.generate_eod_report(timestamp=timestamp)

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
        # SINGLE position-size source of truth: use the ACTUAL filled qty so
        # max_pnl/MFE is consistent with realized PnL and MAE (which use qty).
        # Falls back to lot_size, then 1, for legacy/partial dicts.
        size      = position.get("qty", position.get("lot_size", 1))
        stop_loss = position.get("stop_loss", entry * 0.90)
        max_pnl   = position.get("max_pnl", 0.0)
        ml_prob   = position.get("ml_prob", 0.5)
        # Live market regime drives the dynamic tight/loose trail.
        _regime   = str(self.learner.get_day_type())

        # ── profit_manager handles trailing + lock system ─────────────
        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=entry,
            ltp=ltp,
            lot_size=size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
            target=position.get("target"),
            regime=_regime,
        )

        # Update position dict in-place so caller persists new SL
        position["stop_loss"] = new_sl
        position["max_pnl"]   = new_max_pnl

        # Track highest ladder rung for diagnostics journal
        from engine.execution.profit_manager import ladder_locked_rs
        _lrs, _lstage = ladder_locked_rs(new_max_pnl, size, ml_prob, _regime)
        if _lrs > 0:
            position["_ladder_stage"] = _lstage

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
