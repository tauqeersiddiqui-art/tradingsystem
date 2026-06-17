# ml/trainer_v2_champion.py
# FIXED - audit v2
#
# Changes from original:
#   FIX-1 : FEATURE_COLUMNS imported from updated feature_config (28 features,
#            vix_regime removed, moneyness + time_to_expiry_min added)
#   FIX-2 : Added expectancy validation metric alongside AUC
#            A model with good AUC but negative expectancy is worthless for trading
#   FIX-3 : Added training guard - aborts if label rate < 15% or > 65%
#   FIX-4 : Holdout calibration now uses TimeSeriesSplit instead of fixed 80/20 split
#   FIX-5 : threshold finder uses expectancy-weighted score, not precision x sqrt(n)
#            Previously optimised for precision alone, ignoring win size vs loss size

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import joblib
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import precision_score, roc_auc_score
import warnings
warnings.filterwarnings("ignore")

from ml.feature_config import FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM

DATA_PATH  = "ml/models/training_dataset_v2.csv"  # direction-filtered dataset
MODEL_DIR  = "ml/models"

TARGET_WIN_RATE  = 0.52
MIN_TRADES       = 30
LOSS_COST_WEIGHT = 3.0


def load_data():
    print("[DATA] Loading dataset...")
    df = pd.read_csv(DATA_PATH).dropna()
    df = df.sort_values("timestamp") if "timestamp" in df.columns else df
    print(f"[DATA] Rows: {len(df):,}")
    return df


def get_features(df):
    available = [f for f in FEATURE_COLUMNS if f in df.columns]
    missing   = [f for f in FEATURE_COLUMNS if f not in df.columns]
    if missing:
        print(f"[WARN] Missing features (will use 0): {missing}")
        for m in missing:
            df[m] = 0.0
    print(f"[DATA] Using {len(available)} features")
    return FEATURE_COLUMNS, df


def make_sample_weights(y, timestamps=None, recency_multiplier=3.0, recency_cutoff='2024-01-01'):
    """
    Base weights: winning labels get LOSS_COST_WEIGHT (unchanged).
    Recency: rows from recency_cutoff onward get recency_multiplier applied.
    This recalibrates CE model toward 2024-2026 market regime where NIFTY
    has been declining — CE signals trained on 2020-2022 bull-market patterns
    are misfiring in the current environment (live CE WR=14% vs in-sample 80%).
    """
    w = np.ones(len(y))
    w[y == 1] = LOSS_COST_WEIGHT
    if timestamps is not None:
        cutoff = pd.Timestamp(recency_cutoff)
        recent_mask = pd.to_datetime(timestamps) >= cutoff
        w[recent_mask] *= recency_multiplier
        n_recent = recent_mask.sum()
        print(f"  [RECENCY] {n_recent:,} rows >= {recency_cutoff} weighted x{recency_multiplier}")
    return w


def check_calibration(model, X_df, label):
    probs = model.predict_proba(X_df)[:, 1]
    std   = probs.std()
    print(f"  [{label}] Prob range: {probs.min():.3f}-{probs.max():.3f}  std={std:.4f}", end="  ")
    print("OK  healthy spread" if std >= 0.05 else "WARN  narrow - features may lack signal")
    return std


def compute_expectancy(model, X_df, y, threshold=0.35, avg_win=500, avg_loss=300):
    """
    FIX-2: Trading-aligned metric.
    Expectancy = win_rate x avg_win - loss_rate x avg_loss
    Only trades above `threshold` are counted.
    """
    probs = model.predict_proba(X_df)[:, 1]
    mask  = probs >= threshold
    if mask.sum() < MIN_TRADES:
        return -9999
    actuals = y[mask]
    win_rate = actuals.mean()
    exp = win_rate * avg_win - (1 - win_rate) * avg_loss
    return round(exp, 2)


def find_threshold(model, X_df, y):
    """
    FIX-5: Find threshold that maximises expectancy, not precision x sqrt(n).
    """
    probs = model.predict_proba(X_df)[:, 1]
    best  = {"threshold": 0.35, "precision": 0.0, "trades": 0, "expectancy": -9999}
    for t in np.arange(0.10, 0.65, 0.01):
        preds = (probs >= t).astype(int)
        n     = preds.sum()
        if n < MIN_TRADES or n > len(y) * 0.30:
            continue
        prec = precision_score(y, preds, zero_division=0)
        if prec >= TARGET_WIN_RATE:
            # FIX-5: expectancy score
            exp   = compute_expectancy(model, X_df, y, threshold=t)
            score = exp * np.log1p(n)   # reward expectancy, penalise very low trade count
            if score > best.get("score", -9999):
                best = {
                    "threshold":   round(t, 2),
                    "precision":   round(prec, 3),
                    "trades":      int(n),
                    "expectancy":  exp,
                    "score":       score,
                }
    return best


def validate_label_balance(df, label_col):
    """FIX-3: abort if label balance is degenerate."""
    rate = df[label_col].mean()
    if rate < 0.15:
        raise ValueError(
            f"{label_col} rate={rate:.1%} < 15% - too imbalanced. "
            "Loosen TARGET_MOVE in relabel_dual_champion.py or ATR_MULTIPLIER in dataset_builder.py"
        )
    if rate > 0.65:
        raise ValueError(
            f"{label_col} rate={rate:.1%} > 65% - too loose. "
            "Tighten TARGET_MOVE or ATR_MULTIPLIER."
        )
    print(f"  [CHECK] {label_col} rate={rate:.1%} - OK")


def train_direction(df, features, label_col, model_name, use_recency_weight=False):
    print(f"\n{'-'*56}")
    print(f"  Training: {model_name}  |  Label: {label_col}")
    print(f"{'-'*56}")

    validate_label_balance(df, label_col)   # FIX-3

    X = df[features].values
    y = df[label_col].values
    # Timestamps for recency weighting — use 'timestamp' or 'date' column
    ts_col = 'timestamp' if 'timestamp' in df.columns else ('date' if 'date' in df.columns else None)
    timestamps = df[ts_col].values if (ts_col and use_recency_weight) else None
    print(f"  Positive rate: {y.mean():.1%}  |  Samples: {len(y):,}  |  recency_weight={use_recency_weight}")

    params = dict(
        n_estimators=500, learning_rate=0.02, max_depth=6, num_leaves=31,
        subsample=0.75, colsample_bytree=0.75, min_child_samples=40,
        reg_alpha=0.05, reg_lambda=0.1,
        scale_pos_weight=LOSS_COST_WEIGHT,
        verbose=-1, random_state=42, n_jobs=-1,
    )

    # Walk-forward CV
    tscv     = TimeSeriesSplit(n_splits=5)
    fold_aucs = []
    fold_exps = []
    print("  Walk-forward CV:")
    for fold, (tr_idx, va_idx) in enumerate(tscv.split(X)):
        m = lgb.LGBMClassifier(**params)
        ts_fold = timestamps[tr_idx] if timestamps is not None else None
        m.fit(X[tr_idx], y[tr_idx], sample_weight=make_sample_weights(y[tr_idx], ts_fold))
        if y[va_idx].sum() > 0:
            auc = roc_auc_score(y[va_idx], m.predict_proba(X[va_idx])[:, 1])
            X_va_df = pd.DataFrame(X[va_idx], columns=features)
            exp = compute_expectancy(m, X_va_df, y[va_idx])
        else:
            auc = 0.5
            exp = 0
        fold_aucs.append(auc)
        fold_exps.append(exp)
        print(f"    Fold {fold+1}: AUC={auc:.3f}  Expectancy=Rs{exp}")
    print(f"  Mean AUC: {np.mean(fold_aucs):.3f}  Mean Expectancy: Rs{np.mean(fold_exps):.0f}")

    # Final model
    print("  Training final model on full dataset...")
    X_df = pd.DataFrame(X, columns=features)
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X_df, y, sample_weight=make_sample_weights(y, timestamps))

    # FIX-4: calibration on rolling holdout (TimeSeriesSplit last fold)
    tscv_cal = TimeSeriesSplit(n_splits=5)
    *_, (_, hold_idx) = tscv_cal.split(X)
    X_hold_df = pd.DataFrame(X[hold_idx], columns=features)
    y_hold    = y[hold_idx]
    print(f"  Calibrating on holdout ({len(y_hold)} rows - last time-series fold)...")
    calibrated = CalibratedLGBM(final_model)
    calibrated.fit_calibration(X_hold_df, y_hold)
    calibrated.feature_names_ = features

    check_calibration(calibrated, X_hold_df, model_name)

    # Threshold on recent 30%
    split30 = int(len(X) * 0.70)
    X30_df  = pd.DataFrame(X[split30:], columns=features)
    thresh_info = find_threshold(calibrated, X30_df, y[split30:])

    # Note: PE threshold bump removed — PE is the profitable side (PF=2.17 live)
    # and artificial restriction reduces its edge. Threshold is now purely data-driven.

    print(f"  Optimal threshold: {thresh_info}")

    # FIX-2: print expectancy
    exp_val = compute_expectancy(calibrated, X30_df, y[split30:],
                                 threshold=thresh_info["threshold"])
    print(f"  Expected PnL/trade at threshold: Rs{exp_val}")

    # Feature importance
    importances = final_model.feature_importances_
    feat_imp    = sorted(zip(features, importances), key=lambda x: x[1], reverse=True)
    print("  Top 10 features:")
    for fname, imp in feat_imp[:10]:
        bar = "#" * int(imp / max(importances) * 20)
        print(f"    {fname:<30} {bar} ({imp:.0f})")

    # Save
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, model_name)
    joblib.dump(calibrated, model_path)
    with open(model_path.replace(".pkl", "_threshold.txt"), "w") as f:
        f.write(str(thresh_info["threshold"]))
    with open(model_path.replace(".pkl", "_features.txt"), "w") as f:
        f.write("\n".join(features))
    print(f"  [SAVED] {model_path}  threshold={thresh_info['threshold']}")

    return {
        "model":       calibrated,
        "threshold":   thresh_info["threshold"],
        "mean_auc":    np.mean(fold_aucs),
        "expectancy":  exp_val,
    }


def main():
    print("=" * 60)
    print("  CHAMPION ML TRAINER - Institutional v3")
    print("=" * 60)

    df           = load_data()
    features, df = get_features(df)

    # Direction-filtered training: CE model only sees bullish-market rows,
    # PE model only sees bearish-market rows.  This prevents the model from
    # learning to predict direction (the Supertrend+VWAP filter handles that)
    # and instead focuses it on ENTRY TIMING within the right market regime.
    if "ce_eligible" in df.columns and "pe_eligible" in df.columns:
        df_ce = df[df["ce_eligible"] == True].copy()
        df_pe = df[df["pe_eligible"] == True].copy()
        print(f"[FILTER] CE training rows: {len(df_ce):,}  PE training rows: {len(df_pe):,}")
    else:
        print("[WARN] ce_eligible/pe_eligible columns missing - using all rows. "
              "Run dataset_builder_v2.py first.")
        df_ce = df
        df_pe = df

    # CE: recency-weighted (3x for 2024-2026) to recalibrate toward current declining market
    # PE: no recency weighting — PE edge is stable across regimes (PF=2.17 live)
    ce = train_direction(df_ce, features, "label_ce", "champion_ce_lgbm.pkl", use_recency_weight=True)
    pe = train_direction(df_pe, features, "label_pe", "champion_pe_lgbm.pkl", use_recency_weight=False)

    print(f"\n{'='*60}")
    print(f"  CE: AUC={ce['mean_auc']:.3f}  Threshold={ce['threshold']}  Expectancy=Rs{ce['expectancy']}")
    print(f"  PE: AUC={pe['mean_auc']:.3f}  Threshold={pe['threshold']}  Expectancy=Rs{pe['expectancy']}")

    if ce["mean_auc"] < 0.54 or pe["mean_auc"] < 0.54:
        print("  [WARN] AUC < 0.54 - limited predictive power.")
    else:
        print("  [OK] Models are production-ready.")

    if ce["expectancy"] < 0 or pe["expectancy"] < 0:
        print("  [WARN] Negative expectancy - tighten threshold or retrain with more data.")
    print("=" * 60)


if __name__ == "__main__":
    main()
