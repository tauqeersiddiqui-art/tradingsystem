#!/usr/bin/env python3
"""
tools/threshold_sweep.py

Sweep probability thresholds to find the one that maximizes expectancy.
Uses TimeSeriesSplit for walk-forward validation.

Input: data/signals_with_future_pnl.csv (from make_signals_with_future_pnl.py)
Outputs:
  artifacts/threshold_sweep_results.csv
  artifacts/best_threshold.txt

Usage:
  python tools/threshold_sweep.py --signals data/signals_with_future_pnl.csv --horizon 5 --cost 66 --min-trades 50
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

# Add repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def compute_expectancy(pnls, cost_per_trade, n_trades):
    """
    Expectancy = (sum PnL - n_trades * cost) / n_trades
    Cost is per trade (buy+sell = 2 * cost_per_lot)
    """
    if n_trades == 0:
        return -np.inf
    gross = np.sum(pnls)
    net = gross - n_trades * cost_per_trade
    return net / n_trades


def sweep_thresholds(signals_df, horizon, cost_per_lot, min_trades, n_splits=5):
    """
    Sweep thresholds using TimeSeriesSplit.
    Returns DataFrame with threshold, mean_expectancy, std_expectancy, etc.
    """
    pnl_col = f"future_pnl_{horizon}m"
    if pnl_col not in signals_df.columns:
        raise ValueError(f"Column {pnl_col} not found")

    # Drop NaN
    df = signals_df.dropna(subset=[pnl_col, "score"]).copy()
    df = df.sort_values("timestamp").reset_index(drop=True)

    thresholds = np.linspace(0.50, 0.95, 46)
    tscv = TimeSeriesSplit(n_splits=n_splits)

    results = []

    for thr in thresholds:
        fold_expectancies = []
        fold_trades = []

        for train_idx, test_idx in tscv.split(df):
            test_df = df.iloc[test_idx]

            # Filter by threshold
            mask = test_df["score"] >= thr
            selected = test_df[mask]

            n = len(selected)
            if n < min_trades:
                continue

            pnls = selected[pnl_col].values
            # Cost per trade = 2 * cost_per_lot (buy + sell)
            cost_per_trade = 2 * cost_per_lot
            exp = compute_expectancy(pnls, cost_per_trade, n)

            fold_expectancies.append(exp)
            fold_trades.append(n)

        if fold_expectancies:
            results.append({
                "threshold": round(thr, 4),
                "mean_expectancy": np.mean(fold_expectancies),
                "std_expectancy": np.std(fold_expectancies),
                "median_expectancy": np.median(fold_expectancies),
                "mean_trades": np.mean(fold_trades),
                "total_folds": len(fold_expectancies),
                "horizon": horizon,
                "cost_per_lot": cost_per_lot
            })

    return pd.DataFrame(results)


def find_best_threshold(results_df, metric="mean_expectancy"):
    """Find threshold with best metric (default: mean_expectancy)."""
    if results_df.empty:
        return None
    best_idx = results_df[metric].idxmax()
    return results_df.loc[best_idx, "threshold"], results_df.loc[best_idx]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", default="data/signals_with_future_pnl.csv")
    parser.add_argument("--horizon", type=int, default=5, help="Horizon in minutes (1,3,5,15)")
    parser.add_argument("--cost", type=float, default=66.0, help="Cost per lot per side (INR)")
    parser.add_argument("--min-trades", type=int, default=50, help="Minimum trades per fold")
    parser.add_argument("--n-splits", type=int, default=5, help="TimeSeriesSplit folds")
    parser.add_argument("--out-results", default="artifacts/threshold_sweep_results.csv")
    parser.add_argument("--out-best", default="artifacts/best_threshold.txt")
    parser.add_argument("--metric", default="mean_expectancy", choices=["mean_expectancy", "median_expectancy"])
    args = parser.parse_args()

    print(f"Loading signals from {args.signals}...")
    df = pd.read_csv(args.signals)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    print(f"  Rows: {len(df)}")

    print(f"Sweeping thresholds for {args.horizon}m horizon...")
    print(f"  Cost per lot: {args.cost}, min_trades per fold: {args.min_trades}")

    results = sweep_thresholds(df, args.horizon, args.cost, args.min_trades, args.n_splits)

    if results.empty:
        print("No valid thresholds found (insufficient trades)")
        return

    os.makedirs(os.path.dirname(args.out_results), exist_ok=True)
    results.to_csv(args.out_results, index=False)
    print(f"Results saved to {args.out_results}")

    best_thr, best_row = find_best_threshold(results, args.metric)
    if best_thr is not None:
        with open(args.out_best, "w") as f:
            f.write(str(best_thr))
        print(f"\nBest threshold ({args.metric}): {best_thr:.4f}")
        print(f"  Expectancy: {best_row['mean_expectancy']:.2f} Rs/trade")
        print(f"  Std: {best_row['std_expectancy']:.2f}")
        print(f"  Mean trades/fold: {best_row['mean_trades']:.1f}")

        # Also show top 5
        print("\nTop 5 thresholds:")
        top5 = results.nlargest(5, args.metric)
        for _, row in top5.iterrows():
            print(f"  {row['threshold']:.4f}: exp={row['mean_expectancy']:.2f}±{row['std_expectancy']:.2f}, trades={row['mean_trades']:.1f}")
    else:
        print("No best threshold found")


if __name__ == "__main__":
    main()