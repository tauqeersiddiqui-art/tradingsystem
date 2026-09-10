#!/usr/bin/env python3
"""
tools/calibrate_and_size.py

Fit probability calibration (Platt/Isotonic) on training window, evaluate on holdout.
Save calibration model and produce sample calibrated sizes.

Inputs:
  data/signals_with_future_pnl.csv
  (uses future_pnl_5m as target: >0 = win, <=0 = loss)

Outputs:
  models/calibration_model.pkl
  artifacts/sample_calibrated_sizes.csv

Usage:
  python tools/calibrate_and_size.py --signals data/signals_with_future_pnl.csv --horizon 5 --cost 66 --min-trades 50
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss

# Add repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def calibrate_probabilities(signals_df, horizon, n_splits=5):
    """
    Fit calibration on training folds, pick best method by holdout logloss.
    Returns best calibrator and its logloss.
    """
    pnl_col = f"future_pnl_{horizon}m"
    df = signals_df.dropna(subset=[pnl_col, "score"]).copy()
    df = df.sort_values("timestamp").reset_index(drop=True)

    X = df["score"].values.reshape(-1, 1)
    y = (df[pnl_col] > 0).astype(int).values

    tscv = TimeSeriesSplit(n_splits=n_splits)

    methods = {
        "platt": LogisticRegression(solver="lbfgs", max_iter=1000),
        "isotonic": IsotonicRegression(out_of_bounds="clip"),
    }

    best_method = None
    best_calibrator = None
    best_logloss = np.inf
    fold_loglosses = {k: [] for k in methods}

    for train_idx, test_idx in tscv.split(df):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        for name, model in methods.items():
            try:
                model.fit(X_train.ravel(), y_train)
                if name == "platt":
                    p_pred = model.predict_proba(X_test)[:, 1]
                else:
                    p_pred = model.predict(X_test.ravel())
                p_pred = np.clip(p_pred, 1e-6, 1 - 1e-6)
                ll = log_loss(y_test, p_pred)
                fold_loglosses[name].append(ll)
            except Exception as e:
                fold_loglosses[name].append(np.inf)

    # Pick method with lowest mean logloss
    for name, losses in fold_loglosses.items():
        mean_ll = np.mean(losses)
        print(f"  {name}: mean logloss = {mean_ll:.4f}")
        if mean_ll < best_logloss:
            best_logloss = mean_ll
            best_method = name

    # Refit best on all data
    print(f"\nBest method: {best_method} (logloss={best_logloss:.4f})")
    best_model = methods[best_method]
    best_model.fit(X.ravel(), y)

    return best_model, best_method, best_logloss


def compute_size_frac(calibrated_p, edge_fraction=0.3, max_frac=0.02):
    """
    Confidence adjustment from allocator:
    clip(edge_fraction * (2*p - 1), 0.0, max_frac)
    """
    if calibrated_p is None or not np.isfinite(calibrated_p):
        return 0.0
    adj = edge_fraction * (2.0 * calibrated_p - 1.0)
    return float(np.clip(adj, 0.0, max_frac))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", default="data/signals_with_future_pnl.csv")
    parser.add_argument("--horizon", type=int, default=5, help="Horizon for calibration target")
    parser.add_argument("--cost", type=float, default=66.0, help="Cost per lot per side")
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--edge-fraction", type=float, default=0.3)
    parser.add_argument("--max-frac", type=float, default=0.02)
    parser.add_argument("--out-model", default="models/calibration_model.pkl")
    parser.add_argument("--out-sizes", default="artifacts/sample_calibrated_sizes.csv")
    args = parser.parse_args()

    print(f"Loading signals from {args.signals}...")
    df = pd.read_csv(args.signals)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    print(f"  Rows: {len(df)}")

    print(f"Calibrating on {args.horizon}m horizon...")
    calibrator, method, logloss = calibrate_probabilities(df, args.horizon)

    # Apply calibration to all signals
    X_all = df["score"].values.reshape(-1, 1)
    if method == "platt":
        calibrated_p = calibrator.predict_proba(X_all)[:, 1]
    else:
        calibrated_p = calibrator.predict(X_all.ravel())
    calibrated_p = np.clip(calibrated_p, 1e-6, 1 - 1e-6)

    # Compute size fractions
    size_frac = np.array([compute_size_frac(p, args.edge_fraction, args.max_frac) for p in calibrated_p])

    # Save model
    os.makedirs(os.path.dirname(args.out_model), exist_ok=True)
    model_data = {
        "model": calibrator,
        "method": method,
        "logloss": logloss,
        "horizon": args.horizon,
        "edge_fraction": args.edge_fraction,
        "max_frac": args.max_frac,
    }
    joblib.dump(model_data, args.out_model)
    print(f"Saved calibration model to {args.out_model}")

    # Save sample sizes
    out_df = pd.DataFrame({
        "timestamp": df["timestamp"],
        "score": df["score"],
        "direction": df["direction"],
        "p": calibrated_p,
        "size_frac": size_frac,
        "future_pnl": df[f"future_pnl_{args.horizon}m"] if f"future_pnl_{args.horizon}m" in df.columns else np.nan,
    })
    os.makedirs(os.path.dirname(args.out_sizes), exist_ok=True)
    out_df.to_csv(args.out_sizes, index=False)
    print(f"Saved sample sizes to {args.out_sizes}")

    # Stats
    print(f"\nCalibration stats:")
    print(f"  Method: {method}")
    print(f"  Logloss: {logloss:.4f}")
    print(f"  Calibrated p range: {calibrated_p.min():.4f} - {calibrated_p.max():.4f}")
    print(f"  Size frac range: {size_frac.min():.6f} - {size_frac.max():.6f}")
    print(f"  Mean size frac: {size_frac.mean():.6f}")
    print(f"  Non-zero sizes: {(size_frac > 0).sum()} / {len(size_frac)}")


if __name__ == "__main__":
    main()