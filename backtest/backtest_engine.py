# backtest/backtest_engine.py
# Institutional-grade backtesting engine
# Reuses: LiveEngine, feature_config, profit_manager, risk_manager
# NO logic duplication — calls the same functions as live system
#
# ══════════════════════════════════════════════════════════════════════
# 2026-09-06 — MADE REPRESENTATIVE OF LIVE
#
# Before this change the harness measured a system that has not been live
# for months, so its output could not validate any live setting:
#
#   1. NO PREDICT-FIRST PATH. step() implemented only the legacy hard
#      direction gate (`if direction_bias == 0: return None`, then
#      `if direction_bias != 1: ce_adj = 0.0`). Live runs PREDICT_FIRST=1:
#      side = argmax(ce_adj, pe_adj), then edge-margin, threshold, 5m
#      SuperTrend AGREEMENT and VWAP agreement as confirmations. Those are
#      different populations of trades — the legacy gate can never take the
#      counter-bias side that live's argmax will happily take.
#   2. CE WAS GATED TO THE ORB BREAKOUT CANDLE ONLY (`if not ce_break:
#      ce_adj = 0.0`). Live has no such gate, so ~all CE trades live takes
#      were invisible here.
#   3. NIFTY-ERA SIZING. NIFTY_LOT_SIZE=65 / _lot_size_for_date() 65-or-75
#      against the configured BANKNIFTY LOT_SIZE=30 — every PnL was ~2.2x.
#   4. UNCONDITIONAL LUNCH FILTER (11:00-12:30) while live runs
#      LUNCH_FILTER_ENABLED=0, removing 90 tradable minutes/day.
#   5. MAX_TRADES_PER_DAY=10 vs the configured 4.
#   6. NO TRANSACTION COST. PnL was gross premium change × qty. At
#      Rs66/lot round-trip that is Rs66 of pure fiction per trade, which is
#      larger than the median trade — so a "profitable" run meant nothing.
#      Costs are now deducted in _close_position (CLAUDE.md cost model).
#   7. ATM proxy rounded to a 50-pt grid (NIFTY) on a 100-pt instrument.
#
# Set BT_PREDICT_FIRST=0 to get the old legacy-gate behaviour back.
# ══════════════════════════════════════════════════════════════════════

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

from ml.feature_config import _safe_build_live_features, FEATURE_COLUMNS, ALL_FEATURE_COLUMNS
from ml.predictor_champion import ChampionPredictor
from ml.ml_intraday_learner import IntradayMLLearner
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx, VWAPAccumulator
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops, premium_stop_pts, _TARGET_R
from engine.config.config import Config as _LiveConfig, STRIKE_STEP as _STRIKE_STEP

logger = logging.getLogger("backtest")

# ── Market constants ──────────────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)
_DAY_CLASS_AT = dtime(9, 45)
_MARKET_CLOSE = dtime(15, 30)
_NO_ENTRY_AFTER = dtime(15, 15)

# Institutional session filter — avoid lunch-hour chop (11:00–12:30).
# Live runs with LUNCH_FILTER_ENABLED=0, so this harness follows the same
# switch instead of applying the window unconditionally.
_LUNCH_START    = dtime(11,  0)
_LUNCH_END      = dtime(12, 30)
_LUNCH_ENABLED  = (os.getenv("LUNCH_FILTER_ENABLED", "0") == "1")

# Minimum expected PnL for any new trade (₹150 safeguard) — same as live.
_MIN_EXPECTED_PNL = float(os.getenv("MIN_EXPECTED_PNL", "150.0"))

# Live gate parameters (engine/live_engine.py) mirrored here.
_PREDICT_FIRST   = (os.getenv("BT_PREDICT_FIRST", "1") == "1")

# STRUCTURE-FIRST (Sep-2026): structure (5m ST + VWAP) picks the side, ML is
# only an ACTIVITY filter (P(touch) = ce+pe).  Mirrors LiveEngine._structure_first.
_STRUCTURE_FIRST  = (os.getenv("BT_STRUCTURE_FIRST", "0") == "1")
_ACTIVITY_THR     = float(os.getenv("BT_STRUCTURE_ACTIVITY_THRESHOLD", "0.55"))
_ML_EDGE_MARGIN  = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
_VWAP_TOL        = 0.0015

# ML floor for the LEGACY path only. The predict-first path gates purely on
# CHAMPION_THRESHOLD, exactly like live.
_MIN_ML_FLOOR = 0.40

# Position sizing — BANKNIFTY. The old NIFTY_LOT_SIZE=65 / _lot_size_for_date()
# 65-or-75 inflated every PnL by ~2.2x against the configured LOT_SIZE=30.
# NOTE: Config sets its attributes in __init__, so the value must be read off
# an INSTANCE — a class-level getattr silently returns the default.
_CFG = _LiveConfig()
BANKNIFTY_LOT_SIZE = int(getattr(_CFG, "LOT_SIZE", 30) or 30)
NIFTY_LOT_SIZE = BANKNIFTY_LOT_SIZE      # legacy alias — kept for old callers
OPTIONS_PREMIUM_PROXY = True  # simulate option price as % of spot

# Round-trip transaction cost. CLAUDE.md: Rs66 per lot (qty 65 -> Rs66,
# qty 130 -> Rs132). Deducted from every closed trade.
_COST_PER_LOT = float(getattr(_CFG, "COST_PER_LOT", 66.0) or 66.0)


def _round_trip_cost(qty: int, lot_units: int) -> float:
    """Rs cost for opening AND closing `qty` units (scales with lots)."""
    lots = max(1, round(qty / max(int(lot_units), 1)))
    return lots * _COST_PER_LOT


def _lot_size_for_date(d) -> int:
    """
    Lot size for the traded instrument.

    Was NIFTY-specific (75 pre-2026, 65 from Jan 2026). The live system trades
    BANKNIFTY at Config.LOT_SIZE, so the backtest must size identically or its
    rupee PnL, the profit ladder rungs, and the cost model are all wrong.
    """
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
    gross_pnl:      float = 0.0   # premium change × qty, BEFORE costs
    cost:           float = 0.0   # round-trip cost (Rs66/lot, CLAUDE.md)
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

class OptionPriceSimulator:
    """
    Approximates CE/PE option price from NIFTY spot using a simple
    intrinsic + time-value model.  Good enough for directional PnL.

    In live trading, actual LTP is used. Here we simulate it.
    """

    def __init__(self, base_premium: float = 150.0, atm_vol: float = 0.12):
        self.base_premium = base_premium
        self.atm_vol      = atm_vol
        self.atm_delta    = 0.5   # long ATM option ≈ 0.5 delta BOTH directions

    def price(
        self,
        spot:       float,
        atm_strike: float,
        side:       str,
        mins_to_close: float,
    ) -> float:
        """
        Simplified Black-Scholes-inspired option price.
        Returns approximate ATM option premium.
        """
        T = max(mins_to_close / (375 * 252), 1e-6)   # time in years
        moneyness = (spot - atm_strike) / atm_strike

        intrinsic = max(0, moneyness * spot) if side == "CE" else max(0, -moneyness * spot)
        time_val  = self.base_premium * (T ** 0.5) * self.atm_vol * 100

        return round(max(intrinsic + time_val, 5.0), 2)

    def premium(
        self,
        entry_spot:    float,
        cur_spot:      float,
        side:          str,
        mins_to_close: float,
    ) -> float:
        """
        Directional option premium for a LONG CE/PE, relative to entry spot.

        premium = time_value + delta * favorable_move
          - CE favorable when spot RISES, PE favorable when spot FALLS.
          - time_value decays as mins_to_close shrinks (theta).
        This is the SAME quantity live reads as the option LTP, so backtest
        stops / profit_manager / PnL all operate in premium space exactly
        like live — and it is side-correct (PE profits on down-moves).
        """
        T = max(mins_to_close / (375 * 252), 1e-6)
        time_val  = self.base_premium * (T ** 0.5) * self.atm_vol * 100
        favorable = (cur_spot - entry_spot) if side == "CE" else (entry_spot - cur_spot)
        return round(max(time_val + self.atm_delta * favorable, 1.0), 2)

    def pnl(
        self,
        entry_spot:  float,
        exit_spot:   float,
        atm_strike:  float,
        side:        str,
        qty:         int,
        entry_mins_to_close: float,
        exit_mins_to_close:  float,
    ) -> float:
        # Premium-space PnL: side-correct (PE gains on down-moves) and
        # symmetric (adverse and favorable moves weighted equally).
        ep = self.premium(entry_spot, entry_spot, side, entry_mins_to_close)
        xp = self.premium(entry_spot, exit_spot,  side, exit_mins_to_close)
        return round((xp - ep) * qty, 2)


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

        # Higher-timeframe (5m) SuperTrend direction — live's confirmation gate.
        self._htf5_dir: int = 0

        # Last adjusted ML probabilities (predict-first inputs).
        self._last_ce_adj: float = 0.0
        self._last_pe_adj: float = 0.0

        # Day classifier (optional)
        self._day_clf          = None
        self._day_classified   = False
        self._day_candles_30m  = []
        self._prev_close: Optional[float] = None
        self._open_price_set   = False

        # Day-context cache for NEW-30 features (reset per day)
        self._day_ctx: dict = {
            "date":       None,
            "day_open":   0.0,
            "day_high":   0.0,
            "day_low":    0.0,
            "prev_close": 0.0,
        }

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

            # Predict-first blocker attribution (mirrors live BLOCKER TRACE)
            "pf_evaluated":     0,
            "pf_no_edge":       0,
            "pf_below_thr":     0,
            "pf_htf5_opposes":  0,
            "pf_vwap_fail":     0,
            "pf_side_blocked":  0,
            "pf_pnl_guard":     0,
            "pf_passed":        0,
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
        self._day_ctx        = {
            "date":       None,
            "day_open":   0.0,
            "day_high":   0.0,
            "day_low":    0.0,
            "prev_close": prev_close or 0.0,
        }
        self._vwap.reset()
        self._direction_bias = 0
        self._htf5_dir       = 0
        self._last_ce_adj    = 0.0
        self._last_pe_adj    = 0.0
        self.learner.reset_day()

    # ── Higher-timeframe confirmation (mirrors LiveEngine._htf_supertrend_dir) ──

    def _htf_supertrend_dir(self, window: pd.DataFrame, tf: int = 5) -> int:
        """
        Resample the 1m window to tf-minute candles and return the last
        SuperTrend(10,3) direction (+1 up / -1 down / 0 = insufficient data).
        Byte-for-byte the same construction live uses, so the confirmation gate
        blocks the same candles here as it does in production.
        """
        if window is None or len(window) < tf * 12:
            return 0
        h = window["high"].values.astype(float)
        l = window["low"].values.astype(float)
        c = window["close"].values.astype(float)
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
        day_ctx = self._update_day_ctx(window, ts)
        feats  = _safe_build_live_features(closes, opens, highs, lows, volumes,
                                           signal, ts=ts, day_ctx=day_ctx)

        missing = [f for f in ALL_FEATURE_COLUMNS if f not in feats]
        if missing:
            return None

        return feats

    def _update_day_ctx(self, window: pd.DataFrame, ts: datetime) -> dict:
        """Maintain day_open/high/low/prev_close for NEW-30 features."""
        ctx = self._day_ctx
        today = ts.date()
        if ctx["date"] != today:
            ctx["date"] = today
            ctx["day_open"]   = 0.0
            ctx["day_high"]   = 0.0
            ctx["day_low"]    = 0.0
            ctx["prev_close"] = self._prev_close or 0.0
            if window is not None and len(window):
                try:
                    date_col = next(
                        (c for c in ("ts", "date", "datetime", "timestamp")
                         if c in window.columns), None
                    )
                    if date_col:
                        dts = pd.to_datetime(window[date_col])
                        today_rows = window[dts.dt.date == today]
                        if len(today_rows):
                            ctx["day_open"] = float(today_rows["open"].iloc[0])
                            ctx["day_high"] = float(today_rows["high"].max())
                            ctx["day_low"]  = float(today_rows["low"].min())
                except Exception:
                    pass
        else:
            if window is not None and len(window):
                last = window.iloc[-1]
                try:
                    ctx["day_high"] = max(ctx["day_high"], float(last["high"]))
                    ctx["day_low"]  = min(ctx["day_low"] if ctx["day_low"] else float(last["low"]),
                                           float(last["low"]))
                except Exception:
                    pass
        return ctx

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

    def _live_threshold(self) -> float:
        """
        Same contract as LiveEngine._live_threshold(): under predict-first the
        gate is CHAMPION_THRESHOLD; under the legacy path it is the adaptive
        learner value floored by _MIN_ML_FLOOR.
        """
        if not _PREDICT_FIRST:
            try:
                return float(max(self.learner.get_ml_threshold(), _MIN_ML_FLOOR))
            except Exception:
                return _MIN_ML_FLOOR
        return float(self.config.get("CHAMPION_THRESHOLD", 0.55))

    def _finalize_signal(self, signal: dict, features: dict, price: float) -> Optional[dict]:
        """
        Mirror of LiveEngine._finalize_signal: attach the PREMIUM-space stop
        distance the risk manager will place at the fill, then apply the
        expected-PnL guard net of round-trip cost.

        The legacy backtest computed the guard as (target - price) * lot_size
        with SPOT levels — spot points priced as premium rupees, so the guard
        was ~50x too permissive.
        """
        atr_val  = features.get("atr", price * 0.01)
        day_type = self.learner.get_day_type()
        regime   = (
            "EXPANSION" if "VOLATILE" in day_type else
            "TREND"     if "TREND"    in day_type else
            "RANGE"
        )

        stop_pts   = premium_stop_pts(atr_val)
        target_pts = stop_pts * _TARGET_R
        signal.update(stop_pts=round(stop_pts, 2),
                      target_pts=round(target_pts, 2),
                      regime=regime)

        lot_size = int(self.config.get("LOT_SIZE", BANKNIFTY_LOT_SIZE))
        cost_rs  = float(self.config.get("COST_PER_LOT", _COST_PER_LOT))
        expected_pnl = (signal["ml_prob"] * target_pts * lot_size
                        - (1 - signal["ml_prob"]) * stop_pts * lot_size
                        - cost_rs)
        if expected_pnl < _MIN_EXPECTED_PNL:
            self.telemetry["pf_pnl_guard"] += 1
            return None
        return signal

    def _step_structure_first(self, features: dict, price: float,
                              ts: datetime) -> Optional[dict]:
        """
        Mirror of LiveEngine._check_entry_structure_first (Sep-2026 fix):
        structure picks the side, ML gates on activity only.  See live
        implementation for the full rationale (direction is a coin flip;
        activity is the learnable signal).
        """
        self.telemetry["pf_evaluated"] += 1

        ce_prob = self.predictor.predict(features, "CE")
        pe_prob = self.predictor.predict(features, "PE")
        if ce_prob is None or pe_prob is None:
            return None
        ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(ce_prob, pe_prob, "CE")
        self._last_ce_adj, self._last_pe_adj = float(ce_adj), float(pe_adj)

        pvwap = features.get("price_vs_vwap", 0.0)
        htf5  = self._htf5_dir
        activity = float(ce_prob or 0.0) + float(pe_prob or 0.0)

        blockers = []
        side = None
        if htf5 == 1 and pvwap >= -_VWAP_TOL:
            side = "CE"
        elif htf5 == -1 and pvwap <= _VWAP_TOL:
            side = "PE"
        else:
            blockers.append("NO_STRUCT_DIR")
        if activity < _ACTIVITY_THR:
            blockers.append("LOW_ACTIVITY")
            self.telemetry["pf_below_thr"] += 1
        if side:
            blocked, _reason = self.learner.is_side_blocked(side)
            if blocked:
                blockers.append("SIDE_BLOCKED")
                self.telemetry["pf_side_blocked"] += 1
        if blockers:
            return None

        self.telemetry["pf_passed"] += 1
        if side == "CE":
            self.telemetry["ce_ml_pass"] += 1
        else:
            self.telemetry["pe_ml_pass"] += 1
        signal = {"side": side, "ml_prob": activity, "features": features,
                  "reason": f"STRUCT_{side}"}
        return self._finalize_signal(signal, features, price)

    def _step_predict_first(self, features: dict, price: float,
                            ts: datetime) -> Optional[dict]:
        """
        Byte-for-byte mirror of LiveEngine._check_entry_predict_first:
          1. PREDICT   — side = argmax(ce_adj, pe_adj)
          2. EDGE      — |ce_adj - pe_adj| >= ML_EDGE_MARGIN
          3. THRESHOLD — chosen side clears CHAMPION_THRESHOLD (clamp 0.65)
          4. CONFIRM   — 5m SuperTrend must not OPPOSE; VWAP side must agree
          5. learner side-block, then _finalize_signal
        """
        self.telemetry["pf_evaluated"] += 1

        ce_prob = self.predictor.predict(features, "CE")
        pe_prob = self.predictor.predict(features, "PE")
        if ce_prob is None or pe_prob is None:
            return None

        ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(ce_prob, pe_prob, "CE")
        self._last_ce_adj, self._last_pe_adj = float(ce_adj), float(pe_adj)

        base_thr = self._live_threshold()
        thr      = min(base_thr, 0.65)   # live clamps > 0.65

        side  = "CE" if ce_adj >= pe_adj else "PE"
        prob  = ce_adj if side == "CE" else pe_adj
        pvwap = features.get("price_vs_vwap", 0.0)
        htf5  = self._htf5_dir

        blockers = []
        if abs(ce_adj - pe_adj) < _ML_EDGE_MARGIN:
            blockers.append("NO_EDGE")
            self.telemetry["pf_no_edge"] += 1
        if prob < thr:
            blockers.append("ML_BELOW_THR")
            self.telemetry["pf_below_thr"] += 1
        if (side == "CE" and htf5 == -1) or (side == "PE" and htf5 == 1):
            blockers.append("HTF5_OPPOSES")
            self.telemetry["pf_htf5_opposes"] += 1
        if (side == "CE" and pvwap < -_VWAP_TOL) or (side == "PE" and pvwap > _VWAP_TOL):
            blockers.append("VWAP_FAIL")
            self.telemetry["pf_vwap_fail"] += 1
        blocked, _reason = self.learner.is_side_blocked(side)
        if blocked:
            blockers.append("SIDE_BLOCKED")
            self.telemetry["pf_side_blocked"] += 1

        if side == "CE":
            self.telemetry["ce_raw_signals"] += 1
        else:
            self.telemetry["pe_raw_signals"] += 1

        if blockers:
            return None

        self.telemetry["pf_passed"] += 1
        if side == "CE":
            self.telemetry["ce_ml_pass"] += 1
        else:
            self.telemetry["pe_ml_pass"] += 1

        signal = {"side": side, "ml_prob": prob, "features": features,
                  "reason": f"ML_{side}"}
        return self._finalize_signal(signal, features, price)

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
        # SESSION FILTER — no new entries during lunch chop.
        # Live runs LUNCH_FILTER_ENABLED=0, so this is now switched off by
        # default here too. Applying it unconditionally removed 90 tradable
        # minutes/day that live actually trades.
        # ─────────────────────────────────────────────────────
        if _LUNCH_ENABLED and _LUNCH_START <= now < _LUNCH_END:
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

        # 5m SuperTrend — live's anti-noise confirmation. Computed every
        # candle so both paths can use it.
        try:
            self._htf5_dir = self._htf_supertrend_dir(window, tf=5)
        except Exception:
            self._htf5_dir = 0

        # ═════════════════════════════════════════════════════
        # PREDICT-FIRST PATH — what live actually runs
        # ═════════════════════════════════════════════════════
        if _STRUCTURE_FIRST:
            sig = self._step_structure_first(features, price, ts)
            if sig is not None:
                self.telemetry["signals_returned"] += 1
            else:
                self.telemetry["signals_none"] += 1
            return sig
        elif _PREDICT_FIRST:
            sig = self._step_predict_first(features, price, ts)
            if sig is not None:
                self.telemetry["signals_returned"] += 1
            else:
                self.telemetry["signals_none"] += 1
            return sig

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
            lot_sz  = self.config.get("LOT_SIZE", NIFTY_LOT_SIZE)
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
        # SINGLE position-size source of truth — same as LiveEngine.check_exit:
        # the ACTUAL filled qty drives max_pnl/MFE, and lot_units (the index lot
        # size) drives the cost model inside the ladder.
        qty       = position.get("qty", position.get("lot_size", BANKNIFTY_LOT_SIZE))
        lot_units = position.get("lot_size", BANKNIFTY_LOT_SIZE)

        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=position["entry"],
            ltp=ltp,
            lot_size=qty,
            lot_units=lot_units,
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

    def __init__(self, config: Optional[dict] = None, verbose: bool = True):
        self.config = config or self._default_config()
        self.verbose = verbose

        self.signal_engine = BacktestSignalEngine(self.config)

        # Results
        self.trades:    list[Trade]    = []
        self.day_stats: list[DayStats] = []
        self._trade_id  = 0

        logger.info("[BacktestEngine] Initialized")

    # ── Default config ────────────────────────────────────────────────

    @staticmethod
    def _default_config() -> dict:
        """
        Defaults now MIRROR the live Config instance (engine/config/config.py)
        instead of the NIFTY-era hardcodes. Every value that differed made the
        backtest measure a system nobody runs:
            MAX_TRADES_PER_DAY  10 -> Config (4)
            LOT_SIZE            65 -> Config (30)
            CHAMPION_THRESHOLD 0.42 -> Config / env
            COOLDOWN_SECONDS   180 -> Config.REENTRY_COOLDOWN (300)
        """
        return {
            "INITIAL_CAPITAL":    float(os.getenv("BT_CAPITAL", "100000")),
            "DAILY_LOSS_LIMIT":   float(getattr(_CFG, "DAILY_LOSS_LIMIT", -2000)),
            "MAX_TRADES_PER_DAY": int(getattr(_CFG, "MAX_TRADES_PER_DAY", 4)),
            "MAX_HOLD_SECONDS":   int(getattr(_CFG, "MAX_HOLD_SECONDS", 300)),
            "COOLDOWN_SECONDS":   int(getattr(_CFG, "REENTRY_COOLDOWN", 300)),
            "LOT_SIZE":           BANKNIFTY_LOT_SIZE,
            "COST_PER_LOT":       _COST_PER_LOT,
            "LOTS_PER_TRADE":     int(os.getenv("BT_LOTS_PER_TRADE", "1")),
            "CHAMPION_THRESHOLD": float(getattr(_CFG, "CHAMPION_THRESHOLD", 0.55)),
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
        if not self.verbose:
            metrics["telemetry"] = dict(self.signal_engine.telemetry)
            return metrics
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

        if _PREDICT_FIRST:
            print("\n===== PREDICT-FIRST BLOCKERS =====")
            print(f"Evaluated:                   {t['pf_evaluated']}")
            print(f"  NO_EDGE:                   {t['pf_no_edge']}")
            print(f"  ML_BELOW_THR:              {t['pf_below_thr']}")
            print(f"  HTF5_OPPOSES:              {t['pf_htf5_opposes']}")
            print(f"  VWAP_FAIL:                 {t['pf_vwap_fail']}")
            print(f"  SIDE_BLOCKED:              {t['pf_side_blocked']}")
            print(f"  PNL_GUARD:                 {t['pf_pnl_guard']}")
            print(f"Passed all gates:            {t['pf_passed']}")

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
        metrics["telemetry"] = dict(self.signal_engine.telemetry)
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
        lot_size          = _lot_size_for_date(day_df["date"].iloc[0])   # 75 pre-2026, 65 from Jan 2026
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

                    # ── Trend alignment filter (LEGACY PATH ONLY) ───────────
                    # Live's predict-first gate has NO EMA counter-trend filter —
                    # its confirmations are the 5m SuperTrend and VWAP side,
                    # already applied in _step_predict_first. Running this extra
                    # filter under predict-first would silently remove trades
                    # live takes, which is exactly the class of divergence this
                    # harness is being fixed to eliminate.
                    if not _PREDICT_FIRST:
                        ema20 = features.get("ema20")
                        ema50 = features.get("ema50")
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
                        signal["ml_prob"] = max(signal["ml_prob"] - trend_penalty, 0.0)

                    # Entry SPOT: next candle open or current close
                    if entry_on == "next_open" and i < len(day_df) - 1:
                        next_row = day_df.iloc[i + 1]
                        entry_spot = float(next_row["open"]) + 0.5
                    else:
                        entry_spot = float(row["close"]) + 0.5

                    # ATM strike proxy — real instrument grid (BANKNIFTY = 100).
                    # Was round(spot/50)*50, the NIFTY grid, which produced
                    # phantom strikes like 57450 on a 100-pt instrument.
                    atm_strike = round(entry_spot / _STRIKE_STEP) * _STRIKE_STEP

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
        gross_pnl = round((exit_premium - entry_premium) * qty, 2)

        # ── Round-trip transaction cost ───────────────────────────────────
        # The old harness reported GROSS premium change as PnL. At Rs66/lot the
        # omission is larger than the median trade, so a "profitable" backtest
        # could still be a losing strategy after brokerage+taxes.
        cost = _round_trip_cost(qty, lot_size)
        pnl  = round(gross_pnl - cost, 2)

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
            gross_pnl     = gross_pnl,
            cost          = cost,
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
            "gross_pnl":      round(sum(t.gross_pnl for t in trades), 2),
            "total_cost":     round(sum(t.cost for t in trades), 2),
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
                "gross_pnl":    t.gross_pnl,
                "cost":         t.cost,
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

    import argparse

    ap = argparse.ArgumentParser(description="BANKNIFTY predict-first backtest")
    ap.add_argument("--csv", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "historical", "banknifty_1m_full.csv"))
    ap.add_argument("--thr", type=float, default=None,
                    help="CHAMPION_THRESHOLD override for this run")
    ap.add_argument("--tail", type=int, default=0, help="use only last N rows")
    args = ap.parse_args()

    print("Starting Backtest...")

    cfg = BacktestEngine._default_config()
    if args.thr is not None:
        cfg["CHAMPION_THRESHOLD"] = args.thr

    engine = BacktestEngine(cfg)

    # Default data set is BANKNIFTY (the traded instrument). The old default
    # pointed at nifty_1m_full.csv.
    df = engine.load_data(args.csv)
    if args.tail:
        df = df.tail(args.tail)
    print("Data loaded:", df.shape)
    print(f"Config: predict_first={_PREDICT_FIRST} thr={cfg['CHAMPION_THRESHOLD']} "
          f"lot={cfg['LOT_SIZE']} max_trades={cfg['MAX_TRADES_PER_DAY']} "
          f"cooldown={cfg['COOLDOWN_SECONDS']}s cost=Rs{cfg['COST_PER_LOT']}/lot "
          f"lunch_filter={_LUNCH_ENABLED}")

    results = engine.run(df)

    print("\n===== BACKTEST RESULTS =====")
    for k, v in results.items():
        if k == "telemetry":
            continue
        print(f"{k}: {v}")

    engine.save_results()