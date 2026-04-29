# ml/feedback_trainer.py
# FIXED — audit v2
#
# Changes from original:
#   FIX-1 : FEATURE_COLUMNS imported from updated feature_config
#            (vix_regime removed, moneyness + time_to_expiry_min added)
#   FIX-2 : outcome threshold updated — ₹130 (2× brokerage) rather than 65×2
#            A trade only "wins" if it covers brokerage on both sides
#   No logic changes — just alignment with new feature set

import os
import csv
import argparse
import pandas as pd
import joblib
import lightgbm as lgb
from datetime import datetime
from pathlib import Path
from ml.feature_config import FEATURE_COLUMNS

_ROOT         = Path(__file__).resolve().parent.parent
FEEDBACK_PATH = str(_ROOT / "data" / "ml_feedback.csv")
HIST_PATH     = str(_ROOT / "ml" / "models" / "training_dataset_trade.csv")
FIELDNAMES    = FEATURE_COLUMNS + ["direction", "outcome", "pnl", "reason", "date"]

BROKERAGE_ROUND_TRIP = 65 * 2   # FIX-2


def log_trade_outcome(features: dict, direction: str, pnl: float,
                      reason: str, entry_time: datetime):
    # ===== FIX: skip corrupt feature rows =====
    ema20 = features.get("ema20", 0)
    atr   = features.get("atr", 0)

    if ema20 <= 0 or atr <= 0:
        print(f"[FEEDBACK SKIP] bad row ema20={ema20}, atr={atr}")
        return
    """Call after every trade exit in live_engine_v2.py."""
    outcome     = 1 if pnl > BROKERAGE_ROUND_TRIP else 0   # FIX-2
    file_exists = os.path.isfile(FEEDBACK_PATH)
    os.makedirs(os.path.dirname(FEEDBACK_PATH), exist_ok=True)

    row = {f: features.get(f, 0) for f in FEATURE_COLUMNS}
    row.update(direction=direction, outcome=outcome,
               pnl=round(pnl, 2), reason=reason,
               date=entry_time.strftime("%Y-%m-%d"))

    with open(FEEDBACK_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def retrain_with_feedback(live_weight: int = 3):
    """Merges historical + live feedback and retrains both models."""
    hist  = pd.read_csv(HIST_PATH).dropna()
    feats = [f for f in FEATURE_COLUMNS if f in hist.columns]

    # Pad any missing new features with 0
    for f in FEATURE_COLUMNS:
        if f not in hist.columns:
            hist[f] = 0.0

    try:
        live = pd.read_csv(FEEDBACK_PATH).dropna()
        if len(live) > 0:
            live["label_ce"] = live.apply(
                lambda r: r["outcome"] if r["direction"] == "CE" else 0, axis=1)
            live["label_pe"] = live.apply(
                lambda r: r["outcome"] if r["direction"] == "PE" else 0, axis=1)
            combined = pd.concat([hist] + [live] * live_weight, ignore_index=True)
            print(f"  Live feedback rows: {len(live)} (weighted ×{live_weight})")
        else:
            combined = hist
    except FileNotFoundError:
        combined = hist
        print("  No feedback file yet — training on historical data only")

    combined = combined.dropna()
    models_dir = str(_ROOT / "ml" / "models")
    os.makedirs(models_dir, exist_ok=True)

    for label_col, out_name in [("label_ce", "champion_ce_lgbm"),
                                 ("label_pe", "champion_pe_lgbm")]:
        if label_col not in combined.columns:
            print(f"  Skipping {label_col} — column missing")
            continue
        X = combined[[f for f in FEATURE_COLUMNS if f in combined.columns]]
        y = combined[label_col]
        model = lgb.LGBMClassifier(
            n_estimators=300, learning_rate=0.03, max_depth=5,
            num_leaves=20, class_weight="balanced",
            reg_alpha=0.1, reg_lambda=0.1,
            verbose=-1, random_state=42,
        )
        model.fit(X, y)
        path = os.path.join(models_dir, f"{out_name}.pkl")
        joblib.dump(model, path)
        print(f"  Saved {path}  (samples: {len(combined)})")


def train_regime_models():
    """Trains separate CE/PE models per regime."""
    df    = pd.read_csv(HIST_PATH).dropna()
    feats = [f for f in FEATURE_COLUMNS if f in df.columns]
    models_dir = str(_ROOT / "ml" / "models")
    os.makedirs(models_dir, exist_ok=True)

    for regime in ["TREND", "RANGE", "EXPANSION"]:
        sub = df[df["regime"] == regime] if "regime" in df.columns else pd.DataFrame()
        if len(sub) < 500:
            print(f"  {regime}: {len(sub)} samples — skipping (need 500+)")
            continue
        for lc in ["label_ce", "label_pe"]:
            if lc not in sub.columns:
                continue
            direction = lc.split("_")[1]
            model = lgb.LGBMClassifier(
                n_estimators=250, learning_rate=0.03,
                class_weight="balanced", verbose=-1, random_state=42,
            )
            model.fit(sub[feats], sub[lc])
            out = os.path.join(models_dir,
                               f"champion_{direction}_{regime.lower()}_lgbm.pkl")
            joblib.dump(model, out)
            print(f"  {regime} {lc} -> {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrain",       action="store_true")
    parser.add_argument("--regime-models", action="store_true")
    args = parser.parse_args()
    if args.retrain:
        print("Retraining with live feedback...")
        retrain_with_feedback()
    if args.regime_models:
        print("Training regime-aware models...")
        train_regime_models()
    if not args.retrain and not args.regime_models:
        parser.print_help()
