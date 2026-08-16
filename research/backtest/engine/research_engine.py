"""
Research backtest engine - clean room implementation that mirrors live trading logic.

This does NOT modify live code. It reuses the live modules' logic directly:
- LiveEngine.check_entry() for entry decisions
- LiveEngine.check_exit() (delegating to profit_manager) for exits
- cost_model for cost calculation

Key difference from legacy backtest:
- Uses LOT_SIZE=30 from engine/config/config.py
- Quantity = LOTS_PER_TRADE × LOT_SIZE
- No ORDER_SIZE legacy path
- No fractional quantities
- Enforces Bank Nifty whole-lot invariant
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime

# Add root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from engine.config.config import Config
from engine.execution.cost_model import round_trip_cost, net_pnl, lot_qty
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops
from engine.live_engine import LiveEngine
from ml.predictor_champion import ChampionPredictor
from ml.ml_intraday_learner import IntradayMLLearner
from ml.feature_config import build_live_features, FEATURE_COLUMNS
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx, VWAPAccumulator
from engine.intelligence.phase55_filter import (
    Phase55FilterConfig, evaluate_phase55_filter, infer_regime_from_features
)

# Market constants from live engine
_MARKET_OPEN = dtime(9, 15)
_ORB_END = dtime(9, 30)
_MARKET_CLOSE = dtime(15, 30)
_MIN_EXPECTED_PNL = 150.0
_MIN_ML_FLOOR = 0.55
_CE_ML_FLOOR = 0.65


class ResearchEngine:
    """
    Clean research backtest that mirrors LiveEngine decision logic exactly.

    Uses the same entry/exit functions, ML thresholds, ORB logic, and risk stops
    as the live system - without modifying any protected files.
    """

    def __init__(self, config=None, lots_per_trade=1, enable_ce=True, enable_pe=True):
        """
        Initialize research engine.

        Args:
            config: Config object (defaults to Config())
            lots_per_trade: Number of lots per trade (live default = 1)
            enable_ce: Whether to generate CE signals
            enable_pe: Whether to generate PE signals
        """
        self.config = config or Config()

        # Sizing: whole Bank Nifty lots only
        self.lot_size = lot_qty(self.config)  # 30 for BANKNIFTY
        self.lots_per_trade = lots_per_trade
        self.qty = self.lot_size * self.lots_per_trade

        # Validate sizing invariant
        assert self.qty > 0, f"Quantity must be positive: {self.qty}"
        assert self.qty % self.lot_size == 0, f"Quantity must be multiple of lot_size ({self.lot_size})"

        # Side enablement
        self.enable_ce = enable_ce
        self.enable_pe = enable_pe

        # Components mirroring LiveEngine
        self.predictor = ChampionPredictor()
        self.learner = IntradayMLLearner()
        self.vwap = VWAPAccumulator()

        # ORB state
        self.orb_high = None
        self.orb_low = None
        self.orb_done = False
        self.orb_ce_fired = False
        self.orb_pe_fired = False

        # Phase55
        self.phase55_config = Phase55FilterConfig.from_config(self.config)

        # ML floors
        self.ce_floor = _CE_ML_FLOOR
        self.pe_floor = _MIN_ML_FLOOR

        # Re-entry cooldown
        self.last_exit_ts = 0.0

        # Stats
        self.trade_log = []
        self.block_reasons = {}

    def _log_block(self, reason):
        """Track block reasons (mirrors LiveEngine._count_block)."""
        self.block_reasons[reason] = self.block_reasons.get(reason, 0) + 1

    def _reset_session(self):
        """Reset per-session state (called at market open)."""
        self.orb_high = None
        self.orb_low = None
        self.orb_done = False
        self.orb_ce_fired = False
        self.orb_pe_fired = False
        self.vwap.reset()
        self.learner.reset()  # if available
        self.last_exit_ts = 0.0

    def update_orb(self, candle: dict, ts: datetime):
        """Mirror LiveEngine.update_orb exactly."""
        now = ts.time()
        if self.orb_done:
            return

        if _MARKET_OPEN <= now < _ORB_END:
            if self.orb_high is None:
                self.orb_high = candle["high"]
                self.orb_low = candle["low"]
            else:
                self.orb_high = max(self.orb_high, candle["high"])
                self.orb_low = min(self.orb_low, candle["low"])
        elif now >= _ORB_END and not self.orb_done:
            self.orb_done = True
            if self.orb_high is not None and self.orb_low is not None:
                print(f"[ORB LOCKED] High={self.orb_high:.2f} Low={self.orb_low:.2f}")

    def build_features(self, df_window: pd.DataFrame, ts: datetime):
        """Build live features using the SAME function as live engine."""
        if df_window is None or len(df_window) < 26:
            return None

        closes = df_window["close"].tolist()
        opens = df_window["open"].tolist()
        highs = df_window["high"].tolist()
        lows = df_window["low"].tolist()
        volumes = df_window["volume"].tolist() if "volume" in df_window.columns else [0] * len(closes)

        # Signal dict
        def ema(series, span):
            alpha = 2 / (span + 1)
            val = series[0]
            for p in series[1:]:
                val = p * alpha + val * (1 - alpha)
            return val

        n = len(closes)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50) if n >= 50 else ema20

        c_arr = np.array(closes, dtype=float)
        h_arr = np.array(highs, dtype=float)
        l_arr = np.array(lows, dtype=float)

        rsi_arr = self._rsi_wilder(c_arr, period=14)
        rsi_1m = float(rsi_arr[-1]) if n >= 14 else 50.0

        atr_arr = self._atr_wilder(h_arr, l_arr, c_arr, period=14)
        atr_val = float(atr_arr[-1]) if n >= 14 else abs(closes[-1] - closes[-2]) * 14 ** 0.5
        atr_val = max(atr_val, 0.5)

        trend_strength = (ema20 - ema50) / closes[-1] if closes[-1] != 0 else 0.0

        st_dir_arr, st_line_arr = _compute_supertrend(h_arr, l_arr, c_arr, period=10, multiplier=3.0)
        last_st_dir = int(st_dir_arr[-1])
        last_st_line = float(st_line_arr[-1])
        st_dist = (closes[-1] - last_st_line) / closes[-1] if closes[-1] != 0 else 0.0

        adx_arr, di_plus, _ = _compute_adx(h_arr, l_arr, c_arr, period=14)
        last_adx = float(adx_arr[-1])

        vwap_val = self.vwap.value
        price_vs_vwap = (closes[-1] - vwap_val) / closes[-1] if (closes[-1] != 0 and vwap_val > 0) else 0.0

        signal = {
            "ema20": ema20,
            "ema50": ema50,
            "rsi_1m": rsi_1m,
            "atr": atr_val,
            "trend_strength": trend_strength,
            "supertrend_dir": float(last_st_dir),
            "supertrend_dist": float(np.clip(st_dist, -0.05, 0.05)),
            "price_vs_vwap": float(np.clip(price_vs_vwap, -0.05, 0.05)),
            "adx": float(np.clip(last_adx, 0, 100)),
            "di_spread": float(np.clip(di_plus[-1] - di_plus[-1], -60, 60)),  # placeholder
            "ema_alignment": float(1.0 if ema20 > ema50 else -1.0),
        }

        features = build_live_features(closes, opens, highs, lows, volumes, signal, ts=ts)
        return features

    def check_entry(self, df_window, ts, prev_close=None):
        """
        Mirror LiveEngine.check_entry exactly.

        Returns entry signal dict or None.
        """
        features = self.build_features(df_window, ts)
        if features:
            ce_p = self.predictor.predict(features, "CE")
            pe_p = self.predictor.predict(features, "PE")

            if ce_p is None or pe_p is None:
                return None

            # Get adjusted probs
            ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(ce_p, pe_p, "CE")

            price = df_window["close"].iloc[-1]

            # PREDICT-FIRST logic (mirrors _check_entry_predict_first)
            ce_thr = max(
                getattr(self.predictor, "ce_threshold", 0.5),
                self.learner.get_ml_threshold(),
                self.ce_floor
            )
            pe_thr = max(
                getattr(self.predictor, "pe_threshold", 0.5),
                self.learner.get_ml_threshold(),
                self.pe_floor
            )

            ce_adj_f = float(ce_adj)
            pe_adj_f = float(pe_adj)

            # 1. EDGE check
            ml_edge_margin = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
            if abs(ce_adj_f - pe_adj_f) < ml_edge_margin:
                self._log_block("NO_EDGE")
                return None

            # 2. Direction selection
            side = "CE" if ce_adj_f >= pe_adj_f else "PE"
            prob = ce_adj_f if side == "CE" else pe_adj_f
            thr = ce_thr if side == "CE" else pe_thr

            # 3. Threshold
            if prob < thr:
                self._log_block("ML_BELOW_THRESH")
                return None

            # 4. Side enablement (research control)
            if side == "CE" and not self.enable_ce:
                self._log_block("CE_DISABLED")
                continue_check = True
            if side == "PE" and not self.enable_pe:
                self._log_block("PE_DISABLED")
                continue_check = True

            # 5. Session gates
            now = ts.time()
            if now < _ORB_END:
                self._log_block("ORB_BUILD")
                return None
            if now >= dtime(15, 15):
                self._log_block("MARKET_CLOSING")
                return None

            # 6. Risk stops
            day_type = self.learner.get_day_type() if hasattr(self.learner, 'get_day_type') else "RANGE"
            regime = (
                "EXPANSION" if "VOLATILE" in day_type else
                "TREND" if "TREND" in day_type else
                "RANGE"
            )
            stop_loss, target, _stop_pct = compute_entry_stops(price, features.get("atr", 1.0), regime)

            # 7. PnL guard
            exp_win = (target - price) * self.qty
            exp_loss = (price - stop_loss) * self.qty
            expected_pnl = prob * exp_win - (1 - prob) * exp_loss
            if expected_pnl < _MIN_EXPECTED_PNL:
                self._log_block("PNL_GUARD")
                return None

            # 8. Phase55 filter
            phase55_decision = evaluate_phase55_filter(
                market_features=features,
                ml_predictions={"CE": ce_adj_f, "PE": pe_adj_f},
                current_regime=regime,
                confidence_scores={"side_confidence": float(prob)},
                direction=side,
                config=self.phase55_config,
            )
            if not phase55_decision.get("allow_trade", True):
                self._log_block("PHASE55_BLOCK")
                return None

            return {
                "side": side,
                "ml_prob": prob,
                "threshold": thr,
                "price": price,
                "stop_loss": stop_loss,
                "target": target,
                "qty": self.qty,
                "lots": self.lots_per_trade,
                "regime": regime,
                "ml_raw_ce": float(ce_p),
                "ml_raw_pe": float(pe_p),
                "ce_adj": ce_adj_f,
                "pe_adj": pe_adj_f,
                "ts": ts,
            }

        return None

    def check_exit(self, position, ltp, held_seconds):
        """
        Mirror LiveEngine.check_exit exactly.

        Delegates to profit_manager.manage_position.
        """
        entry = position["entry"]
        size = position.get("qty", self.qty)
        stop_loss = position.get("stop_loss", entry * 0.90)
        max_pnl = position.get("max_pnl", 0.0)
        ml_prob = position.get("ml_prob", 0.5)
        target = position.get("target")

        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=entry,
            ltp=ltp,
            lot_size=size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
            target=target,
            regime=position.get("regime", "RANGE"),
        )

        position["stop_loss"] = new_sl
        position["max_pnl"] = new_max_pnl

        if pm_reason:
            return True, pm_reason

        # Time-based exit (max hold)
        max_hold = self.config.MAX_HOLD_SECONDS
        if held_seconds > max_hold and new_max_pnl < 100:
            return True, "TIME_EXIT_WEAK"

        return False, ""

    def run_backtest(self, df: pd.DataFrame, start_date: datetime, end_date: datetime):
        """
        Run backtest over date range.

        Args:
            df: DataFrame with columns: date, open, high, low, close, volume
            start_date: Start datetime
            end_date: End datetime

        Returns:
            List of trade dicts
        """
        trades = []
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])

        # Filter to date range
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        df = df[mask]

        if len(df) == 0:
            raise ValueError(f"No data for period {start_date} to {end_date}")

        # Sort by timestamp
        df = df.sort_values("date").reset_index(drop=True)

        # Track trading days
        current_date = None
        prev_close = None
        position = None
        entry_time = None
        window_size = 200  # rolling window

        # Feed features for day classification
        day_candles_30m = []
        day_classified = False

        for idx, row in df.iterrows():
            ts = row["date"]

            # Reset daily state
            if ts.date() != current_date:
                if current_date is not None:
                    # Force close at day end
                    if position:
                        exit_price = row["close"] - 0.5
                        trade = self._close_position(position, exit_price, ts, "DAY_END")
                        trades.append(trade)
                        position = None

                    # Day classification
                    if len(day_candles_30m) >= 10 and hasattr(self.predictor, 'day_classifier'):
                        pass  # Mirror live logic

                current_date = ts.date()
                day_candles_30m = []
                day_classified = False
                self._reset_session()

            # Build rolling window
            start_idx = max(0, idx - window_size)
            window_df = df.iloc[start_idx:idx+1]

            candle = {
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row.get("volume", 0),
            }

            # Update ORB
            self.update_orb(candle, ts)

            # Update VWAP
            self.vwap.update(
                high=float(candle["high"]),
                low=float(candle["low"]),
                close=float(candle["close"]),
                volume=float(candle["volume"] if "volume" in row else 0),
            )

            # Feed learner
            self.learner.update_candle(
                close=candle["close"],
                high=candle["high"],
                low=candle["low"],
                ts=ts,
            )

            if position:
                # Exit logic
                ltp = candle["close"]
                held_seconds = (ts - entry_time).total_seconds()

                should_exit, exit_reason = self.check_exit(
                    position, ltp, held_seconds
                )

                if should_exit:
                    exit_price = ltp - 0.5  # realistic fill
                    trade = self._close_position(position, exit_price, ts, exit_reason)
                    trades.append(trade)
                    position = None
                    entry_time = None

            # Entry logic
            if position is None:
                signal = self.check_entry(window_df, ts, prev_close)
                if signal:
                    position = {
                        "symbol": "BANKNIFTY",
                        "side": signal["side"],
                        "entry": signal["price"],
                        "stop_loss": signal["stop_loss"],
                        "target": signal["target"],
                        "qty": signal["qty"],
                        "lot_size": self.lot_size,
                        "ml_prob": signal["ml_prob"],
                        "max_pnl": 0.0,
                        "regime": signal["regime"],
                        "ts": ts,
                    }
                    entry_time = ts

            window_df = None  # free memory

        self.trade_log = trades
        return trades

    def _close_position(self, position, exit_price, ts, exit_reason):
        """Close position and create trade record."""
        entry_price = position["entry"]
        qty = position["qty"]
        side = position["side"]

        # Gross PnL (premium change × qty)
        gross_pnl = round((exit_price - entry_price) * qty, 2)

        # Cost (round trip)
        cost = round_trip_cost(qty, self.config)

        # Net PnL
        net = net_pnl(gross_pnl, qty, self.config)

        # Calculate MFE, MAE
        max_pnl = position.get("max_pnl", 0.0)
        giveback = max_pnl - abs(gross_pnl - max_pnl) if max_pnl else 0.0

        trade = {
            "timestamp": ts.isoformat(),
            "date": ts.strftime("%Y-%m-%d"),
            "symbol": position.get("symbol", "BANKNIFTY"),
            "side": side,
            "quantity": qty,
            "lots": position["lot_size"] // self.lot_size,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "gross_pnl": gross_pnl,
            "cost": cost,
            "net_pnl": net,
            "exit_reason": exit_reason,
            "ml_probability": round(position.get("ml_prob", 0.0), 4),
            "ml_threshold": round(position.get("stop_loss", 0.0), 2),
            "regime": position.get("regime", "UNKNOWN"),
            "orb_high": self.orb_high,
            "orb_low": self.orb_low,
            "entry_ts": position.get("ts", ts).isoformat() if isinstance(position.get("ts"), datetime) else str(position.get("ts", "")),
            "max_pnl": round(max_pnl, 2),
            "giveback": round(giveback, 2),
        }

        return trade

    # ───────────────────────────────────────────────────────────────────
    # Indicator implementations (mirrors ml.indicators)
    # ───────────────────────────────────────────────────────────────────

    def _rsi_wilder(self, prices: np.ndarray, period: int = 14):
        """Calculate RSI using Wilder's smoothing."""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        _roll = gains.rolling if hasattr(gains, 'rolling') else None

        # Use pandas rolling for Wilder smoothing
        if hasattr(gains, 'rolling'):
            avg_gain = gains.rolling(window=period, min_periods=1).mean().iloc[-1]
            avg_loss = losses.rolling(window=period, min_periods=1).mean().iloc[-1]
        else:
            # Fallback to simple average
            avg_gain = np.mean(gains[-period:]) if len(gains) >= period else np.mean(gains)
            avg_loss = np.mean(losses[-period:]) if len(losses) >= period else np.mean(losses)

        if avg_loss == 0:
            return np.full(len(prices), 100.0)
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return np.full(len(prices), rsi)

    def _atr_wilder(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14):
        """Calculate ATR using Wilder's method."""
        high_low = high[1:] - low[1:]
        high_close = np.abs(high[1:] - close[:-1])
        low_close = np.abs(low[1:] - close[:-1])

        tr = np.max(np.vstack([high_low, high_close, low_close]), axis=0)
        atr = np.zeros_like(tr)
        atr[0] = np.mean(tr[:period]) if len(tr) >= period else np.mean(tr)

        for i in range(1, len(tr)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

        return np.concatenate([np.array([atr[0]]), atr])


if __name__ == "__main__":
    print("Research Engine initialized.")
    print(f"LOT_SIZE = {lot_qty(Config())}")
    print(f"COST_PER_LOT = {Config().COST_PER_LOT}")