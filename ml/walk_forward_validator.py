"""
ml/walk_forward_validator.py
Out-of-sample validation for LightGBM CE/PE models.

Usage:
    python -m ml.walk_forward_validator

AUC < 0.55 = model has no real edge -> retrain.
AUC > 0.58 = acceptable -> deploy.
"""

import pandas as pd
import numpy as np
import lightgbm as lgb
from pathlib import Path
from sklearn.metrics import roc_auc_score, precision_score
from ml.feature_config import FEATURE_COLUMNS

DATA_PATH = Path(__file__).resolve().parent / "models" / "training_dataset_trade.csv"
N_SPLITS  = 5


def walk_forward_validate(label_col: str) -> pd.DataFrame:
    df    = pd.read_csv(DATA_PATH).dropna()
    feats = [f for f in FEATURE_COLUMNS if f in df.columns]
    miss  = set(FEATURE_COLUMNS) - set(feats)
    if miss:
        print(f"  [WARN] Features missing from dataset (will skip): {miss}")

    n         = len(df)
    fold_size = n // (N_SPLITS + 1)
    results   = []

    print(f"\n{'='*60}")
    print(f"  Walk-forward  |  label={label_col}  |  folds={N_SPLITS}")
    print(f"{'='*60}")

    for i in range(N_SPLITS):
        train_end   = fold_size * (i + 1)
        test_end    = min(fold_size * (i + 2), n)
        train_start = max(0, train_end - fold_size * 6)

        X_tr = df.iloc[train_start:train_end][feats]
        y_tr = df.iloc[train_start:train_end][label_col]
        X_te = df.iloc[train_end:test_end][feats]
        y_te = df.iloc[train_end:test_end][label_col]

        if y_te.nunique() < 2:
            print(f"  Fold {i+1}: skipped -- single class in test set")
            continue

        model = lgb.LGBMClassifier(
            n_estimators=300, learning_rate=0.03, max_depth=5,
            num_leaves=20, class_weight="balanced",
            reg_alpha=0.1, reg_lambda=0.1,
            verbose=-1, random_state=42,
        )
        model.fit(X_tr, y_tr)
        probs = model.predict_proba(X_te)[:, 1]

        auc      = roc_auc_score(y_te, probs)
        prec_30  = precision_score(y_te, probs >= 0.30, zero_division=0)
        sig_rate = float((probs >= 0.30).mean())
        p90      = float(np.percentile(probs, 90))
        grade    = "GOOD" if auc >= 0.58 else ("WEAK" if auc >= 0.52 else "NO EDGE")

        print(
            f"  Fold {i+1}: AUC={auc:.4f} [{grade}] | "
            f"P@0.30={prec_30:.3f} | Signal%={sig_rate:.1%} | P90={p90:.3f}"
        )
        results.append(dict(fold=i+1, label=label_col, auc=round(auc, 4),
                            precision_30=round(prec_30, 4), signal_rate=round(sig_rate, 4)))

    df_r = pd.DataFrame(results)
    if not df_r.empty:
        avg = df_r["auc"].mean()
        print(f"\n  Avg AUC: {avg:.4f}  {'-> has edge' if avg >= 0.56 else '-> retrain needed'}")
    return df_r


if __name__ == "__main__":
    walk_forward_validate("label_ce")
    walk_forward_validate("label_pe")
