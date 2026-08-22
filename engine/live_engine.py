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
from engine.execution.filters import compute_entry_quality
from engine.execution.cost_model import round_trip_cost
from engine.risk.risk_manager import compute_entry_stops

logger = logging.getLogger("live_engine")

# ── Market session constants ──────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)   # ORB window: 9:15 – 9:29 (15 candles)

# Zerodha instrument token for BANK NIFTY index (used in ORB reconstruction)
_NIFTY_INDEX_TOKEN = 260105
_DAY_CLASS_AT = dtime(9, 45)   # Day classifier locks after 9:44
_MARKET_CLOSE = dtime(15, 30)

# ── Session filter — no new entries during lunch chop ─────────────────
_LUNCH_START    = dtime(11,  0)
_LUNCH_END      = dtime(12, 30)   # was 14:00 — 12:30-14:00 window recovered

# ── TIER 1: Warmup block — no entries until WARMUP_MINUTES after open ──
# Default 90 min = 11:00 (allows ML learner to warm up, avoids early noise)
# Controlled by Config.WARMUP_MINUTES (env: WARMUP_MINUTES)
def _warmup_end_time(warmup_minutes: int) -> dtime:
    """Compute warmup end time from market open + minutes."""
    total_min = 9 * 60 + 15 + warmup_minutes
    return dtime(total_min // 60, total_min % 60)

# ── Minimum expected PnL to accept a signal (capital safeguard) ───────
_MIN_EXPECTED_PNL = 150.0

# ── ML floors — per-side thresholds ──────────────────────────────────
# PE: 0.65 (backtest WR=58%, avg+174 at this level)
# CE: 0.78 — raised from 0.70. Model is CE-biased (outputs 0.80-0.96 all day
#     even on bear days). Higher floor forces only truly exceptional signals.
#     On RANGE regime days, CE_ML_FLOOR rises further to 0.85 (see STEP 5).
_MIN_ML_FLOOR    = 0.65   # PE floor
_CE_ML_FLOOR     = 0.78   # CE needs high confidence — model is CE-biased
# Re-entry cooldown is config-driven (Config.REENTRY_COOLDOWN, default 300s);
# see LiveEngine.__init__ self._reentry_cooldown.
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
        _cfg = getattr(ctx, "config", None)
        # Config-driven (defaults match config.py). Raised 180->300 so a
        # stopped option is not re-entered while still reversing.
        self._reentry_cooldown: float = float(getattr(_cfg, "REENTRY_COOLDOWN", 300))
        # Lunch filter OFF for dry-run; flip LUNCH_FILTER_ENABLED=1 for live.
        self._lunch_enabled: bool = bool(getattr(_cfg, "LUNCH_FILTER_ENABLED", False))
        # Higher-timeframe (5m) SuperTrend direction — anti-noise entry gate.
        self._htf5_dir: int = 0
        # ── PREDICT-FIRST direction selection ─────────────────────────
        # When True, the ML models CHOOSE the direction (argmax of ce/pe
        # probability) and structure (5m trend + VWAP) only CONFIRMS it —
        # instead of 1m SuperTrend choosing and ML rubber-stamping. Requires
        # the v3 directional models (both sides trained on all bars).
        self._predict_first: bool = (os.getenv("PREDICT_FIRST", "1") == "1")
        # Minimum gap between the two sides' probs to claim a directional
        # edge. If |ce - pe| < margin, conviction is too low → no trade.
        self._ml_edge_margin: float = float(os.getenv("ML_EDGE_MARGIN", "0.15"))

        # ══════════════════════════════════════════════════════════════════
        # ENTRY FIXES — Structure, Pullback, HTF, Trap
        # ══════════════════════════════════════════════════════════════════

        # A. Structure confirmation — track HH/LL for continuation
        self._last_swing_high: float | None = None
        self._last_swing_low: float | None = None
        self._swing_lookback: int = 5     # bars to confirm swing
        self._structure_confirmed: bool = False
        self._structure_wait_bars: int = 0

        # B. Pullback entry — wait for retrace after breakout
        self._breakout_detected: bool = False
        self._breakout_price: float | None = None
        self._breakout_side: str | None = None   # "CE" or "PE"
        self._breakout_ts: datetime | None = None
        self._pullback_target: float | None = None
        self._pullback_tolerance_pct: float = 0.35   # 35% retrace of breakout move
        self._max_pullback_wait_bars: int = 8         # max bars to wait for pullback
        self._pullback_wait_count: int = 0

        # C. HTF trend alignment — 15m/30m SuperTrend + EMA
        self._htf15_dir: int = 0
        self._htf30_dir: int = 0
        self._htf15_ema20: float | None = None
        self._htf15_ema50: float | None = None
        self._htf30_ema20: float | None = None
        self._htf30_ema50: float | None = None
        self._require_htf_align: bool = bool(getattr(_cfg, "REQUIRE_HTF_ALIGN", True)) if _cfg else True

        # D. Trap filter — detect failed breakouts (immediate reversal)
        self._trap_lookback: int = 3       # bars to confirm breakout failure
        self._trap_threshold_pct: float = 0.5   # reversal >50% of breakout move = trap
        self._recent_breakouts: list = []   # track recent breakout attempts

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

    def _backfill_day_classification(self, csv_path: str):
        """
        Late-start recovery: engine started after 09:45, so the day
        classifier never collected its first-30-min candles (the code only
        collects while now < 09:45). Read today's 9:15-9:44 candles from
        the historical CSV, feed the learner + classifier, and classify now.
        """
        import pandas as pd
        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            logger.warning(f"[DAY CLASSIFIER] Backfill: CSV read failed: {e}")
            return
        date_col = next(
            (c for c in ["date", "datetime", "timestamp", "time"]
             if c in df.columns), None
        )
        if date_col is None:
            logger.warning("[DAY CLASSIFIER] Backfill: no date column")
            return
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col])
        df = df.sort_values(date_col)

        today = datetime.now().date()
        start = datetime.combine(today, dtime(9, 15))
        end   = datetime.combine(today, dtime(9, 44))
        win   = df[(df[date_col] >= start) & (df[date_col] <= end)]

        if len(win) < 10:
            logger.warning(
                f"[DAY CLASSIFIER] Backfill: only {len(win)} candles in 9:15-9:44 window"
            )
            return

        candles = []
        for _, row in win.iterrows():
            candles.append({
                "open":   float(row.get("open",  row.get("close", 0))),
                "high":   float(row.get("high",  row.get("close", 0))),
                "low":    float(row.get("low",   row.get("close", 0))),
                "close":  float(row.get("close", 0)),
                "volume": int(row.get("volume", 0)),
            })

        # Feed learner so get_day_type() resolves to a real day type
        self.learner.backfill_first_30m(candles)
        # Collect for the DayClassifier model
        self._day_candles_30m = candles
        logger.info(
            f"[DAY CLASSIFIER] Backfilled {len(candles)} candles from history | "
            f"learner_day_type={self.learner.get_day_type()}"
        )

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
            # Late-start recovery: if the engine restarted after 9:45 no
            # candles were collected live — backfill from today's CSV so the
            # day type is not stuck UNKNOWN (which blocks every ML entry via
            # RANGE_REGIME_SKIP). Only scalps would trade otherwise.
            if len(self._day_candles_30m) < 10:
                self._backfill_day_classification(
                    os.getenv("HIST_CSV", "data/historical/nifty_1m_full.csv")
                )
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

        # ── HTF 5m SuperTrend (existing) ──
        try:
            self._htf5_dir = self._htf_supertrend_dir(df, tf=5)
        except Exception:
            self._htf5_dir = 0

        # ── FIX C: HTF 15m / 30m trend alignment ──────────────────────
        # Compute SuperTrend direction on resampled 15m and 30m candles.
        # Also compute EMA20 vs EMA50 crossover on those timeframes.
        # These give true "higher-timeframe" context: trade only in the
        # direction of the dominant trend.
        try:
            self._htf15_dir = self._htf_supertrend_dir(df, tf=15)
        except Exception:
            self._htf15_dir = 0
        try:
            self._htf30_dir = self._htf_supertrend_dir(df, tf=30)
        except Exception:
            self._htf30_dir = 0

        # HTF EMA alignment (proxy via mean of resampled closes)
        self._htf15_ema20, self._htf15_ema50 = self._htf_ema_pair(df, tf=15)
        self._htf30_ema20, self._htf30_ema50 = self._htf_ema_pair(df, tf=30)

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

    def _htf_ema_pair(self, df, tf: int):
        """
        Return (ema20, ema50) on tf-minute resampled closes.
        Uses simple EMA calculation on resampled data.
        """
        if df is None or len(df) < tf * 12:
            return None, None
        import numpy as np
        c = df["close"].values.astype(float)
        usable = (len(c) // tf) * tf
        if usable < tf * 12:
            return None, None
        c = c[-usable:].reshape(-1, tf)[:, -1]
        if len(c) < 50:
            return None, None

        def ema(series, span):
            alpha = 2 / (span + 1)
            val = series[0]
            for p in series[1:]:
                val = p * alpha + val * (1 - alpha)
            return val

        ema20 = ema(c[-min(len(c), 60):], 20)
        ema50 = ema(c[-min(len(c), 100):], 50) if len(c) >= 50 else ema20
        return ema20, ema50

    def _htf_trend_aligned(self, direction: int) -> bool:
        """
        FIX C -- Check if HTF (15m & 30m) trend aligns with trade direction.
        direction: +1 for CE (bullish), -1 for PE (bearish).
        Returns True if both 15m AND 30m SuperTrend + EMA agree.
        """
        if not self._require_htf_align:
            return True

        st_ok_15 = (self._htf15_dir == 0) or (self._htf15_dir == direction)
        st_ok_30 = (self._htf30_dir == 0) or (self._htf30_dir == direction)

        ema_ok_15 = True
        if self._htf15_ema20 is not None and self._htf15_ema50 is not None:
            ema_ok_15 = (self._htf15_ema20 > self._htf15_ema50) if direction == 1 else \
                        (self._htf15_ema20 < self._htf15_ema50)

        ema_ok_30 = True
        if self._htf30_ema20 is not None and self._htf30_ema50 is not None:
            ema_ok_30 = (self._htf30_ema20 > self._htf30_ema50) if direction == 1 else \
                        (self._htf30_ema20 < self._htf30_ema50)

        return st_ok_15 and st_ok_30 and ema_ok_15 and ema_ok_30

    def _update_swings_and_structure(self, high: float, low: float, close: float):
        """
        FIX A -- Track swing highs/lows and confirm structure (HH/HL for bullish,
        LH/LL for bearish). Sets self._structure_confirmed when price makes
        a clear continuation pattern.
        """
        if self._last_swing_high is None or high > self._last_swing_high:
            self._last_swing_high = high
        if self._last_swing_low is None or low < self._last_swing_low:
            self._last_swing_low = low

        if self._last_swing_high and self._last_swing_low:
            if close > self._last_swing_high:
                self._structure_confirmed = True
                self._structure_wait_bars = 0
            elif close < self._last_swing_low:
                self._structure_confirmed = True
                self._structure_wait_bars = 0
            else:
                # Price inside range — auto-confirm after 5 bars (range IS structure)
                self._structure_wait_bars = getattr(self, '_structure_wait_bars', 0) + 1
                if self._structure_wait_bars >= 5:
                    self._structure_confirmed = True
                else:
                    self._structure_confirmed = False
        else:
            self._structure_confirmed = False

    def _check_pullback_entry(self, df_window, side: str, price: float) -> bool:
        """
        FIX B -- Pullback entry logic.
        - When breakout detected (price breaks ORB or key level), don't enter immediately.
        - Wait for retrace of 30-50% of breakout move, then enter on resumption.
        Returns True if pullback entry condition met (ready to enter now).
        """
        if not self._breakout_detected:
            if side == "CE" and self.orb_done and self.orb_high and price > self.orb_high:
                self._breakout_detected = True
                self._breakout_price = price
                self._breakout_side = "CE"
                self._breakout_ts = datetime.now()
                self._pullback_wait_count = 0
                return False
            elif side == "PE" and self.orb_done and self.orb_low and price < self.orb_low:
                self._breakout_detected = True
                self._breakout_price = price
                self._breakout_side = "PE"
                self._breakout_ts = datetime.now()
                self._pullback_wait_count = 0
                return False
            return True

        if self._breakout_side != side:
            self._breakout_detected = False
            self._breakout_price = None
            self._breakout_side = None
            return True

        self._pullback_wait_count += 1

        if side == "CE" and self._breakout_price:
            breakout_move = price - self.orb_high if self.orb_high else 0
            if breakout_move > 0:
                self._pullback_target = self._breakout_price - (breakout_move * self._pullback_tolerance_pct)
                if price <= self._pullback_target:
                    self._pullback_target = None
                    return True
        elif side == "PE" and self._breakout_price:
            breakout_move = self.orb_low - price if self.orb_low else 0
            if breakout_move > 0:
                self._pullback_target = self._breakout_price + (breakout_move * self._pullback_tolerance_pct)
                if price >= self._pullback_target:
                    self._pullback_target = None
                    return True

        if self._pullback_wait_count >= self._max_pullback_wait_bars:
            self._breakout_detected = False
            self._breakout_price = None
            self._breakout_side = None
            self._pullback_target = None
            return True

        return False

    def _check_trap_filter(self, df_window, side: str, price: float) -> bool:
        """
        FIX D -- Trap filter: detect if recent breakout failed immediately.
        If price broke ORB/level but reversed >50% of move within 3 bars,
        mark as trap and block new entries in same direction for a few bars.
        Returns True if OK to enter (no trap detected).
        """
        if not self.orb_done:
            return True

        current_bar = {
            "side": side,
            "price": price,
            "orb_high": self.orb_high,
            "orb_low": self.orb_low,
        }
        self._recent_breakouts.append(current_bar)
        if len(self._recent_breakouts) > 10:
            self._recent_breakouts.pop(0)

        if len(self._recent_breakouts) < 3:
            return True

        recent = self._recent_breakouts[-3:]
        for i, bar in enumerate(recent[:-1]):
            next_bar = recent[i + 1]
            if bar["side"] == "CE" and bar["orb_high"] and bar["price"] > bar["orb_high"]:
                breakout_move = bar["price"] - bar["orb_high"]
                if breakout_move > 0:
                    reversal = bar["price"] - next_bar["price"]
                    if reversal > breakout_move * self._trap_threshold_pct:
                        logger.warning(f"[TRAP FILTER] CE breakout failed: move={breakout_move:.1f} reversal={reversal:.1f}")
                        return False
            elif bar["side"] == "PE" and bar["orb_low"] and bar["price"] < bar["orb_low"]:
                breakout_move = bar["orb_low"] - bar["price"]
                if breakout_move > 0:
                    reversal = next_bar["price"] - bar["price"]
                    if reversal > breakout_move * self._trap_threshold_pct:
                        logger.warning(f"[TRAP FILTER] PE breakout failed: move={breakout_move:.1f} reversal={reversal:.1f}")
                        return False

        return True


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

        # ── TIER 1: Warmup block — no entries until WARMUP_MINUTES after open ──
        # Allows ML learner to warm up; avoids early-session noise trades.
        _cfg = getattr(self.ctx, "config", None)
        _warmup_min = int(getattr(_cfg, "WARMUP_MINUTES", 90)) if _cfg else 90
        _warmup_end = _warmup_end_time(_warmup_min)
        if now < _warmup_end:
            self._last_block_reason = f"WARMUP_BLOCK (until {_warmup_end.strftime('%H:%M')})"
            return None

        if not features:
            self._last_block_reason = "INSUFFICIENT_DATA (<26 candles)"
            return None

        # ── TIER 1: Regime filter — skip RANGE days (31% WR, negative expectancy) ──
        _regime_str = str(self.learner.get_day_type()).upper()
        _is_range = "RANGE" in _regime_str or _regime_str in ("UNKNOWN", "")
        _skip_range = bool(getattr(_cfg, "SKIP_RANGE_REGIME", True)) if _cfg else True
        if _skip_range and _is_range:
            self._count_block("RANGE_REGIME_SKIP")
            self._last_block_reason = f"RANGE_REGIME_SKIP (day_type={_regime_str})"
            return None

        # ── Re-entry cooldown ────────────────────────────────────────────
        _secs_since_exit = time.time() - self._last_exit_ts
        if self._last_exit_ts > 0 and _secs_since_exit < self._reentry_cooldown:
            _wait = int(self._reentry_cooldown - _secs_since_exit)
            self._count_block("COOLDOWN")
            self._last_block_reason = f"COOLDOWN ({_wait}s remaining)"
            return None

        # ══ PREDICT-FIRST PATH ═══════════════════════════════════════════
        # ML chooses direction; structure confirms. Replaces the legacy
        # "1m SuperTrend decides, ML rubber-stamps" path below. Toggle with
        # PREDICT_FIRST=0 to fall back to the legacy gate (e.g. if reverting
        # to the old saturated models).
        if self._predict_first:
            return self._check_entry_predict_first(df_window, features, ts)

        # ══ STEP 3: Direction gate (LEGACY — PREDICT_FIRST=0) ════════════
        direction_bias = self._direction_bias
        vwap_confirms  = getattr(self, "_vwap_confirms", False)
        _dir_str = "BULL" if direction_bias == 1 else ("BEAR" if direction_bias == -1 else "NONE")
        _vwap_str = "VWAP✓" if vwap_confirms else "VWAP✗"
        logger.info(f"[DIRECTION] dir={_dir_str} ST={features.get('supertrend_dir',0):.0f} {_vwap_str} pvwap={features.get('price_vs_vwap',0):.4f} CE={self._last_ce_adj:.3f} PE={self._last_pe_adj:.3f}")
        if direction_bias == 0:
            self._count_block("NO_DIRECTION")
            self._last_block_reason = "NO_DIRECTION (ST=0)"
            return None

        # ── Higher-timeframe (5m) confirmation — anti-noise entry gate ───
        # Block when the slow 5m SuperTrend OPPOSES the 1m direction (we are
        # about to trade against the prevailing trend = the main reason trades
        # go negative on entry). htf5_dir==0 means not enough data → allow.
        htf5 = self._htf5_dir
        if (direction_bias == 1 and htf5 == -1) or (direction_bias == -1 and htf5 == 1):
            self._count_block("HTF5_OPPOSES")
            self._last_block_reason = f"HTF5_OPPOSES (1m={direction_bias} 5m={htf5})"
            return None

        # ══ STEP 4: Signal thresholds + ORB breakout ════════════════════
        orb_ok = bool(
            self._day_clf and self._day_classified and self._day_clf.should_trade_orb()
        )

        price      = df_window["close"].iloc[-1]
        
        # ── FIX A: Update swing structure (HH/LL) ──────────────────────
        self._update_swings_and_structure(
            df_window["high"].iloc[-1], df_window["low"].iloc[-1], price
        )

        # ── FIX D: Trap filter — block if recent breakout failed ─────────
        if not self._check_trap_filter(df_window, "CE", price):
            self._count_block("TRAP_FILTER")
            self._last_block_reason = "TRAP_FILTER (CE breakout failed)"
            return None
        if not self._check_trap_filter(df_window, "PE", price):
            self._count_block("TRAP_FILTER")
            self._last_block_reason = "TRAP_FILTER (PE breakout failed)"
            return None

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
            # Block CE if price is more than 0.15% below VWAP (tight proximity allowed).
            # pvwap = (close - vwap) / close; -0.0015 ≈ 1.5 pts on NIFTY at 24000.
            _pvwap_now = features.get("price_vs_vwap", 0.0)
            _VWAP_TOLERANCE = -0.0015
            if direction_bias == 1 and not vwap_confirms and _pvwap_now < _VWAP_TOLERANCE:
                self._count_block("VWAP_FAIL")
                self._last_block_reason = f"CE_VWAP_FAIL (pvwap={_pvwap_now:.4f} < {_VWAP_TOLERANCE})"
                ce_adj = 0.0
            elif direction_bias == 1 and not vwap_confirms and _pvwap_now >= _VWAP_TOLERANCE:
                # Price is within tolerance — allow entry only if CE is high conviction
                if ce_adj < 0.70:
                    self._count_block("VWAP_NEAR_MISS")
                    self._last_block_reason = f"CE_VWAP_NEAR (pvwap={_pvwap_now:.4f}, CE={ce_adj:.3f}<0.70)"
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
                self._last_htf_bullish = _htf_bullish   # expose for journal / market_state
                # High-conviction override: CE ≥ 0.93 with sustained BULL can bypass HTF gate.
                # Rationale: EMA30/EMA60 lag by definition — a very high ML prob with OI
                # confirmation often signals a real trend reversal, not a dead-cat bounce.
                _htf_override = ce_adj >= 0.93
                if not _htf_bullish and not _htf_override:
                    self._count_block("HTF_FAIL")
                    self._last_block_reason = "CE_HTF_FAIL (30m EMA bearish vs 60m)"
                    _ema30_v = float(np.mean(_closes[-30:])) if len(_closes) >= 60 else 0
                    _ema60_v = float(np.mean(_closes[-60:])) if len(_closes) >= 60 else 0
                    logger.info(f"[BLOCK] CE_HTF_FAIL — ema30={_ema30_v:.1f} ema60={_ema60_v:.1f} (bearish slope, CE skipped)")
                    ce_adj = 0.0
                elif not _htf_bullish and _htf_override:
                    logger.info(f"[HTF OVERRIDE] CE={ce_adj:.3f}≥0.93 — bypassing HTF gate (EMA bearish but high-conviction signal)")
                else:
                    _pure_ml_ce = not ce_breakout
                    # On RANGE days, require extra-high CE confidence — model is noisy in chop
                    _regime_str = str(self.learner.get_day_type()).upper()
                    _is_range   = "RANGE" in _regime_str or _regime_str in ("UNKNOWN", "")
                    _ce_floor   = 0.85 if (_pure_ml_ce and _is_range) else _CE_ML_FLOOR
                    ce_thr = max(threshold - 0.03 if (ce_breakout and orb_ok) else threshold, _ce_floor)
                    # ── CE relative-strength gate (model is CE-saturated) ──
                    # The CE model outputs 0.80-0.96 nearly all day, so the
                    # absolute floor barely discriminates. Require the current
                    # CE prob to rank in the top 30% of today's own CE
                    # distribution before firing a pure-ML CE. Needs >=50
                    # samples; otherwise it is skipped (no over-restriction
                    # early in the session).
                    _ce_pctile = self._ml_percentile(self._last_ce_prob)
                    if (_pure_ml_ce and len(self._ml_history) >= 50
                            and _ce_pctile < 70):
                        self._count_block("CE_WEAK_RANK")
                        self._last_block_reason = (
                            f"CE_WEAK_RANK (pctile={_ce_pctile}<70)"
                        )
                        ce_adj = 0.0
                    if ce_adj >= ce_thr:
                        blocked, reason_block = self.learner.is_side_blocked("CE")
                        if blocked:
                            self._count_block("ML_BLOCKED")
                            self._last_block_reason = f"CE_BLOCKED ({reason_block})"
                        else:
                            reason = "ML_CE" if _pure_ml_ce else "ORB+ML_CE"
                            self._last_block_reason = f"SIGNAL_FIRE ({reason})"
                            signal = {"side": "CE", "ml_prob": ce_adj,
                                      "features": features, "reason": reason}
                    else:
                        self._count_block("CE_WEAK")
                        self._last_block_reason = (
                            f"CE_WEAK (ce_adj={ce_adj:.2f} < floor={ce_thr:.2f})"
                        )

        # PE (only if CE not triggered)
        if signal is None:
            # ── FIX A: Structure confirmation (LH/LL) ──────────────────────
            if not self._structure_confirmed:
                self._count_block("NO_STRUCTURE")
                self._last_block_reason = "NO_STRUCTURE (waiting for LH/LL confirmation)"
                pe_adj = 0.0
            # ── FIX C: HTF 15m/30m trend alignment ────────────────────────
            elif not self._htf_trend_aligned(-1):  # -1 for PE/bearish
                self._count_block("HTF_MISALIGN")
                self._last_block_reason = "HTF_MISALIGN (15m/30m trend not bearish)"
                pe_adj = 0.0
            # ── FIX B: Pullback entry — wait for retrace ──────────────────
            elif not self._check_pullback_entry(df_window, "PE", price):
                self._count_block("PULLBACK_WAIT")
                self._last_block_reason = "PULLBACK_WAIT (waiting for retrace)"
                pe_adj = 0.0
            else:
                pe_thr = threshold - 0.03 if (pe_breakout and orb_ok) else threshold
                if pe_adj >= pe_thr:
                    blocked, reason_block = self.learner.is_side_blocked("PE")
                    if blocked:
                        self._count_block("ML_BLOCKED")
                        self._last_block_reason = f"PE_BLOCKED ({reason_block})"
                    else:
                        reason = "ORB+ML_PE" if pe_breakout else "ML_PE"
                        self._last_block_reason = f"SIGNAL_FIRE ({reason})"
                        signal = {"side": "PE", "ml_prob": pe_adj,
                                  "features": features, "reason": reason}

        if signal is None:
            # F3: count final block reason (signal evaluated but nothing fired)
            _br = self._last_block_reason
            if   "VWAP"    in _br: _bk = "VWAP_FAIL"
            elif "HTF"     in _br: _bk = "HTF_FAIL"
            elif "BLOCKED" in _br: _bk = "ML_BLOCKED"
            elif "WEAK"    in _br: _bk = "ML_BELOW_THR"
            elif "ML_BELOW" in _br: _bk = "ML_BELOW_THR"
            else:                  _bk = "ML_BELOW_THR"
            self._count_block(_bk)
            return None

        # ══ STEP 6: Risk + expected PnL guard ═══════════════════════════
        return self._finalize_signal(signal, features, price)

    def _finalize_signal(self, signal: dict, features: dict, price: float):
        """
        STEP 6 — attach risk stops + expected-PnL guard, then return the
        signal (or None if it fails the guard). Shared by both the legacy
        direction-gate path and the predict-first path.
        """
        atr_val  = features.get("atr", price * 0.01)
        day_type = self.learner.get_day_type()
        regime   = (
            "EXPANSION" if "VOLATILE" in day_type else
            "TREND"     if "TREND"    in day_type else
            "RANGE"
        )

        # ── TIER 1: Wider initial stop using INITIAL_SL_MULT (default 1.5x) ──
        _cfg = getattr(self.ctx, "config", None)
        _sl_mult = float(getattr(_cfg, "INITIAL_SL_MULT", 1.5)) if _cfg else 1.5

        # Temporarily scale ATR for stop calculation to apply INITIAL_SL_MULT
        scaled_atr = atr_val * _sl_mult
        side = signal.get("side", "CE")
        stop_loss, target, stop_pct = compute_entry_stops(price, scaled_atr, regime, side=side)
        signal.update(stop_loss=stop_loss, target=target,
                      stop_pct=stop_pct, regime=regime)

        lot_size     = self.ctx.config.LOT_SIZE
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

        # ── TIER 1: Entry slippage guard — skip if bid/ask spread > MAX_ENTRY_SLIP_PTS ──
        _max_slip = float(getattr(_cfg, "MAX_ENTRY_SLIP_PTS", 1.0)) if _cfg else 1.0
        _bid, _ask = self.ctx.broker.get_bid_ask(signal.get("symbol", ""))
        if _bid is not None and _ask is not None:
            _spread = _ask - _bid
            if _spread > _max_slip:
                self._count_block("ENTRY_SLIPPAGE")
                self._last_block_reason = f"ENTRY_SLIPPAGE (spread={_spread:.2f} > {_max_slip})"
                return None

        logger.info(
            f"[ENTRY SIGNAL] {signal['side']} | reason={signal['reason']} | "
            f"prob={signal['ml_prob']:.3f} | SL={stop_loss:.2f} | TP={target:.2f} | "
            f"ExpPnL=Rs{expected_pnl:.0f} | SL_mult={_sl_mult}x"
        )
        return signal

    def _check_entry_predict_first(self, df_window, features, ts):
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
          5. FIX A: Structure confirmation (HH/HL for CE, LH/LL for PE)
          6. FIX C: HTF 15m/30m trend alignment
          7. FIX B: Pullback entry — wait for retrace after breakout
          8. FIX D: Trap filter — block if recent breakout failed
          9. learner side-block + finalize (risk/PnL guard).
        """
        price  = df_window["close"].iloc[-1]
        ce_adj = self._last_ce_adj
        pe_adj = self._last_pe_adj
        htf5   = self._htf5_dir
        pvwap  = features.get("price_vs_vwap", 0.0)
        _cfg   = getattr(self.ctx, "config", None)

        # ── FIX A: Update swing structure (HH/LL) ──────────────────────
        self._update_swings_and_structure(
            df_window["high"].iloc[-1], df_window["low"].iloc[-1], price
        )

        # Per-side thresholds: use the learner's adaptive threshold (base from
        # CHAMPION_THRESHOLD=0.35 + day-type adjustment, clamped 0.45-0.56).
        # The predictor's calibrated threshold FILES (0.79) are stale for the
        # de-saturated models — live probs top out ~0.46, so the file threshold
        # made ML unfirable (ML_BELOW_THR forever). Learner adaptive only.
        learn_thr = self.learner.get_ml_threshold()
        ce_thr = learn_thr
        pe_thr = learn_thr

        # 1. PREDICT direction
        side   = "CE" if ce_adj >= pe_adj else "PE"
        prob   = ce_adj if side == "CE" else pe_adj
        thr    = ce_thr if side == "CE" else pe_thr
        other  = pe_adj if side == "CE" else ce_adj

        logger.info(
            f"[PREDICT-FIRST] CE={ce_adj:.3f} PE={pe_adj:.3f} -> {side} "
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

        # 4. CONFIRM — 5m trend must AGREE (htf5==0 = insufficient data → allow)
        # Conditional on REQUIRE_5M_TREND config (default True)
        _require_5m = bool(getattr(_cfg, "REQUIRE_5M_TREND", True)) if _cfg else True
        if _require_5m:
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
        # Conditional on REQUIRE_VWAP_ALIGN config (default True)
        _require_vwap = bool(getattr(_cfg, "REQUIRE_VWAP_ALIGN", True)) if _cfg else True
        _VWAP_TOL = 0.0015
        if _require_vwap:
            if side == "CE" and pvwap < -_VWAP_TOL:
                self._count_block("VWAP_FAIL")
                self._last_block_reason = f"CE_VWAP_FAIL (pvwap={pvwap:.4f} below VWAP)"
                return None
            if side == "PE" and pvwap > _VWAP_TOL:
                self._count_block("VWAP_FAIL")
                self._last_block_reason = f"PE_VWAP_FAIL (pvwap={pvwap:.4f} above VWAP)"
                return None

        # 5. FIX A: Structure confirmation (HH/HL for CE, LH/LL for PE)
        if not self._structure_confirmed:
            self._count_block("NO_STRUCTURE")
            self._last_block_reason = f"NO_STRUCTURE ({side} waiting for HH/LL confirmation)"
            return None

        # 6. FIX C: HTF 15m/30m trend alignment
        direction = 1 if side == "CE" else -1
        if not self._htf_trend_aligned(direction):
            self._count_block("HTF_MISALIGN")
            self._last_block_reason = f"HTF_MISALIGN (15m/30m trend opposes {side})"
            return None

        # 6b. ENTRY QUALITY — rejection-first entry-timing / trade-quality
        # gate (shared with scalp path). Entry proceeds ONLY if no rule fires.
        _eq_cost = round_trip_cost(
            getattr(_cfg, "LOT_SIZE", 30), _cfg) if _cfg is not None else None
        eq = compute_entry_quality(
            df_window, side, price, ts,
            {"breakout_ts": self._breakout_ts, "orb_done": self.orb_done},
            cost_rs=_eq_cost,
        )
        if not eq["accepted"]:
            self._count_block(eq["reason"])
            self._last_block_reason = eq["reason"]
            return None

        # 7. FIX B: Pullback entry — wait for retrace after breakout
        if not self._check_pullback_entry(df_window, side, price):
            self._count_block("PULLBACK_WAIT")
            self._last_block_reason = f"PULLBACK_WAIT ({side} waiting for retrace)"
            return None

        # 8. FIX D: Trap filter — block if recent breakout failed
        if not self._check_trap_filter(df_window, side, price):
            self._count_block("TRAP_FILTER")
            self._last_block_reason = f"TRAP_FILTER ({side} breakout failed)"
            return None

        # 9. learner side-block (consecutive-loss / losing-side lock)
        blocked, reason_block = self.learner.is_side_blocked(side)
        if blocked:
            self._count_block("ML_BLOCKED")
            self._last_block_reason = f"{side}_BLOCKED ({reason_block})"
            return None

        self._last_block_reason = f"SIGNAL_FIRE (ML_{side})"
        signal = {"side": side, "ml_prob": prob,
                  "features": features, "reason": f"ML_{side}",
                  "entry_quality": eq["metrics"]}
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
            "htf_bullish":     getattr(self, "_last_htf_bullish", True),
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
            # F3 — block analytics
            "block_counts":    dict(self._block_counts),
            # F4 — ML edge
            "ml_edge":         self._last_ml_edge,
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
        # SINGLE position-size source of truth: use the ACTUAL filled qty so
        # max_pnl/MFE is consistent with realized PnL and MAE (which use qty).
        size      = position["qty"]
        stop_loss = position.get("stop_loss", entry * 0.90)
        max_pnl   = position.get("max_pnl", 0.0)
        ml_prob   = position.get("ml_prob", 0.5)
        side      = position.get("side", "CE")

        # ── profit_manager handles trailing + lock system ─────────────
        # Pass config for Tier 2 params (TRAIL_ACTIVATION_PTS, etc.)
        new_sl, new_max_pnl, pm_reason, scale_out_info = manage_position(
            entry_price=entry,
            ltp=ltp,
            lot_size=size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
            target=position.get("target"),
            config=getattr(self.ctx, "config", None),
            side=side,
        )

        # Update position dict in-place so caller persists new SL
        position["stop_loss"] = new_sl
        position["max_pnl"]   = new_max_pnl

        # Handle scale-out signal
        if scale_out_info:
            position["_scale_out"] = scale_out_info
            logger.info(
                f"[SCALE OUT] Triggered at +{scale_out_info['trigger_pts']:.1f}pt "
                f"| scaling out {scale_out_info['pct']*100:.0f}% of position"
            )

        # Track highest ladder rung for diagnostics journal
        from engine.execution.profit_manager import ladder_locked_rs
        _lrs, _lstage = ladder_locked_rs(new_max_pnl, size)
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
