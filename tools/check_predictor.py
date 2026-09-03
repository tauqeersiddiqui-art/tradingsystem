#!/usr/bin/env python3
"""
Tool to check predictor thresholds and raw model probabilities.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.predictor_champion import ChampionPredictor
from engine.data.candle_builder import CandleBuilder
from engine.config.config import Config, INDEX_TOKEN, HIST_CSV

def main():
    print("=== CHECKING PREDICTOR THRESHOLDS ===")
    p = ChampionPredictor()
    print(f"CE Thr: {p.ce_threshold}")
    print(f"PE Thr: {p.pe_threshold}")

    print("\n=== LOADING SAMPLE HISTORICAL DATA ===")
    # Get recent historical candles
    cb = CandleBuilder(None, INDEX_TOKEN, max_candles=100)
    # Seed from CSV to get some data
    cb.seed_from_csv(HIST_CSV, n=50)
    df = cb.get_window(50)
    if df is None:
        print("ERROR: Could not get candle window")
        return

    # Extract OHLCV lists
    closes = df['close'].tolist()
    opens = df['open'].tolist()
    highs = df['high'].tolist()
    lows = df['low'].tolist()
    volumes = df['volume'].tolist()

    # Build a minimal signal dict (we'll use zeros for missing precomputed values)
    # This is just to get feature vectors for testing
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

    # Build features for the last candle
    from ml.feature_config import build_live_features
    features = build_live_features(closes, opens, highs, lows, volumes, signal)

    # Prepare feature array in correct order
    from ml.feature_config import FEATURE_COLUMNS
    X = [[features[f] for f in FEATURE_COLUMNS]]
    import pandas as pd
    X_df = pd.DataFrame(X, columns=FEATURE_COLUMNS)

    # Get raw model outputs
    print("\n=== RAW MODEL OUTPUTS (first 10 samples) ===")
    ce_raw_probs = p.ce_model.base_model.predict_proba(X_df)[:, 1]
    pe_raw_probs = p.pe_model.base_model.predict_proba(X_df)[:, 1]

    print(f"CE raw probs: {ce_raw_probs[:10].tolist()}")
    print(f"PE raw probs: {pe_raw_probs[:10].tolist()}")

    # Also show calibrated
    ce_cal_probs = p.ce_model.predict_proba(X_df)[:, 1]
    pe_cal_probs = p.pe_model.predict_proba(X_df)[:, 1]
    print(f"CE calibrated probs: {ce_cal_probs[:10].tolist()}")
    print(f"PE calibrated probs: {pe_cal_probs[:10].tolist()}")

if __name__ == "__main__":
    main()