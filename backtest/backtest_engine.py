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
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops

logger = logging.getLogger("backtest")

# ── Market constants ──────────────────────────────────────────────────
_MARKET_OPEN  = dtime(9, 15)
_ORB_END      = dtime(9, 30)
_DAY_CLASS_AT = dtime(9, 45)
_MARKET_CLOSE = dtime(15, 30)
_NO_ENTRY_AFTER = dtime(15, 15)

NIFTY_LOT_SIZE = 75          # standard lot size for simulation
OPTIONS_PREMIUM_PROXY = True  # simulate option price as % of spot


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

class OptionPriceSimulator:
    """
    Approximates CE/PE option price from NIFTY spot using a simple
    intrinsic + time-value model.  Good enough for directional PnL.

    In live trading, actual LTP is used. Here we simulate it.
    """

    def __init__(self, base_premium: float = 150.0, atm_vol: float = 0.12):
        self.base_premium = base_premium
        self.atm_vol      = atm_vol

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
        ep = self.price(entry_spot, atm_strike, side, entry_mins_to_close)
        xp = self.price(exit_spot,  atm_strike, side, exit_mins_to_close)
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

        # Day classifier (optional)
        self._day_clf          = None
        self._day_classified   = False
        self._day_candles_30m  = []
        self._prev_close: Optional[float] = None
        self._open_price_set   = False

        try:
            from ml.day_classifier import DayClassifier
            self._day_clf = DayClassifier()
            logger.info("[BacktestSignalEngine] DayClassifier loaded")
        except Exception as e:
            logger.warning(f"[BacktestSignalEngine] DayClassifier unavailable: {e}")

    # ── Day reset ─────────────────────────────────────────────────────

    def reset_day(self, prev_close: Optional[float] = None):
        self.orb_high        = None
        self.orb_low         = None
        self.orb_done        = False
        self._day_classified = False
        self._day_candles_30m = []
        self._prev_close     = prev_close
        self._open_price_set = False
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

        if not self._day_classified and now < _DAY_CLASS_AT:
            self._day_candles_30m.append({
                "open": row["open"], "high": row["high"],
                "low": row["low"],  "close": row["close"], "volume": row.get("volume", 0),
            })

        if not self._day_classified and now >= _DAY_CLASS_AT:
            self._day_classified = True
            if self._day_clf and len(self._day_candles_30m) >= 10:
                df30 = pd.DataFrame(self._day_candles_30m)
                self._day_clf.classify(df30, self._prev_close)

    # ── Feature builder ───────────────────────────────────────────────

    def _build_features(self, window: pd.DataFrame) -> Optional[dict]:
        if len(window) < 26:
            return None

        closes  = window["close"].tolist()
        opens   = window["open"].tolist()
        highs   = window["high"].tolist()
        lows    = window["low"].tolist()
        volumes = window["volume"].tolist() if "volume" in window.columns else [0] * len(closes)

        signal = self._compute_signal(closes, highs, lows)
        feats  = _safe_build_live_features(closes, opens, highs, lows, volumes, signal)

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

        return {
            "ema20": e20, "ema50": e50, "rsi_1m": rsi,
            "atr": atr, "trend_strength": (e20 - e50) / closes[-1] if closes[-1] else 0.0,
        }

    # ── Main step ─────────────────────────────────────────────────────

    def step(self, window: pd.DataFrame, ts: datetime) -> Optional[dict]:
        """
        Returns signal dict or None.
        Identical contract to LiveEngine.step().
        """
        row = window.iloc[-1]
        now = ts.time()

        if not self._open_price_set and now >= _MARKET_OPEN:
            self.learner.set_open_price(row["close"])
            self._open_price_set = True

        self._update_orb(row, ts)
        self._maybe_classify_day(row, ts)

        # Time gates
        if now < _ORB_END:            return None
        if now >= _NO_ENTRY_AFTER:    return None

        # Day type gate
        day = self.learner.get_day_type()
        #if day == "VOLATILE_DAY":
         #   return None

        features = self._build_features(window)
        if not features:
            return None

        price     = row["close"]
        threshold = self.learner.get_ml_threshold()

        ce_prob = self.predictor.predict(features, "CE")
        pe_prob = self.predictor.predict(features, "PE")

        if ce_prob is None or pe_prob is None:
            return None

        ce_adj, pe_adj = self.learner.get_adjusted_ml_prob(ce_prob, pe_prob, "CE")

        ce_break = self.orb_done and self.orb_high and price > self.orb_high
        pe_break = self.orb_done and self.orb_low  and price < self.orb_low

        signal = None

        # ── CE check ─────────────────────────────────────────────
        ce_thr = threshold - 0.03 if ce_break else threshold
        if ce_adj >= ce_thr:
            blocked, _ = self.learner.is_side_blocked("CE")
            if not blocked:
                atr_val = features.get("atr", price * 0.01)
                day_type = self.learner.get_day_type()
                regime = (
                    "EXPANSION" if "VOLATILE" in day_type else
                    "TREND"     if "TREND" in day_type else "RANGE"
                )
                stop_loss, target, stop_pct = compute_entry_stops(price, atr_val, regime)
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

        # ── PE check ─────────────────────────────────────────────
        if signal is None:
            pe_thr = threshold - 0.03 if pe_break else threshold
            if pe_adj >= pe_thr:
                blocked, _ = self.learner.is_side_blocked("PE")
                if not blocked:
                    atr_val = features.get("atr", price * 0.01)
                    day_type = self.learner.get_day_type()
                    regime = (
                        "EXPANSION" if "VOLATILE" in day_type else
                        "TREND"     if "TREND" in day_type else "RANGE"
                    )
                    stop_loss, target, stop_pct = compute_entry_stops(price, atr_val, regime)
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

    # ── Exit check ────────────────────────────────────────────────────

    def check_exit(self, position: dict, ltp: float, held_seconds: float) -> tuple[bool, str]:
        stop_loss = position.get("stop_loss", position["entry"] * 0.90)
        max_pnl   = position.get("max_pnl",   0.0)
        ml_prob   = position.get("ml_prob",    0.5)
        lot_size  = position.get("lot_size",   NIFTY_LOT_SIZE)

        new_sl, new_max_pnl, pm_reason = manage_position(
            entry_price=position["entry"],
            ltp=ltp,
            lot_size=lot_size,
            stop_loss=stop_loss,
            max_pnl=max_pnl,
            ml_prob=ml_prob,
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
            "LOT_SIZE":           NIFTY_LOT_SIZE,
            "LOTS_PER_TRADE":     1,
            "CHAMPION_THRESHOLD": float(os.getenv("CHAMPION_THRESHOLD", 0.42)),
            "ENTRY_ON":           "next_open",   # "next_open" or "current_close"
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
        lot_size          = self.config["LOT_SIZE"]
        qty               = lot_size * self.config["LOTS_PER_TRADE"]
        entry_on          = self.config["ENTRY_ON"]

        # Rolling window buffer: warmup history + growing day candles
        window_buf = deque(hist_df.to_dict("records"), maxlen=300)

        for i in range(len(day_df)):
            row = day_df.iloc[i]
            if i % 50000 == 0:
                print(f"[PROGRESS] {i}/{len(day_df)}")
            ts  = row["date"]
            now = ts.time()

            # Skip pre-market
            if now < _MARKET_OPEN:
                window_buf.append(row.to_dict())
                continue

            # Force exit at 15:15
            if now >= dtime(15, 15) and position is not None:
                ltp = row["close"]
                exit_price, exit_reason = ltp - 0.5, "TIME_CLOSE"
                trade = self._close_position(
                    position, exit_price, ts, exit_reason, trades_today, day_pnl
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
                ltp          = row["close"]
                held_seconds = (ts - entry_time).total_seconds()

                exit_flag, exit_reason = self.signal_engine.check_exit(
                    position, ltp, held_seconds
                )

                # Hard SL belt+suspenders
                if ltp <= position.get("stop_loss", position["entry"] * 0.90):
                    exit_flag, exit_reason = True, "STOP"

                if exit_flag:
                    exit_price = (row["open"] if entry_on == "next_open" else ltp) - 0.5
                    trade = self._close_position(
                        position, exit_price, ts, exit_reason,
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

                    side = signal["side"]
                    features = signal.get("features", {})

                    # TREND FILTER (CORRECT PLACE)
                    ema20 = features.get("ema20")
                    ema50 = features.get("ema50")

                    if ema20 < ema50 and side == "CE":
                        continue

                    if ema20 > ema50 and side == "PE":
                        continue

                    # Entry price: next candle open or current close
                    if entry_on == "next_open" and i < len(day_df) - 1:
                        next_row = day_df.iloc[i + 1]
                        entry_price = float(next_row["open"]) + 0.5
                    else:
                        entry_price = float(row["close"]) + 0.5

                    stop_loss = signal.get("stop_loss", entry_price * 0.90)
                    target    = signal.get("target",    entry_price * 1.05)

                    # ATM strike proxy
                    atm_strike = round(entry_price / 50) * 50

                    # FIX: define mins_to_close
                    mins_to_close = max(
                        (_MARKET_CLOSE.hour * 60 + _MARKET_CLOSE.minute) -
                        (ts.hour * 60 + ts.minute),
                        1
                    )

                    position = {
                        "symbol":      f"NIFTY_{side}_{atm_strike}",
                        "side":        side,
                        "entry":       entry_price,
                        "qty":         qty,
                        "lot_size":    lot_size,
                        "stop_loss":   stop_loss,
                        "target":      target,
                        "max_pnl":     0.0,
                        "ml_prob":     signal["ml_prob"],
                        "features":    features,
                        "regime":      signal.get("regime", "UNKNOWN"),
                        "reason":      signal.get("reason", ""),
                        "atm_strike":  atm_strike,
                        "entry_mins_to_close": mins_to_close,
                        "orb_high":    self.signal_engine.orb_high,
                        "orb_low":     self.signal_engine.orb_low,
                        "_entry_ts":   ts,
                    }
                    entry_time    = ts
                    trades_today += 1

        # ── End of day force-close ────────────────────────────────────
        if position is not None:
            ltp        = day_df["close"].iloc[-1]
            trade = self._close_position(
                position, ltp - 0.5, day_df["date"].iloc[-1],
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
        exit_price:  float,
        exit_time:   datetime,
        exit_reason: str,
        trades_today: int,
        day_pnl:     float,
    ) -> Trade:

        self._trade_id += 1
        entry     = position["entry"]
        side      = position["side"]
        qty       = position["qty"]
        lot_size  = position["lot_size"]

        # Raw points PnL
        # CE: profit if price rises; PE: profit if price falls
        exit_mins_to_close = max(
            (_MARKET_CLOSE.hour * 60 + _MARKET_CLOSE.minute) -
            (exit_time.hour * 60 + exit_time.minute), 1
        )

        pnl = _opt_sim.pnl(
            entry_spot=entry,
            exit_spot=exit_price,
            atm_strike=position["atm_strike"],
            side=side,
            qty=qty,
            entry_mins_to_close=position["entry_mins_to_close"],
            exit_mins_to_close=exit_mins_to_close,
        )

        held_secs = (exit_time - position["_entry_ts"]).total_seconds()

        return Trade(
            trade_id      = self._trade_id,
            date          = str(exit_time.date()),
            entry_time    = position["_entry_ts"],   # actual entry_time stored in caller
            exit_time     = exit_time,
            side          = side,
            entry_price   = entry,
            exit_price    = exit_price,
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
            stop_loss     = position.get("stop_loss", entry * 0.90),
            target        = position.get("target", entry * 1.05),
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

    print("🚀 Starting Backtest...")

    engine = BacktestEngine()

    df = engine.load_data(r"D:\All Bots\trading_system\data\historical\nifty_1m_full.csv")
    df = df.tail(200000)
    print("Data loaded:", df.shape)

    results = engine.run(df)

    print("\n===== BACKTEST RESULTS =====")
    for k, v in results.items():
        print(f"{k}: {v}")

    engine.save_results()