#!/usr/bin/env python3
"""
tools/build_stop_mfe_dataset.py

Build dataset for ML exits:
- p_stop_hit: probability that stop is hit before target within horizon
- expected_mfe: expected maximum favorable excursion (in points)

Inputs:
  data/historical/banknifty_1m_full.csv

Outputs:
  data/stop_mfe_dataset.csv with columns:
    timestamp, score, direction, horizon, entry_price,
    mfe_pts, mae_pts, stop_hit (0/1), pnl_pts

Usage:
  python tools/build_stop_mfe_dataset.py --candles data/historical/banknifty_1m_full.csv --out data/stop_mfe_dataset.csv --horizons 5,15
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.make_signals_with_future_pnl import add_features, load_models, FEATURE_NAMES


def compute_stop_mfe(df, idx, horizon, stop_pts=8.0, target_pts=16.0):
    """
    Simulate trade from idx with given stop/target.
    Returns: mfe_pts, mae_pts, stop_hit (0/1), pnl_pts
    """
    if idx + 1 >= len(df):
        return np.nan, np.nan, np.nan, np.nan

    entry_price = df["open"].iloc[idx + 1]
    side = 1  # assume long for now; direction handled by caller

    max_fav = 0.0
    max_adv = 0.0
    stop_hit = 0
    exit_price = None

    for h in range(1, horizon + 1):
        if idx + h >= len(df):
            break
        cur_high = df["high"].iloc[idx + h]
        cur_low = df["low"].iloc[idx + h]
        cur_close = df["close"].iloc[idx + h]

        # For long: favorable = high - entry, adverse = entry - low
        fav = cur_high - entry_price
        adv = entry_price - cur_low

        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

        # Check stop hit (adverse exceeds stop)
        if adv >= stop_pts and exit_price is None:
            stop_hit = 1
            exit_price = entry_price - stop_pts

        # Check target hit
        if fav >= target_pts and exit_price is None:
            exit_price = entry_price + target_pts

    if exit_price is None:
        # Exit at horizon close
        exit_price = df["close"].iloc[min(idx + horizon, len(df) - 1)]

    pnl_pts = exit_price - entry_price

    return max_fav, max_adv, stop_hit, pnl_pts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles", default="data/historical/banknifty_1m_full.csv")
    parser.add_argument("--out", default="data/stop_mfe_dataset.csv")
    parser.add_argument("--horizons", default="5,15", help="Comma-separated minutes")
    parser.add_argument("--stop-pts", type=float, default=8.0)
    parser.add_argument("--target-pts", type=float, default=16.0)
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]

    print(f"Loading candles from {args.candles}...")
    df = pd.read_csv(args.candles)
    print(f"  Rows: {len(df)}")

    print("Adding features...")
    df = add_features(df)

    print("Loading models...")
    models, thresholds = load_models()

    feature_file = "ml/models/champion_ce_lgbm_features.txt"
    if os.path.exists(feature_file):
        with open(feature_file) as f:
            model_features = [line.strip() for line in f if line.strip()]
    else:
        model_features = FEATURE_NAMES

    print("Building stop/MFE dataset...")
    rows = []
    warmup = 60

    for idx in range(warmup, len(df) - max(horizons) - 1):
        # Get ML score
        feat_row = df.iloc[idx]
        X = np.array([[feat_row.get(f, 0) for f in model_features]])

        ce_p = 0.5
        pe_p = 0.5

        if "ce" in models:
            try:
                ce_p = models["ce"].predict_proba(X)[0, 1] if hasattr(models["ce"], "predict_proba") else models["ce"].predict(X)[0]
            except:
                pass

        if "pe" in models:
            try:
                pe_p = models["pe"].predict_proba(X)[0, 1] if hasattr(models["pe"], "predict_proba") else models["pe"].predict(X)[0]
            except:
                pass

        if ce_p >= pe_p:
            direction = "CE"
            score = ce_p
        else:
            direction = "PE"
            score = pe_p

        # For PE, favorable move is DOWN
        for h in horizons:
            if direction == "CE":
                mfe, mae, stop_hit, pnl = compute_stop_mfe(df, idx, h, args.stop_pts, args.target_pts)
            else:
                # For PE, swap high/low logic
                if idx + 1 >= len(df):
                    mfe = mae = stop_hit = pnl = np.nan
                else:
                    entry_price = df["open"].iloc[idx + 1]
                    max_fav = 0.0
                    max_adv = 0.0
                    stop_hit = 0
                    exit_price = None

                    for h_step in range(1, h + 1):
                        if idx + h_step >= len(df):
                            break
                        cur_high = df["high"].iloc[idx + h_step]
                        cur_low = df["low"].iloc[idx + h_step]
                        cur_close = df["close"].iloc[idx + h_step]

                        # PE: favorable = entry - low (down move), adverse = high - entry
                        fav = entry_price - cur_low
                        adv = cur_high - entry_price

                        max_fav = max(max_fav, fav)
                        max_adv = max(max_adv, adv)

                        if adv >= args.stop_pts and exit_price is None:
                            stop_hit = 1
                            exit_price = entry_price + args.stop_pts
                        if fav >= args.target_pts and exit_price is None:
                            exit_price = entry_price - args.target_pts

                    if exit_price is None:
                        exit_price = df["close"].iloc[min(idx + h, len(df) - 1)]
                    pnl = exit_price - entry_price  # negative for PE win
                    mfe = max_fav
                    mae = max_adv

            rows.append({
                "timestamp": df["timestamp"].iloc[idx],
                "score": score,
                "direction": direction,
                "horizon": h,
                "entry_price": df["open"].iloc[idx + 1],
                "mfe_pts": round(mfe, 2) if np.isfinite(mfe) else np.nan,
                "mae_pts": round(mae, 2) if np.isfinite(mae) else np.nan,
                "stop_hit": int(stop_hit) if np.isfinite(stop_hit) else np.nan,
                "pnl_pts": round(pnl, 2) if np.isfinite(pnl) else np.nan,
            })

        if len(rows) % 10000 == 0:
            print(f"  Processed {len(rows)} rows...")

    out_df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out_df.to_csv(args.out, index=False)
    print(f"Saved {len(out_df)} rows to {args.out}")

    # Quick stats
    for h in horizons:
        sub = out_df[out_df["horizon"] == h]
        print(f"\nHorizon {h}m:")
        print(f"  Stop hit rate: {sub['stop_hit'].mean():.3f}")
        print(f"  MFE mean: {sub['mfe_pts'].mean():.2f}")
        print(f"  PnL mean: {sub['pnl_pts'].mean():.2f}")


if __name__ == "__main__":
    main()