# backtest/backtest_engine.py
# Institutional-grade backtesting engine
# Reuses: LiveEngine, feature_config, profit_manager, risk_manager
# NO logic duplication — calls the same functions as live system

import os
import sys
import logging
import warnings
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Project root on path ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.feature_config import _safe_build_live_features, FEATURE_COLUMNS
from ml.predictor_champion import ChampionPredictor
from ml.ml_intraday_learner import IntradayMLLearner
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx, VWAPAccumulator
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops
from backtest.option_pricer import OptionPriceSimulator

logger = logging.getLogger("backtest")

# ── Market constants ──────────────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)
_DAY_CLASS_AT = dtime(9, 45)
_MARKET_CLOSE = dtime(15, 30)
_NO_ENTRY_AFTER = dtime(15, 15)

# Institutional session filter — avoid lunch-hour chop (11:00–12:30)
_LUNCH_START    = dtime(11,  0)
_LUNCH_END      = dtime(12, 30)

# Minimum expected PnL for any new trade (₹150 safeguard)
_MIN_EXPECTED_PNL = 150.0

# ML floor: never trade below 0.65 (backtest: PE@0.65 = 58% WR, avg +Rs174).
# Old 0.62 floor: 52.6% WR. Raising to 0.65 improves both WR and avg trade.
_MIN_ML_FLOOR = 0.65

BANKNIFTY_LOT_SIZE = 30      # BANKNIFTY lot size (default, overridden by config)
OPTIONS_PREMIUM_PROXY = True  # simulate option price as % of spot


def _lot_size_for_date(d, config: dict = None) -> int:
    """BANKNIFTY lot size from config or default."""
    if config and "LOT_SIZE" in config:
        return int(config["LOT_SIZE"])
    return BANKNIFTY_LOT_SIZE


def _mins_to_close(ts) -> float:
    """Minutes from ts until market close (≥1)."""
    return max(
        (_MARKET_CLOSE.hour * 60 + _MARKET_CLOSE.minute) -
        (ts.hour * 60 + ts.minute),
        1,
    )


# ══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    trade_id:       int
    date:           str
    entry_time:     datetime
    exit_time:      Optional[datetime]
    side:           str          # CE or PE
    entry_price:    float
    exit_price:     float
    qty:            int
    lot_size:       int
    pnl:            float
    ml_prob:        float
    entry_reason:   str
    exit_reason:    str
    regime:         str
    orb_high:       Optional[float]
    orb_low:        Optional[float]
    held_candles:   int
    held_seconds:   float
    max_pnl:        float
    stop_loss:      float
    target:         float
    features:       dict = field(default_factory=dict, repr=False)


@dataclass
class DayStats:
    date:           str
    trades:         int   = 0
    pnl:            float = 0.0
    wins:           int   = 0
    losses:         int   = 0
    day_type:       str   = "UNKNOWN"
    killed:         bool  = False   # daily loss limit hit


# ══════════════════════════════════════════════════════════════════════
# OPTION PRICE SIMULATOR
# Used since we backtest on NIFTY spot (no options OHLC needed)
# ══════════════════════════════════════════════════════════════════════


_opt_sim = OptionPriceSimulator()


# ══════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE  (reuses live_engine logic without broker/context)
# ══════════════════════════════════════════════════════════════════════

class BacktestSignalEngine:
    """
    Mirrors LiveEngine.step() but works with pure DataFrames.
    Calls the SAME feature builder, predictor, risk_manager, profit_manager.
    """

    def __init__(self, config: dict):
        self.config    = config
        self.predictor = ChampionPredictor()
        self.learner   = IntradayMLLearner()

        # ORB state (reset per day)
        self.orb_high: Optional[float] = None
        self.orb_low:  Optional[float] = None
        self.orb_done: bool            = False
        self.orb_ce_fired: bool        = False   # one-shot: locks after first CE ORB signal
        self.orb_pe_fired: bool        = False   # one-shot: locks after first PE ORB signal

        # VWAP accumulator (reset per day)
        self._vwap = VWAPAccumulator()

        # Current direction bias (+1 bullish, -1 bearish, 0 unclear)
        self._direction_bias: int = 0

        # Day classifier (optional)
        self._day_clf          = None
        self._day_classified   = False
        self._day_candles_30m  = []
        self._prev_close: Optional[float] = None
        self._open_price_set   = False

        # ═══════════════════════════════════════════════════════
        # TELEMETRY / PIPELINE OBSERVABILITY
        # Tracks where signals are generated, filtered, blocked,
        # rejected, and executed.
        # ═══════════════════════════════════════════════════════
        self.telemetry = {

            # Raw ORB breakout detections
            "ce_raw_signals": 0,
            "pe_raw_signals": 0,

            # Passed ML threshold
            "ce_ml_pass": 0,
            "pe_ml_pass": 0,

            # Blocked by learner logic
            "ce_blocked": 0,
            "pe_blocked": 0,

            # Successfully executed trades
            "ce_executed": 0,
            "pe_executed": 0,

            # Diagnostics
            "signals_returned": 0,
            "signals_none": 0,

            # Day stats
            "trend_candles": 0,
            "range_candles": 0,
            "volatile_candles": 0,
            "unknown_candles": 0,
        }

        try:
            from ml.day_classifier import DayClassifier
            self._day_clf = DayClassifier()
            logger.info("[BacktestSignalEngine] DayClassifier loaded")

        except Exception as e:
            logger.warning(
                f"[BacktestSignalEngine] DayClassifier unavailable: {e}"
            )
    # ── Day reset ─────────────────────────────────────────────────────

    def reset_day(self, prev_close: Optional[float] = None):
        self.orb_high        = None
        self.orb_low         = None
        self.orb_done        = False
        self.orb_ce_fired    = False
        self.orb_pe_fired    = False
        self._day_classified = False
        self._day_candles_30m = []
        self._prev_close     = prev_close
        self._open_price_set = False
        self._clf_day_type   = None
        self._vwap.reset()
        self._direction_bias = 0
        self.learner.reset_day()

    # ── ORB ───────────────────────────────────────────────────────────

    def _update_orb(self, row: pd.Series, ts: datetime):
        now = ts.time()
        if self.orb_done:
            return
        if _MARKET_OPEN <= now < _ORB_END:
            if self.orb_high is None:
                self.orb_high, self.orb_low = row["high"], row["low"]
            else:
                self.orb_high = max(self.orb_high, row["high"])
                self.orb_low  = min(self.orb_low,  row["low"])
        elif now >= _ORB_END:
            self.orb_done = True

    # ── Day classification ────────────────────────────────────────────

    def _maybe_classify_day(self, row: pd.Series, ts: datetime):
        now = ts.time()
        self.learner.update_candle(row["close"], row["high"], row["low"], ts)

        # Accumulate VWAP from market open
        self._vwap.update(
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0)),
        )

        if not self._day_classified and now < _DAY_CLASS_AT:
            self._day_candles_30m.append({
                "open": row["open"], "high": row["high"],
                "low": row["low"],  "close": row["close"], "volume": row.get("volume", 0),
            })

        if not self._day_classified and now >= _DAY_CLASS_AT:
            self._day_classified = True
            if self._day_clf and len(self._day_candles_30m) >= 10:
                df30 = pd.DataFrame(self._day_candles_30m)
                self._clf_day_type = self._day_clf.classify(df30, self._prev_close)

    # ── Feature builder ───────────────────────────────────────────────

    def _build_features(self, window: pd.DataFrame, ts: datetime) -> Optional[dict]:
        if len(window) < 26:
            return None

        closes  = window["close"].tolist()
        opens   = window["open"].tolist()
        highs   = window["high"].tolist()
        lows    = window["low"].tolist()
        volumes = window["volume"].tolist() if "volume" in window.columns else [0] * len(closes)

        signal = self._compute_signal(closes, highs, lows)
        feats  = _safe_build_live_features(closes, opens, highs, lows, volumes, signal, ts=ts)

        missing = [f for f in FEATURE_COLUMNS if f not in feats]
        if missing:
            return None

        return feats

    def _compute_signal(self, closes, highs, lows) -> dict:
        import numpy as np

        def ema(series, span):
            alpha, val = 2 / (span + 1), series[0]
            for p in series[1:]: val = p * alpha + val * (1 - alpha)
            return val

        n    = len(closes)
        e20  = ema(closes[-min(n, 60):], 20)
        e50  = ema(closes[-min(n, 100):], 50) if n >= 50 else e20

        if n >= 15:
            g, l = [], []
            for i in range(-14, 0):
                d = closes[i] - closes[i-1]
                (g if d > 0 else l).append(abs(d))
            rsi = 100 - (100 / (1 + np.mean(g or [1e-6]) / np.mean(l or [1e-6])))
        else:
            rsi = 50.0

        if n >= 15:
            h = np.array(highs[-14:]); lw = np.array(lows[-14:]); c = np.array(closes[-14:])
            tr = [max(h[i]-lw[i], abs(h[i]-c[i-1]), abs(lw[i]-c[i-1])) for i in range(1,14)]
            atr = max(float(np.mean(tr)), 0.5)
        else:
            atr = max(abs(closes[-1] - closes[-2]) * 14**0.5, 0.5)

        # ── Supertrend (10/3) over the rolling window ─────────────────
        st_dir, st_line = _compute_supertrend(
            np.array(highs, dtype=float),
            np.array(lows, dtype=float),
            np.array(closes, dtype=float),
            period=10, multiplier=3.0,
        )
        last_st_dir  = int(st_dir[-1])
        last_st_line = float(st_line[-1])
        st_dist      = (closes[-1] - last_st_line) / closes[-1] if closes[-1] != 0 else 0.0

        # ── ADX (14) over the rolling window ─────────────────────────
        adx_arr, di_plus, di_minus = _compute_adx(
            np.array(highs, dtype=float),
            np.array(lows, dtype=float),
            np.array(closes, dtype=float),
            period=14,
        )
        last_adx      = float(adx_arr[-1])
        last_di_plus  = float(di_plus[-1])
        last_di_minus = float(di_minus[-1])
        di_spread     = last_di_plus - last_di_minus

        # ── VWAP bias ────────────────────────────────────────────────
        vwap_val    = self._vwap.value
        price_vs_vwap = (closes[-1] - vwap_val) / closes[-1] if (closes[-1] != 0 and vwap_val > 0) else 0.0

        # ── Direction bias (hard gate) ────────────────────────────────
        # Bullish: Supertrend=UP AND price above VWAP
        # Bearish: Supertrend=DOWN AND price below VWAP
        if last_st_dir == 1 and price_vs_vwap > 0:
            self._direction_bias = 1
        elif last_st_dir == -1 and price_vs_vwap < 0:
            self._direction_bias = -1
        else:
            self._direction_bias = 0

        return {
            "ema20":          e20,
            "ema50":          e50,
            "rsi_1m":         rsi,
            "atr":            atr,
            "trend_strength": (e20 - e50) / closes[-1] if closes[-1] else 0.0,
            # Direction stack — NEW
            "supertrend_dir":  float(last_st_dir),
            "supertrend_dist": float(np.clip(st_dist, -0.05, 0.05)),
            "price_vs_vwap":   float(np.clip(price_vs_vwap, -0.05, 0.05)),
            "adx":             float(np.clip(last_adx, 0, 100)),
            "di_spread":       float(np.clip(di_spread, -60, 60)),
            "ema_alignment":   float(1.0 if e20 > e50 else -1.0),
        }

    # ── Main step ─────────────────────────────────────────────────────

    def step(self, window: pd.DataFrame, ts: datetime) -> Optional[dict]:
        """
        Returns signal dict or None.
        Identical contract to LiveEngine.step().
        """

        row = window.iloc[-1]
        now = ts.time()

        # ─────────────────────────────────────────────────────
        # OPEN PRICE INIT
        # ─────────────────────────────────────────────────────
        if not self._open_price_set and now >= _MARKET_OPEN:
            self.learner.set_open_price(row["close"])
            self._open_price_set = True

        self._update_orb(row, ts)
        self._maybe_classify_day(row, ts)

        # ─────────────────────────────────────────────────────
        # TIME GATES
        # ─────────────────────────────────────────────────────
        if now < _ORB_END:
            self.telemetry["signals_none"] += 1
            return None

        if now >= _NO_ENTRY_AFTER:
            self.telemetry["signals_none"] += 1
            return None

        # ─────────────────────────────────────────────────────
        # SESSION FILTER — no new entries during lunch chop
        # Institutional desks avoid 11:00–14:00 on NIFTY options.
        # Thin order flow → fake breakouts → high cost of carry.
        # ─────────────────────────────────────────────────────
        if _LUNCH_START <= now < _LUNCH_END:
            self.telemetry["signals_none"] += 1
            return None

        # ─────────────────────────────────────────────────────
        # DAY TYPE GATE
        # ─────────────────────────────────────────────────────
        day = self.learner.get_day_type()

        # Optional hard block
        # if day == "VOLATILE_DAY":
        #     return None

        # Track day stats
        if "TREND" in day:
            self.telemetry["trend_candles"] += 1
        elif "RANGE" in day:
            self.telemetry["range_candles"] += 1
        elif "VOLATILE" in day:
            self.telemetry["volatile_candles"] += 1
        else:
            self.telemetry["unknown_candles"] += 1

        # ─────────────────────────────────────────────────────
        # FEATURE BUILD
        # ─────────────────────────────────────────────────────
        features = self._build_features(window, ts)

        if not features:
            self.telemetry["signals_none"] += 1
            return None

        price = row["close"]

        # Adaptive threshold — never below ML floor (0.62 filters losers)
        threshold = max(self.learner.get_ml_threshold(), _MIN_ML_FLOOR)

        # ─────────────────────────────────────────────────────
        # DIRECTION GATE — Institutional rule: no counter-trend
        # trades. The ML model is only allowed to recommend a
        # direction that AGREES with Supertrend+VWAP consensus.
        # This single gate eliminated ~60% of CE losses in
        # the audit (CE was taken on DOWN days with 33% WR).
        # ─────────────────────────────────────────────────────
        direction_bias = self._direction_bias  # computed in _compute_signal()

        # If market has no clear direction, skip to avoid random-walk noise
        if direction_bias == 0:
            logger.debug("[DIRECTION] No clear bias — skipping candle")
            self.telemetry["signals_none"] += 1
            return None

        # ─────────────────────────────────────────────────────
        # ML PREDICTIONS
        # ─────────────────────────────────────────────────────
        ce_prob = self.predictor.predict(features, "CE")
        pe_prob = self.predictor.predict(features, "PE")

        if ce_prob is None or pe_prob is None:
            self.telemetry["signals_none"] += 1
            return None

        ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(
            ce_prob,
            pe_prob,
            "CE"
        )

        # Hard direction gate: zero out the non-aligned side
        if direction_bias != 1:
            ce_adj = 0.0   # market is bearish — no CE
        if direction_bias != -1:
            pe_adj = 0.0   # market is bullish — no PE

        logger.info(
            f"[ML] CE={ce_adj:.3f} "
            f"PE={pe_adj:.3f} "
            f"THR={threshold:.3f} "
            f"BIAS={'BULL' if direction_bias==1 else 'BEAR'}"
        )

        # ─────────────────────────────────────────────────────
        # ORB BREAKOUTS — one-shot per side per day
        # Volume confirmation: skip ORB signal if volume is
        # below 130% of the 20-candle average (fake breakout filter).
        # ─────────────────────────────────────────────────────
        vols    = window["volume"].values if "volume" in window.columns else np.zeros(len(window))
        avg_vol = vols[-20:].mean() if len(vols) >= 20 else 0
        cur_vol = vols[-1] if len(vols) > 0 else 0
        vol_ok  = (avg_vol <= 0) or (cur_vol > avg_vol * 1.3)  # skip if no volume data

        ce_break = (
            self.orb_done and
            self.orb_high is not None and
            price > self.orb_high and
            not self.orb_ce_fired and
            vol_ok
        )

        pe_break = (
            self.orb_done and
            self.orb_low is not None and
            price < self.orb_low and
            not self.orb_pe_fired and
            vol_ok
        )

        # Lock immediately on detection — prevents chasing at worse prices
        if ce_break:
            self.orb_ce_fired = True
        if pe_break:
            self.orb_pe_fired = True

        # ─────────────────────────────────────────────────────
        # TELEMETRY — RAW SIGNALS
        # ─────────────────────────────────────────────────────
        if ce_break:
            self.telemetry["ce_raw_signals"] += 1
            logger.info(
                f"[RAW CE BREAK] "
                f"price={price:.2f} "
                f"orb_high={self.orb_high:.2f}"
            )

        if pe_break:
            self.telemetry["pe_raw_signals"] += 1
            logger.info(
                f"[RAW PE BREAK] "
                f"price={price:.2f} "
                f"orb_low={self.orb_low:.2f}"
            )

        signal = None

        # ═════════════════════════════════════════════════════
        # CE CHECK — ORB CONFIRMATION REQUIRED
        # ─────────────────────────────────────────────────────
        # Lesson from audit: ML_CE without ORB has 37% WR and
        # loses ₹65/trade. ORB+ML_CE has 46% WR and makes ₹80/trade.
        # CE is only taken AFTER an ORB breakout confirmed this session.
        # orb_ce_fired=True once today's CE breakout is detected, so
        # the first ORB trade AND subsequent ML_CE trades that day
        # are both allowed. Days with no ORB upbreak → no CE at all.
        # ═════════════════════════════════════════════════════
        orb_ok   = (self._clf_day_type == "TREND")
        ce_thr   = threshold - 0.03 if (ce_break and orb_ok) else threshold

        # Hard gate: CE only on the ORB breakout candle itself.
        # Follow-on ML_CE (same day after ORB) has 37% WR and loses.
        # The ORB breakout moment has the best entry timing and momentum.
        if not ce_break:
            ce_adj = 0.0   # block all CE except the actual ORB breakout

        if ce_adj >= ce_thr:

            self.telemetry["ce_ml_pass"] += 1

            blocked, reason_block = self.learner.is_side_blocked("CE")

            if blocked:

                self.telemetry["ce_blocked"] += 1

                logger.info(
                    f"[BLOCKED] CE blocked: {reason_block}"
                )

            else:

                atr_val = features.get("atr", price * 0.01)

                day_type = self.learner.get_day_type()

                regime = (
                    "EXPANSION" if "VOLATILE" in day_type else
                    "TREND"     if "TREND" in day_type else
                    "RANGE"
                )

                stop_loss, target, stop_pct = compute_entry_stops(
                    price,
                    atr_val,
                    regime
                )

                signal = {
                    "side": "CE",
                    "ml_prob": ce_adj,
                    "features": features,
                    "reason": "ORB+ML_CE" if ce_break else "ML_CE",
                    "stop_loss": stop_loss,
                    "target": target,
                    "stop_pct": stop_pct,
                    "regime": regime,
                }

        else:

            logger.info(
                f"[CE FAIL] "
                f"ce_adj={ce_adj:.3f} "
                f"ce_thr={ce_thr:.3f}"
            )

        # ═════════════════════════════════════════════════════
        # PE CHECK
        # ═════════════════════════════════════════════════════
        if signal is None:

            pe_thr = threshold - 0.03 if (pe_break and orb_ok) else threshold
            if pe_adj >= pe_thr:

                self.telemetry["pe_ml_pass"] += 1

                blocked, reason_block = self.learner.is_side_blocked("PE")

                if blocked:

                    self.telemetry["pe_blocked"] += 1

                    logger.info(
                        f"[BLOCKED] PE blocked: {reason_block}"
                    )

                else:

                    atr_val = features.get("atr", price * 0.01)

                    day_type = self.learner.get_day_type()

                    regime = (
                        "EXPANSION" if "VOLATILE" in day_type else
                        "TREND"     if "TREND" in day_type else
                        "RANGE"
                    )

                    stop_loss, target, stop_pct = compute_entry_stops(
                        price,
                        atr_val,
                        regime
                    )

                    signal = {
                        "side": "PE",
                        "ml_prob": pe_adj,
                        "features": features,
                        "reason": "ORB+ML_PE" if pe_break else "ML_PE",
                        "stop_loss": stop_loss,
                        "target": target,
                        "stop_pct": stop_pct,
                        "regime": regime,
                    }

            else:

                logger.info(
                    f"[PE FAIL] "
                    f"pe_adj={pe_adj:.3f} "
                    f"pe_thr={pe_thr:.3f}"
                )

        # ─────────────────────────────────────────────────────
        # MINIMUM EXPECTED PnL GUARD — ₹150 capital safeguard
        # Only enter if probability-weighted expected PnL >= ₹150.
        # Prevents entering trades where the math doesn't work.
        # ─────────────────────────────────────────────────────
        if signal is not None:
            prob    = signal["ml_prob"]
            sl      = signal["stop_loss"]
            tgt     = signal["target"]
            lot_sz  = self.config.get("LOT_SIZE", BANKNIFTY_LOT_SIZE)
            exp_win  = (tgt - price) * lot_sz
            exp_loss = (price - sl) * lot_sz
            expected_pnl = prob * exp_win - (1 - prob) * exp_loss
            if expected_pnl < _MIN_EXPECTED_PNL:
                logger.info(
                    f"[PNL GUARD] Expected PnL Rs{expected_pnl:.0f} < Rs{_MIN_EXPECTED_PNL} — skipping"
                )
                signal = None

        # ─────────────────────────────────────────────────────
        # FINAL SIGNAL TELEMETRY
        # ─────────────────────────────────────────────────────
        if signal is not None:

            self.telemetry["signals_returned"] += 1

        else:

            self.telemetry["signals_none"] += 1

        # IMPORTANT FIX
        return signal
    
    # ── Exit check ────────────────────────────────────────────────────

    def check_exit(self, position: dict, ltp: float, held_seconds: float) -> tuple[bool, str]:
        stop_loss = position.get("stop_loss", position["entry"] * 0.90)
        max_pnl   = position.get("max_pnl",   0.0)
        ml_prob   = position.get("ml_prob",    0.5)
        lot_size  = position.get("lot_size",   BANKNIFTY_LOT_SIZE)

        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=position["entry"],
            ltp=ltp,
            lot_size=lot_size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
            target=position.get("target"),
        )
        position["stop_loss"] = new_sl
        position["max_pnl"]   = new_max_pnl

        if pm_reason:
            return True, pm_reason

        max_hold = self.config.get("MAX_HOLD_SECONDS", 300)
        if held_seconds > max_hold:
            if position["max_pnl"] < 100:
                return True, "TIME_EXIT_WEAK"

        ml_edge = ml_prob - 0.5
        early, e_reason = self.learner.should_exit_early(
            ltp=ltp, entry_price=position["entry"],
            held_seconds=held_seconds, ml_prob=ml_prob, ml_edge=ml_edge,
        )
        if early:
            return True, e_reason

        return False, ""


# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════

class BacktestEngine:

    def __init__(self, config: Optional[dict] = None):
        self.config = config or self._default_config()

        self.signal_engine = BacktestSignalEngine(self.config)

        # Results
        self.trades:    list[Trade]    = []
        self.day_stats: list[DayStats] = []
        self._trade_id  = 0

        logger.info("[BacktestEngine] Initialized")

    # ── Default config ────────────────────────────────────────────────

    @staticmethod
    def _default_config() -> dict:
        return {
            "INITIAL_CAPITAL":    100_000,
            "DAILY_LOSS_LIMIT":   -2_000,
            "MAX_TRADES_PER_DAY": 10,
            "MAX_HOLD_SECONDS":   300,
            "COOLDOWN_SECONDS":   180,
            "LOT_SIZE":           BANKNIFTY_LOT_SIZE,
            "LOTS_PER_TRADE":     1,
            "CHAMPION_THRESHOLD": float(os.getenv("CHAMPION_THRESHOLD", 0.42)),
            "ENTRY_ON":           "current_close",  # enter on signal candle close (was next_open = late entry)
        }

    # ── Data loading (once) ───────────────────────────────────────────

    def load_data(self, csv_path: str) -> pd.DataFrame:
        logger.info(f"[DATA] Loading: {csv_path}")
        df = pd.read_csv(csv_path)

        date_col = next(
            (c for c in ["date", "datetime", "timestamp", "time"] if c in df.columns),
            None
        )
        if date_col is None:
            raise ValueError("No date column found in CSV")

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).rename(columns={date_col: "date"})
        df = df.sort_values("date").reset_index(drop=True)

        # Ensure OHLCV columns
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                df[col] = df["close"] if "close" in df.columns else 0.0

        if "volume" not in df.columns:
            df["volume"] = 0

        df["date"] = pd.to_datetime(df["date"])
        df["day"]  = df["date"].dt.date

        logger.info(f"[DATA] Loaded {len(df):,} rows | "
                    f"{df['day'].nunique()} trading days | "
                    f"{df['date'].min()} → {df['date'].max()}")
        return df

    # ── Main run ──────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame, warmup_candles: int = 100) -> dict:
        """
        Run full backtest on pre-loaded DataFrame.
        warmup_candles: candles needed before signals start (feature warmup).
        """
        self.trades    = []
        self.day_stats = []
        self._trade_id = 0

        all_days     = sorted(df["day"].unique())
        equity       = self.config["INITIAL_CAPITAL"]
        total_pnl    = 0.0
        equity_peak  = equity
        max_drawdown = 0.0
        prev_close   = None

        logger.info(f"[BACKTEST] Running {len(all_days)} trading days...")

        for day_idx, trading_day in enumerate(all_days):
            day_df = df[df["day"] == trading_day].copy().reset_index(drop=True)

            # Get historical window (warmup) from before this day
            hist_df = df[df["day"] < trading_day].tail(warmup_candles)

            day_result = self._run_day(
                day_df    = day_df,
                hist_df   = hist_df,
                prev_close= prev_close,
                equity    = equity,
            )

            # Accumulate
            day_pnl   = day_result["pnl"]
            total_pnl += day_pnl
            equity    += day_pnl
            equity_peak = max(equity_peak, equity)
            dd          = equity - equity_peak
            max_drawdown = min(max_drawdown, dd)

            self.trades.extend(day_result["trades"])
            self.day_stats.append(day_result["day_stat"])

            prev_close = float(day_df["close"].iloc[-1])

            if (day_idx + 1) % 20 == 0:
                logger.info(
                    f"  Day {day_idx+1}/{len(all_days)} | "
                    f"Equity={equity:,.0f} | Trades={len(self.trades)}"
                )

        metrics = self._compute_metrics(total_pnl, equity, max_drawdown)
        logger.info("[BACKTEST] Complete")
        # ═══════════════════════════════════════════════════════
        # FINAL TELEMETRY SUMMARY
        # ═══════════════════════════════════════════════════════

        t = self.signal_engine.telemetry

        print("\n===== TELEMETRY =====")

        print(f"CE Raw Signals:              {t['ce_raw_signals']}")
        print(f"PE Raw Signals:              {t['pe_raw_signals']}")

        print(f"CE ML Pass:                  {t['ce_ml_pass']}")
        print(f"PE ML Pass:                  {t['pe_ml_pass']}")

        print(f"CE Blocked:                  {t['ce_blocked']}")
        print(f"PE Blocked:                  {t['pe_blocked']}")

        print(f"CE Executed:                 {t['ce_executed']}")
        print(f"PE Executed:                 {t['pe_executed']}")

        print(f"Signals Returned:            {t['signals_returned']}")
        print(f"Signals None:                {t['signals_none']}")

        print(f"Trend Candles:               {t['trend_candles']}")
        print(f"Range Candles:               {t['range_candles']}")
        print(f"Volatile Candles:            {t['volatile_candles']}")
        print(f"Unknown Candles:             {t['unknown_candles']}")

        # ═══════════════════════════════════════════════════════
        # DERIVED DIAGNOSTICS
        # ═══════════════════════════════════════════════════════

        try:

            ce_conversion = (
                t["ce_executed"] / max(t["ce_raw_signals"], 1)
            ) * 100

            pe_conversion = (
                t["pe_executed"] / max(t["pe_raw_signals"], 1)
            ) * 100

            print("\n===== CONVERSION RATES =====")

            print(f"CE Conversion Rate:          {ce_conversion:.2f}%")
            print(f"PE Conversion Rate:          {pe_conversion:.2f}%")
            ce_ml_conversion = (
                t["ce_executed"] / max(t["ce_ml_pass"], 1)
            ) * 100

            pe_ml_conversion = (
                t["pe_executed"] / max(t["pe_ml_pass"], 1)
            ) * 100

            print(f"CE ML Conversion Rate:       {ce_ml_conversion:.2f}%")
            print(f"PE ML Conversion Rate:       {pe_ml_conversion:.2f}%")
        except Exception as e:

            print(f"[Telemetry Error] {e}")
        return metrics

    # ── Single day simulation ─────────────────────────────────────────

    def _run_day(
        self,
        day_df:     pd.DataFrame,
        hist_df:    pd.DataFrame,
        prev_close: Optional[float],
        equity:     float,
    ) -> dict:

        date_str  = str(day_df["date"].iloc[0].date())
        day_stat  = DayStats(date=date_str)
        trades    = []

        # Reset signal engine for new day
        self.signal_engine.reset_day(prev_close)

        position:         Optional[dict] = None
        entry_time:       Optional[datetime] = None
        last_exit_time:   Optional[datetime] = None
        trades_today      = 0
        day_pnl           = 0.0
        daily_limit       = self.config["DAILY_LOSS_LIMIT"]
        max_trades        = self.config["MAX_TRADES_PER_DAY"]
        cooldown          = self.config["COOLDOWN_SECONDS"]
        lot_size          = _lot_size_for_date(day_df["date"].iloc[0], self.config)   # BANKNIFTY = 30
        qty               = lot_size * self.config["LOTS_PER_TRADE"]
        entry_on          = self.config["ENTRY_ON"]

        # Rolling window buffer: warmup history + growing day candles
        window_buf = deque(hist_df.to_dict("records"), maxlen=300)

        for i in range(len(day_df)):
            row = day_df.iloc[i]
            ts  = row["date"]
            now = ts.time()

            # Skip pre-market
            if now < _MARKET_OPEN:
                window_buf.append(row.to_dict())
                continue

            # Force exit at 15:15
            if now >= dtime(15, 15) and position is not None:
                exit_spot, exit_reason = row["close"] - 0.5, "TIME_CLOSE"
                trade = self._close_position(
                    position, exit_spot, ts, exit_reason, trades_today, day_pnl
                )
                trades.append(trade)
                day_pnl  += trade.pnl
                day_stat.pnl += trade.pnl
                day_stat.trades += 1
                day_stat.wins   += 1 if trade.pnl > 0 else 0
                day_stat.losses += 0 if trade.pnl > 0 else 1
                self.signal_engine.learner.record_trade_result(
                    position["side"], trade.pnl, position["ml_prob"],
                    position.get("features", {}), exit_reason
                )
                position   = None
                entry_time = None
                window_buf.append(row.to_dict())
                continue

            window_buf.append(row.to_dict())
            window_df = pd.DataFrame(list(window_buf))

            # ── Position management ───────────────────────────────────
            if position is not None:
                cur_spot     = row["close"]
                # Option premium LTP at this candle (side-correct, premium space)
                ltp          = _opt_sim.premium(
                    position["entry_spot"], cur_spot,
                    position["side"], _mins_to_close(ts)
                )
                held_seconds = (ts - entry_time).total_seconds()

                exit_flag, exit_reason = self.signal_engine.check_exit(
                    position, ltp, held_seconds
                )

                # Hard SL belt+suspenders (premium space, side-agnostic)
                if ltp <= position.get("stop_loss", position["entry"] * 0.90):
                    exit_flag, exit_reason = True, "STOP"

                if exit_flag:
                    # Realistic fill: exit at the close that TRIGGERED the exit
                    # (decided on this candle's close), minus slippage. Filling at
                    # this candle's OPEN would be a look-back and made STOPs show
                    # fake profits.
                    exit_spot = cur_spot - 0.5
                    trade = self._close_position(
                        position, exit_spot, ts, exit_reason,
                        trades_today, day_pnl
                    )
                    trades.append(trade)
                    day_pnl     += trade.pnl
                    day_stat.pnl    += trade.pnl
                    day_stat.trades += 1
                    day_stat.wins   += 1 if trade.pnl > 0 else 0
                    day_stat.losses += 0 if trade.pnl > 0 else 1
                    last_exit_time   = ts

                    self.signal_engine.learner.record_trade_result(
                        position["side"], trade.pnl, position["ml_prob"],
                        position.get("features", {}), exit_reason
                    )
                    position   = None
                    entry_time = None

                    # Daily loss limit
                    if day_pnl <= daily_limit:
                        day_stat.killed = True
                        break

            # ── Entry logic ───────────────────────────────────────────
            if position is None and trades_today < max_trades and day_pnl > daily_limit:

                # Cooldown gate
                if last_exit_time is not None:
                    elapsed = (ts - last_exit_time).total_seconds()
                    if elapsed < cooldown:
                        continue

                signal = self.signal_engine.step(window_df, ts)

                if signal is not None:
                    logger.info(f"[SIGNAL] {signal}")       
                    side = signal["side"]
                    features = signal.get("features", {})

                    # TREND FILTER (CORRECT PLACE)
                    ema20 = features.get("ema20")
                    ema50 = features.get("ema50")

                    # ── Trend alignment filter ──────────────────────────────
                    # Mild misalignment → soft probability penalty (keep trading).
                    # STRONG counter-trend (EMA gap > 0.15% of price) → skip the
                    # entry entirely. This is where the big CE losses on downtrend
                    # days come from. ORB breakouts are exempt (fighting EMA is
                    # their job).
                    trend_penalty = 0.0
                    counter_trend = False
                    ema_gap_pct   = abs(ema20 - ema50) / max(float(row["close"]), 1)

                    # CE against trend
                    if side == "CE" and ema20 < ema50:
                        trend_penalty = 0.04
                        counter_trend = ema_gap_pct > 0.0015

                    # PE against trend
                    elif side == "PE" and ema20 > ema50:
                        trend_penalty = 0.04
                        counter_trend = ema_gap_pct > 0.0015

                    if counter_trend and not signal.get("reason", "").startswith("ORB"):
                        # Pure-ML entry fighting a strong trend — skip it.
                        continue

                    # Soft trend adjustment only
                    signal["ml_prob"] = max(
                        signal["ml_prob"] - trend_penalty,
                        0.0
                    )
                    # Entry price: next candle open or current close
                    if entry_on == "next_open" and i < len(day_df) - 1:
                        next_row = day_df.iloc[i + 1]
                        entry_price = float(next_row["open"]) + 0.5
                    else:
                        entry_price = float(row["close"]) + 0.5

                    # Entry SPOT: next candle open or current close
                    if entry_on == "next_open" and i < len(day_df) - 1:
                        next_row = day_df.iloc[i + 1]
                        entry_spot = float(next_row["open"]) + 0.5
                    else:
                        entry_spot = float(row["close"]) + 0.5

                    # ATM strike proxy (BANKNIFTY: 100-pt strikes)
                    atm_strike = round(entry_spot / 100) * 100

                    # mins to close at entry
                    mins_to_close = _mins_to_close(ts)

                    # Entry option PREMIUM — what live reads as the fill LTP.
                    # Everything downstream (stops, profit_manager, PnL) is now
                    # in PREMIUM space, side-agnostic and aligned with live.
                    entry_premium = _opt_sim.premium(entry_spot, entry_spot, side, mins_to_close)

                    atr_val = features.get("atr", entry_spot * 0.01)
                    regime  = signal.get("regime", "UNKNOWN")
                    stop_loss, target, _stop_pct = compute_entry_stops(
                        entry_premium, atr_val, regime
                    )

                    position = {
                        "symbol":      f"BANKNIFTY_{side}_{atm_strike}",
                        "side":        side,
                        "entry":       entry_premium,
                        "entry_spot":  entry_spot,
                        "qty":         qty,
                        "lot_size":    lot_size,
                        "stop_loss":   stop_loss,
                        "target":      target,
                        "max_pnl":     0.0,
                        "ml_prob":     signal["ml_prob"],
                        "features":    features,
                        "regime":      regime,
                        "reason":      signal.get("reason", ""),
                        "atm_strike":  atm_strike,
                        "entry_mins_to_close": mins_to_close,
                        "orb_high":    self.signal_engine.orb_high,
                        "orb_low":     self.signal_engine.orb_low,
                        "_entry_ts":   ts,
                    }
                    entry_time    = ts
                    trades_today += 1
                    # ═══════════════════════════════════════════════════════
                    # TELEMETRY — EXECUTED TRADES
                    # ═══════════════════════════════════════════════════════
                    if side == "CE":
                        self.signal_engine.telemetry["ce_executed"] += 1
                    else:
                        self.signal_engine.telemetry["pe_executed"] += 1

                    logger.info(
                        f"[EXECUTED] "
                        f"side={side} "
                        f"entry_premium={entry_premium:.2f} "
                        f"ml_prob={signal['ml_prob']:.3f}"
                    )

        # ── End of day force-close ────────────────────────────────────
        if position is not None:
            exit_spot = day_df["close"].iloc[-1] - 0.5
            trade = self._close_position(
                position, exit_spot, day_df["date"].iloc[-1],
                "DAY_END", trades_today, day_pnl
            )
            trades.append(trade)
            day_pnl     += trade.pnl
            day_stat.pnl    += trade.pnl
            day_stat.trades += 1
            day_stat.wins   += 1 if trade.pnl > 0 else 0
            day_stat.losses += 0 if trade.pnl > 0 else 1
            self.signal_engine.learner.record_trade_result(
                position["side"], trade.pnl, position["ml_prob"],
                position.get("features", {}), "DAY_END"
            )

        day_stat.day_type = self.signal_engine.learner.get_day_type()
        return {"pnl": day_pnl, "trades": trades, "day_stat": day_stat}

    # ── Position closer ───────────────────────────────────────────────

    def _close_position(
        self,
        position:    dict,
        exit_spot:   float,
        exit_time:   datetime,
        exit_reason: str,
        trades_today: int,
        day_pnl:     float,
    ) -> Trade:

        self._trade_id += 1
        entry_premium = position["entry"]                 # premium at entry
        entry_spot    = position.get("entry_spot", entry_premium)
        side      = position["side"]
        qty       = position["qty"]
        lot_size  = position["lot_size"]

        # Exit option PREMIUM from the exit spot (side-correct), then PnL is the
        # premium change × qty — exactly what live realises.
        exit_mins_to_close = _mins_to_close(exit_time)
        exit_premium = _opt_sim.premium(entry_spot, exit_spot, side, exit_mins_to_close)
        pnl = round((exit_premium - entry_premium) * qty, 2)

        held_secs = (exit_time - position["_entry_ts"]).total_seconds()

        return Trade(
            trade_id      = self._trade_id,
            date          = str(exit_time.date()),
            entry_time    = position["_entry_ts"],   # actual entry_time stored in caller
            exit_time     = exit_time,
            side          = side,
            entry_price   = entry_premium,
            exit_price    = exit_premium,
            qty           = qty,
            lot_size      = lot_size,
            pnl           = pnl,
            ml_prob       = position.get("ml_prob", 0.0),
            entry_reason  = position.get("reason", ""),
            exit_reason   = exit_reason,
            regime        = position.get("regime", "UNKNOWN"),
            orb_high      = position.get("orb_high"),
            orb_low       = position.get("orb_low"),
            held_candles  = int(held_secs // 60),
            held_seconds  = held_secs,
            max_pnl       = position.get("max_pnl", 0.0),
            stop_loss     = position.get("stop_loss", entry_premium * 0.90),
            target        = position.get("target", entry_premium * 1.05),
            features      = position.get("features", {}),
        )

    # ══════════════════════════════════════════════════════════════════
    # METRICS
    # ══════════════════════════════════════════════════════════════════

    def _compute_metrics(
        self,
        total_pnl:    float,
        final_equity: float,
        max_drawdown: float,
    ) -> dict:

        trades = self.trades
        n = len(trades)

        if n == 0:
            return {"error": "No trades generated", "total_trades": 0}

        pnls     = [t.pnl for t in trades]
        wins     = [p for p in pnls if p > 0]
        losses   = [p for p in pnls if p <= 0]

        win_rate      = len(wins) / n
        avg_win       = np.mean(wins)   if wins   else 0
        avg_loss      = np.mean(losses) if losses else 0
        avg_pnl       = np.mean(pnls)
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf")

        # Sharpe (daily PnL series)
        daily_pnl = defaultdict(float)
        for t in trades:
            daily_pnl[t.date] += t.pnl
        dp_series = np.array(list(daily_pnl.values()))
        sharpe    = (np.mean(dp_series) / np.std(dp_series) * np.sqrt(252)
                     if np.std(dp_series) > 0 else 0)

        # By strategy
        by_reason: dict = defaultdict(lambda: {"n": 0, "pnl": 0, "wins": 0})
        for t in trades:
            key = t.entry_reason
            by_reason[key]["n"]    += 1
            by_reason[key]["pnl"]  += t.pnl
            by_reason[key]["wins"] += 1 if t.pnl > 0 else 0

        # ML probability bucket accuracy
        buckets = {
            "0.40-0.50": {"n": 0, "wins": 0},
            "0.50-0.60": {"n": 0, "wins": 0},
            "0.60-0.70": {"n": 0, "wins": 0},
            "0.70+":     {"n": 0, "wins": 0},
        }
        for t in trades:
            p = t.ml_prob
            b = ("0.70+" if p >= 0.70 else
                 "0.60-0.70" if p >= 0.60 else
                 "0.50-0.60" if p >= 0.50 else
                 "0.40-0.50")
            buckets[b]["n"]    += 1
            buckets[b]["wins"] += 1 if t.pnl > 0 else 0

        for b in buckets:
            nb = buckets[b]["n"]
            buckets[b]["win_rate"] = round(buckets[b]["wins"] / nb, 3) if nb else 0

        # Exit reason breakdown
        by_exit: dict = defaultdict(lambda: {"n": 0, "pnl": 0})
        for t in trades:
            by_exit[t.exit_reason]["n"]   += 1
            by_exit[t.exit_reason]["pnl"] += t.pnl

        return {
            "total_trades":   n,
            "win_rate":       round(win_rate, 3),
            "total_pnl":      round(total_pnl, 2),
            "avg_pnl":        round(avg_pnl, 2),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "profit_factor":  round(profit_factor, 3),
            "max_drawdown":   round(max_drawdown, 2),
            "sharpe":         round(sharpe, 3),
            "final_equity":   round(final_equity, 2),
            "by_strategy":    dict(by_reason),
            "by_exit_reason": dict(by_exit),
            "ml_bucket_accuracy": buckets,
            "trading_days":   len(self.day_stats),
            "days_killed":    sum(1 for d in self.day_stats if d.killed),
        }

    # ══════════════════════════════════════════════════════════════════
    # EXPORT
    # ══════════════════════════════════════════════════════════════════

    def get_trade_log(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        rows = []
        for t in self.trades:
            rows.append({
                "trade_id":     t.trade_id,
                "date":         t.date,
                "exit_time":    t.exit_time,
                "side":         t.side,
                "entry_price":  t.entry_price,
                "exit_price":   t.exit_price,
                "qty":          t.qty,
                "pnl":          t.pnl,
                "ml_prob":      t.ml_prob,
                "entry_reason": t.entry_reason,
                "exit_reason":  t.exit_reason,
                "regime":       t.regime,
                "orb_high":     t.orb_high,
                "orb_low":      t.orb_low,
                "held_seconds": t.held_seconds,
                "max_pnl":      t.max_pnl,
                "stop_loss":    t.stop_loss,
                "target":       t.target,
            })
        return pd.DataFrame(rows)

    def get_day_log(self) -> pd.DataFrame:
        if not self.day_stats:
            return pd.DataFrame()
        return pd.DataFrame([vars(d) for d in self.day_stats])

    def save_results(self, output_dir: str = "backtest/results"):
        os.makedirs(output_dir, exist_ok=True)

        trade_log = self.get_trade_log()
        day_log   = self.get_day_log()

        trade_path = os.path.join(output_dir, "trade_log.csv")
        day_path   = os.path.join(output_dir, "day_log.csv")

        trade_log.to_csv(trade_path, index=False)
        day_log.to_csv(day_path,     index=False)

        logger.info(f"[RESULTS] Trade log → {trade_path}")
        logger.info(f"[RESULTS] Day log   → {day_path}")

        return trade_path, day_path
if __name__ == "__main__":

    print("Starting Backtest...")

    engine = BacktestEngine()

    df = engine.load_data(r"D:\All Bots\trading_system\data\historical\banknifty_1m_full.csv")
    df = df.tail(200000)
    print("Data loaded:", df.shape)

    results = engine.run(df)

    print("\n===== BACKTEST RESULTS =====")
    for k, v in results.items():
        print(f"{k}: {v}")

    engine.save_results()