# ml/validate_65feat_walkforward.py — one-off validation
# Confirms the 65-feature conditional-direction AUC (0.676 from
# probe_features_v4) is stable across multiple chronological folds,
# not an artifact of the single 80/20 cut.
import os
import sys
import warnings
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score
warnings.filterwarnings("ignore")

from ml.probe_features_v4 import add_new_features, label_cond, PARAMS
from ml.feature_config import FEATURE_COLUMNS

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ml", "models", "training_dataset_v3.csv")

df = pd.read_csv(_DATA).dropna()
df["date"] = pd.to_datetime(df["date"], errors="coerce")
df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
print(f"loaded {len(df):,} rows")

df, new_cols = add_new_features(df)
ce, pe = label_cond(df)
touch = (ce == 1) | (pe == 1)   # ndarray from label_cond
df["y"] = ce
df = df.replace([np.inf, -np.inf], np.nan)
all_cols = list(FEATURE_COLUMNS) + new_cols
df[all_cols] = df[all_cols].fillna(0.0)

print(f"features: baseline={len(FEATURE_COLUMNS)} new={len(new_cols)} combined={len(all_cols)}")

# 4 chronological folds: train = [start, cut_k), test = [cut_k, cut_{k+1})
cuts = [int(len(df) * f) for f in (0.60, 0.70, 0.80, 0.90)]
results = []
for i, te_end in enumerate(cuts):
    tr_start = 0
    te_start = cuts[i - 1] if i > 0 else 0
    tr_m = touch[tr_start:te_start]
    te_m = touch[te_start:te_end]
    if tr_m.sum() < 2000 or te_m.sum() < 2000:
        continue
    tr = df.iloc[tr_start:te_start][tr_m]
    te = df.iloc[te_start:te_end][te_m]
    ytr, yte = tr["y"].values, te["y"].values
    m = lgb.LGBMClassifier(**PARAMS)
    m.fit(tr[all_cols], ytr)
    p = m.predict_proba(te[all_cols])[:, 1]
    auc = roc_auc_score(yte, p)
    results.append((auc, len(tr), len(te),
                    str(tr["date"].min().date()), str(te["date"].min().date()), str(te["date"].max().date())))
    print(f"fold {i}: train {len(tr):,} -> test {len(te):,} "
          f"({results[-1][4]}..{results[-1][5]})  AUC={auc:.4f}")

aucs = [r[0] for r in results]
print(f"\nmean AUC = {np.mean(aucs):.4f}  min={min(aucs):.4f}  max={max(aucs):.4f}")
print("STABLE ENOUGH" if min(aucs) >= 0.55 else "NOT STABLE — window-dependent")