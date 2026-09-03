#!/usr/bin/env python3
"""
Tool to check feature statistics for live and training data.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from ml.feature_config import FEATURE_COLUMNS, build_live_features
from engine.config.config import Config, INDEX_TOKEN, HIST_CSV
from engine.data.candle_builder import CandleBuilder

def get_recent_live_features(n=50):
    """Get features for the last n candles from historical CSV."""
    cb = CandleBuilder(None, INDEX_TOKEN, max_candles=200)
    cb.seed_from_csv(HIST_CSV, n=200)
    df = cb.get_window(n)
    if df is None:
        raise ValueError("Could not get candle window")
    closes = df['close'].tolist()
    opens = df['open'].tolist()
    highs = df['high'].tolist()
    lows = df['low'].tolist()
    volumes = df['volume'].tolist()
    # Build a minimal signal dict (we'll use zeros for missing precomputed values)
    signal = {
        "ema20": closes[-1] if closes else 0.0,
        "ema50": closes[-1] if closes else 0.0,
        "rsi_1m": 50.0,
        "atr": 0.0,
        "trend_strength": 0.0,
        "supertrend_dir": 0,
        "supertrend_dist": 0.0,
        "price_vs_vwap": 0.0,
        "adx": 20.0,
        "di_spread": 0.0,
        "ema_alignment": 0.0,
        "volume_ratio": 1.0,
        "vwap": closes[-1] if closes else 0.0
    }
    features = build_live_features(closes, opens, highs, lows, volumes, signal)
    # Return as a DataFrame with one row (the last candle)
    return pd.DataFrame([{f: features[f] for f in FEATURE_COLUMNS}])

def get_training_features(n=100):
    """Get features for the last n rows of the training dataset."""
    train_df = pd.read_csv('ml/models/training_dataset_v3.csv')
    # We only want the feature columns, drop the target and other columns
    X = train_df[FEATURE_COLUMNS].dropna().tail(n)
    return X

def print_feature_stats(df, label):
    print(f"\n=== {label} FEATURE STATISTICS (n={len(df)}) ===")
    print(f"{'Feature':<25} | {'min':>10} | {'max':>10} | {'mean':>10} | {'std':>10} | {'zeros':>6}")
    print("-" * 90)
    for col in FEATURE_COLUMNS:
        vals = df[col]
        min_val = vals.min()
        max_val = vals.max()
        mean_val = vals.mean()
        std_val = vals.std()
        zero_count = (vals == 0).sum()
        print(f"{col:<25} | {min_val:>10.4f} | {max_val:>10.4f} | {mean_val:>10.4f} | {std_val:>10.4f} | {zero_count:>6}")

def main():
    print("Checking feature statistics...")
    # Get recent live features (50 rows)
    live_df = get_recent_live_features(50)
    print_feature_stats(live_df, "LIVE (50 recent candles)")

    # Get training features (100 rows)
    train_df = get_training_features(100)
    print_feature_stats(train_df, "TRAINING (100 rows)")

if __name__ == "__main__":
    main()