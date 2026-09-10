#!/usr/bin/env python3
"""
tools/make_signals_with_future_pnl.py

Generate signal dataset with future P&L at multiple horizons from minute candles.
Uses the trained ML models to produce probabilities, then computes forward P&L.

Input: data/historical/banknifty_1m_full.csv
Output: data/signals_with_future_pnl.csv

Columns:
  timestamp, score, direction, future_pnl_1m, future_pnl_3m, future_pnl_5m, future_pnl_15m,
  entry_price, option_type, synthetic_flag

Usage:
  python tools/make_signals_with_future_pnl.py --candles data/historical/banknifty_1m_full.csv --out data/signals_with_future_pnl.csv
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd
import joblib
from pathlib import Path

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.portfolio.allocator import CapitalAllocator


# ──────────────────────────────────────────────────────────────────────────────
# Feature engineering (match ml_intraday_learner / training pipeline)
# ──────────────────────────────────────────────────────────────────────────────

FEATURE_NAMES = [
    "supertrend_dir", "supertrend_dist", "price_vs_vwap", "adx", "di_spread",
    "ema_alignment", "volume_ratio", "ema20", "ema50", "macd", "returns",
    "volatility", "rsi", "atr", "trend_strength", "hour", "weekday",
    "return_1", "return_3", "candle_body_pct", "range_break_strength",
    "mins_since_open", "mins_to_close", "session_open", "session_close",
    "time_to_expiry_min", "moneyness", "momentum_velocity", "range_compression",
    "wick_ratio", "body_efficiency", "mom3_strength", "upper_wick",
    "lower_wick", "close_position"
]


def compute_atr(high, low, close, period=14):
    """Wilder ATR."""
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    a = 1.0 / period
    rma = np.empty(len(tr))
    rma[0] = tr[0]
    for i in range(1, len(tr)):
        rma[i] = (tr[i] - rma[i-1]) * a + rma[i-1]
    return np.concatenate([[np.nan], rma])


def add_features(df):
    """Add all features required by the ML models."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["date"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Basic
    df["returns"] = df["close"].pct_change()
    df["return_1"] = df["close"].pct_change(1)
    df["return_3"] = df["close"].pct_change(3)

    # EMA
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema_alignment"] = (df["ema20"] > df["ema50"]).astype(float)

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    df["atr"] = compute_atr(df["high"].values, df["low"].values, df["close"].values)

    # Volatility
    df["volatility"] = df["returns"].rolling(20).std()

    # VWAP
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_vol_typ"] = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"] = df["cum_vol_typ"] / df["cum_vol"].replace(0, np.nan)
    df["price_vs_vwap"] = (df["close"] - df["vwap"]) / df["close"]

    # ADX / DI (simplified)
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    df["plus_dm"] = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    df["minus_dm"] = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum(np.abs(df["high"] - df["close"].shift(1)),
                               np.abs(df["low"] - df["close"].shift(1))))
    atr14 = tr.rolling(14).mean()
    plus_di = 100 * df["plus_dm"].rolling(14).mean() / atr14
    minus_di = 100 * df["minus_dm"].rolling(14).mean() / atr14
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.rolling(14).mean()
    df["di_spread"] = (plus_di - minus_di) / 100

    # Volume ratio
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)

    # Candle metrics
    df["candle_body"] = np.abs(df["close"] - df["open"])
    df["candle_range"] = df["high"] - df["low"]
    df["candle_body_pct"] = df["candle_body"] / df["candle_range"].replace(0, np.nan)
    df["upper_wick"] = df["high"] - np.maximum(df["open"], df["close"])
    df["lower_wick"] = np.minimum(df["open"], df["close"]) - df["low"]
    df["wick_ratio"] = df["upper_wick"] / (df["lower_wick"] + 1e-9)
    df["body_efficiency"] = df["candle_body"] / df["candle_range"].replace(0, np.nan)
    df["close_position"] = (df["close"] - df["low"]) / df["candle_range"].replace(0, np.nan)

    # Trend strength
    df["trend_strength"] = (df["close"] - df["close"].shift(5)) / df["atr"].replace(0, np.nan)

    # Momentum
    df["momentum_velocity"] = df["close"].diff(3) / df["atr"].replace(0, np.nan)
    df["mom3_strength"] = df["close"].pct_change(3)

    # Range compression
    df["range_compression"] = df["candle_range"] / df["candle_range"].rolling(20).mean().replace(0, np.nan)

    # Range break
    df["range_break_strength"] = (df["close"] - df["high"].rolling(20).max()) / df["atr"].replace(0, np.nan)

    # Time features
    df["hour"] = df["timestamp"].dt.hour
    df["weekday"] = df["timestamp"].dt.weekday
    df["mins_since_open"] = (df["timestamp"].dt.hour - 9) * 60 + df["timestamp"].dt.minute - 15
    df["mins_to_close"] = (15 - df["timestamp"].dt.hour) * 60 - df["timestamp"].dt.minute + 15
    df["session_open"] = (df["hour"] == 9).astype(float)
    df["session_close"] = (df["hour"] == 15).astype(float)

    # Supertrend (simplified)
    hl2 = (df["high"] + df["low"]) / 2
    atr10 = compute_atr(df["high"].values, df["low"].values, df["close"].values, period=10)
    basic_ub = hl2 + 3 * atr10
    basic_lb = hl2 - 3 * atr10
    final_ub = basic_ub.copy()
    final_lb = basic_lb.copy()
    supertrend = np.full(len(df), np.nan)
    direction = np.full(len(df), 1)
    for i in range(1, len(df)):
        if df["close"].iloc[i-1] <= final_ub[i-1]:
            final_ub[i] = min(basic_ub[i], final_ub[i-1])
        else:
            final_ub[i] = basic_ub[i]
        if df["close"].iloc[i-1] >= final_lb[i-1]:
            final_lb[i] = max(basic_lb[i], final_lb[i-1])
        else:
            final_lb[i] = basic_lb[i]
        if direction[i-1] == 1 and df["close"].iloc[i] > final_ub[i]:
            direction[i] = -1
        elif direction[i-1] == -1 and df["close"].iloc[i] < final_lb[i]:
            direction[i] = 1
        else:
            direction[i] = direction[i-1]
        supertrend[i] = final_lb[i] if direction[i] == 1 else final_ub[i]
    df["supertrend_dir"] = direction
    df["supertrend_dist"] = (df["close"] - supertrend) / df["close"]

    # Option-like features (synthetic)
    df["time_to_expiry_min"] = 60 * 6.5 - df["mins_since_open"]  # approx weekly expiry
    df["moneyness"] = 0.5  # ATM approx

    return df


def load_models():
    """Load CE and PE models + thresholds."""
    models = {}
    thresholds = {}
    for side in ["ce", "pe"]:
        # Try LGBM first, then CatBoost
        for model_type in ["lgbm", "cat"]:
            pkl = f"ml/models/champion_{side}_{model_type}.pkl"
            if os.path.exists(pkl):
                models[side] = joblib.load(pkl)
                print(f"Loaded {pkl}")
                break
        # Threshold
        thr_file = f"ml/models/champion_{side}_{model_type}_threshold.txt"
        if os.path.exists(thr_file):
            with open(thr_file) as f:
                thresholds[side] = float(f.read().strip())
            print(f"Loaded threshold {side}: {thresholds[side]}")
        else:
            thresholds[side] = 0.55
    return models, thresholds


def compute_future_pnl(df, idx, horizons, cost_per_lot=66):
    """
    Compute future P&L at multiple horizons.
    Entry at next candle open. Exit at horizon candle close.
    Simplified: uses underlying points, then converts to Rs assuming 1 lot (30 qty).
    Synthetic flag = True since no real option premium data.
    """
    results = {}
    if idx + 1 >= len(df):
        return {f"future_pnl_{h}m": np.nan for h in horizons}

    entry_price = df["open"].iloc[idx + 1]  # next candle open

    for h in horizons:
        exit_idx = idx + h
        if exit_idx >= len(df):
            results[f"future_pnl_{h}m"] = np.nan
        else:
            exit_price = df["close"].iloc[exit_idx]
            pnl_pts = exit_price - entry_price
            # Convert to Rs: 1 lot = 30 qty, cost = 66 per side = 132 round-trip
            # But we compute gross PnL, cost applied later in threshold sweep
            results[f"future_pnl_{h}m"] = pnl_pts * 30  # 30 qty per lot

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles", default="data/historical/banknifty_1m_full.csv")
    parser.add_argument("--out", default="data/signals_with_future_pnl.csv")
    parser.add_argument("--horizons", default="1,3,5,15", help="Comma-separated minutes")
    parser.add_argument("--cost", type=float, default=66.0, help="Cost per lot per side (INR)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]

    print(f"Loading candles from {args.candles}...")
    df = pd.read_csv(args.candles)
    print(f"  Rows: {len(df)}")

    print("Adding features...")
    df = add_features(df)

    print("Loading models...")
    models, thresholds = load_models()

    # Load feature names for the specific model
    feature_file = "ml/models/champion_ce_lgbm_features.txt"
    if os.path.exists(feature_file):
        with open(feature_file) as f:
            model_features = [line.strip() for line in f if line.strip()]
    else:
        model_features = FEATURE_NAMES

    print("Generating signals...")
    rows = []

    # Process each row (skip warmup period)
    warmup = 60  # need 60 bars for indicators
    for idx in range(warmup, len(df) - max(horizons) - 1):
        # Get features
        feat_row = df.iloc[idx]
        X = np.array([[feat_row.get(f, 0) for f in model_features]])

        # Predict CE and PE
        ce_prob = models.get("ce", None)
        pe_prob = models.get("pe", None)

        if ce_prob is not None:
            try:
                ce_p = ce_prob.predict_proba(X)[0, 1] if hasattr(ce_prob, "predict_proba") else ce_prob.predict(X)[0]
            except:
                ce_p = 0.5
        else:
            ce_p = 0.5

        if pe_prob is not None:
            try:
                pe_p = pe_prob.predict_proba(X)[0, 1] if hasattr(pe_prob, "predict_proba") else pe_prob.predict(X)[0]
            except:
                pe_p = 0.5
        else:
            pe_p = 0.5

        # Determine direction and score
        if ce_p >= pe_p:
            direction = "CE"
            score = ce_p
        else:
            direction = "PE"
            score = pe_p

        # Compute future P&L
        future = compute_future_pnl(df, idx, horizons, args.cost)

        rows.append({
            "timestamp": df["timestamp"].iloc[idx],
            "score": score,
            "direction": direction,
            "entry_price": df["open"].iloc[idx + 1],
            "option_type": "CE" if direction == "CE" else "PE",
            "synthetic_flag": True,
            **future
        })

        if len(rows) % 10000 == 0:
            print(f"  Processed {len(rows)} signals...")

    out_df = pd.DataFrame(rows)
    print(f"Total signals: {len(out_df)}")
    print(f"  CE: {(out_df['direction']=='CE').sum()}, PE: {(out_df['direction']=='PE').sum()}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out_df.to_csv(args.out, index=False)
    print(f"Saved to {args.out}")

    # Quick stats
    for h in horizons:
        col = f"future_pnl_{h}m"
        valid = out_df[col].dropna()
        if len(valid) > 0:
            print(f"  {h}m: mean={valid.mean():.1f}, std={valid.std():.1f}, >0: {(valid>0).mean():.3f}")


if __name__ == "__main__":
    main()