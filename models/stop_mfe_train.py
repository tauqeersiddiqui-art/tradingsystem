#!/usr/bin/env python3
"""
models/stop_mfe_train.py

Train ML models for exits:
- p_stop_hit: classifier predicting stop hit probability
- expected_mfe: regressor predicting max favorable excursion

Inputs:
  data/stop_mfe_dataset.csv

Outputs:
  models/stop_mfe_classifier.pkl
  models/stop_mfe_regressor.pkl

Usage:
  python models/stop_mfe_train.py --data data/stop_mfe_dataset.csv --horizon 5
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss, mean_squared_error, classification_report

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def prepare_features(df):
    """Extract features from the dataset."""
    # Score, direction, horizon, and derived features
    df = df.copy()

    # Encode direction
    df["dir_enc"] = (df["direction"] == "CE").astype(int)

    # Time features
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["hour"] = df["timestamp"].dt.hour
    df["minute"] = df["timestamp"].dt.minute
    df["mins_since_open"] = (df["hour"] - 9) * 60 + df["minute"] - 15

    # Features for model
    feature_cols = ["score", "dir_enc", "horizon", "mins_since_open"]
    return df[feature_cols].values


def train_horizon(df, horizon, out_dir="models"):
    """Train models for a specific horizon."""
    print(f"\n=== Training for {horizon}m horizon ===")

    sub = df[df["horizon"] == horizon].dropna(subset=["stop_hit", "mfe_pts", "score"]).copy()
    print(f"  Samples: {len(sub)}")

    if len(sub) < 100:
        print(f"  Insufficient data for {horizon}m")
        return None, None

    X = prepare_features(sub)
    y_class = sub["stop_hit"].values
    y_reg = sub["mfe_pts"].values

    # TimeSeriesSplit
    tscv = TimeSeriesSplit(n_splits=5)

    # Classifier: p_stop_hit
    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        min_samples_split=20,
        min_samples_leaf=10,
        random_state=42,
        n_jobs=-1,
        class_weight="balanced"
    )

    # Regressor: expected_mfe
    reg = RandomForestRegressor(
        n_estimators=200,
        max_depth=10,
        min_samples_split=20,
        min_samples_leaf=10,
        random_state=42,
        n_jobs=-1,
    )

    # Cross-validation scores
    cv_ll = []
    cv_mse = []

    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train_c, y_test_c = y_class[train_idx], y_class[test_idx]
        y_train_r, y_test_r = y_reg[train_idx], y_reg[test_idx]

        clf.fit(X_train, y_train_c)
        reg.fit(X_train, y_train_r)

        p_pred = clf.predict_proba(X_test)[:, 1]
        p_pred = np.clip(p_pred, 1e-6, 1 - 1e-6)
        cv_ll.append(log_loss(y_test_c, p_pred))

        mfe_pred = reg.predict(X_test)
        cv_mse.append(mean_squared_error(y_test_r, mfe_pred))

    print(f"  CV LogLoss: {np.mean(cv_ll):.4f} ± {np.std(cv_ll):.4f}")
    print(f"  CV MSE (MFE): {np.mean(cv_mse):.4f} ± {np.std(cv_mse):.4f}")

    # Refit on all data
    clf.fit(X, y_class)
    reg.fit(X, y_reg)

    # Feature importance
    feat_names = ["score", "dir_enc", "horizon", "mins_since_open"]
    print("  Classifier feature importance:")
    for name, imp in zip(feat_names, clf.feature_importances_):
        print(f"    {name}: {imp:.4f}")

    # Save models
    os.makedirs(out_dir, exist_ok=True)
    clf_path = os.path.join(out_dir, f"stop_mfe_classifier_{horizon}m.pkl")
    reg_path = os.path.join(out_dir, f"stop_mfe_regressor_{horizon}m.pkl")

    joblib.dump(clf, clf_path)
    joblib.dump(reg, reg_path)
    print(f"  Saved: {clf_path}, {reg_path}")

    return clf, reg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/stop_mfe_dataset.csv")
    parser.add_argument("--horizons", default="5,15", help="Comma-separated minutes")
    parser.add_argument("--out-dir", default="models")
    args = parser.parse_args()

    print(f"Loading dataset from {args.data}...")
    df = pd.read_csv(args.data)
    print(f"  Rows: {len(df)}")

    horizons = [int(h) for h in args.horizons.split(",")]

    for h in horizons:
        train_horizon(df, h, args.out_dir)

    print("\n=== Training complete ===")


if __name__ == "__main__":
    main()