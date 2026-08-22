# ml/trainer.py
# De-saturated, calibrated DIRECTIONAL trainer with a deploy gate.
#
# FIXES vs trainer_v2_champion.py:
#   BUG-FIX (saturation): v2 weighted positives TWICE — scale_pos_weight=3 AND
#     sample_weight[y==1]=3 — pinning CE probabilities high (0.8-0.96 all day).
#     v3 uses NO class over-weighting; probabilities stay calibrated/honest.
#   Recency weighting is kept (regime relevance) but applied by DATE only,
#     never by label, so it cannot bias direction.
#   Adds a DEPLOY GATE: new models overwrite the champions ONLY if
#     walk-forward AUC >= MIN_AUC, calibration std >= MIN_STD, and holdout
#     expectancy > 0. Otherwise they are written as *_candidate.pkl and the
#     live champions are left untouched.
#   Backs up existing champions to ml/models/backup_<ts>/ before any overwrite.
#
# RUN:  python ml/trainer.py   (after dataset_builder.py)

import sys, os, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import warnings
warnings.filterwarnings("ignore")

# CatBoost support (optional -- skip gracefully if not installed)
try:
    from catboost import CatBoostClassifier
    _CATBOOST_AVAILABLE = True
except ImportError:
    _CATBOOST_AVAILABLE = False

from ml.feature_config import FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM

DATA_PATH = "ml/models/training_dataset.csv"
MODEL_DIR = "ml/models"

# ── Deploy gate thresholds ────────────────────────────────────────────
MIN_AUC = 0.55       # below this the model has no real edge
MIN_STD = 0.05       # calibrated-prob spread; below = saturated/no signal
RECENCY_CUTOFF = "2024-01-01"
RECENCY_MULT   = 3.0


def recency_weights(timestamps):
    """Recency only — NEVER by label (that was the v2 saturation bug)."""
    w = np.ones(len(timestamps))
    if timestamps is not None:
        recent = pd.to_datetime(timestamps) >= pd.Timestamp(RECENCY_CUTOFF)
        w[recent] *= RECENCY_MULT
    return w


def expectancy(probs, y, threshold, avg_win=500, avg_loss=300, min_trades=30):
    mask = probs >= threshold
    if mask.sum() < min_trades:
        return -9999.0
    wr = y[mask].mean()
    return round(wr * avg_win - (1 - wr) * avg_loss, 1)


def find_threshold(probs, y):
    best = {"threshold": 0.50, "expectancy": -9999.0, "wr": 0.0, "trades": 0}
    for t in np.arange(0.40, 0.80, 0.01):
        n = int((probs >= t).sum())
        if n < 30 or n > len(y) * 0.5:
            continue
        e = expectancy(probs, y, t)
        if e > best["expectancy"]:
            wr = y[probs >= t].mean()
            best = {"threshold": round(float(t), 2), "expectancy": e,
                    "wr": round(float(wr), 3), "trades": n}
    return best


def train_one(df, label_col, name):
    print(f"\n{'-'*60}\n  {name}  (label={label_col})\n{'-'*60}")
    X = df[FEATURE_COLUMNS].values
    y = df[label_col].values.astype(int)
    ts = df["date"].values if "date" in df.columns else None
    print(f"  samples={len(y):,}  positive_rate={y.mean():.1%}")

    params = dict(
        n_estimators=500, learning_rate=0.02, max_depth=6, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
        reg_alpha=0.05, reg_lambda=0.1,
        verbose=-1, random_state=42, n_jobs=-1,
        # NOTE: no scale_pos_weight — calibrated probabilities, not biased.
    )

    tscv = TimeSeriesSplit(n_splits=5)
    aucs = []
    for k, (tr, va) in enumerate(tscv.split(X)):
        m = lgb.LGBMClassifier(**params)
        m.fit(X[tr], y[tr], sample_weight=recency_weights(ts[tr] if ts is not None else None))
        if y[va].sum() > 0:
            a = roc_auc_score(y[va], m.predict_proba(X[va])[:, 1])
        else:
            a = 0.5
        aucs.append(a)
        print(f"    fold {k+1}: AUC={a:.3f}")
    mean_auc = float(np.mean(aucs))
    print(f"  mean AUC = {mean_auc:.3f}")

    # Final model + Platt calibration on the last time-series fold
    final = lgb.LGBMClassifier(**params)
    final.fit(pd.DataFrame(X, columns=FEATURE_COLUMNS), y,
              sample_weight=recency_weights(ts))
    *_, (_, hold) = TimeSeriesSplit(n_splits=5).split(X)
    cal = CalibratedLGBM(final)
    cal.fit_calibration(pd.DataFrame(X[hold], columns=FEATURE_COLUMNS), y[hold])
    cal.feature_names_ = list(FEATURE_COLUMNS)

    probs = cal.predict_proba(pd.DataFrame(X[hold], columns=FEATURE_COLUMNS))[:, 1]
    std = float(probs.std())
    thr = find_threshold(probs, y[hold])
    print(f"  calibrated prob: min={probs.min():.3f} max={probs.max():.3f} "
          f"mean={probs.mean():.3f} std={std:.3f}")
    print(f"  best threshold: {thr}")

    return {"model": cal, "auc": mean_auc, "std": std,
            "threshold": thr["threshold"], "expectancy": thr["expectancy"],
            "wr": thr["wr"], "trades": thr["trades"]}


def train_one_cat(df, label_col, name):
    """Train + Platt-calibrate one CatBoost directional model."""
    print(f"\n{'-'*60}\n  {name} [CatBoost]  (label={label_col})\n{'-'*60}")
    X   = df[FEATURE_COLUMNS].values
    Xdf = pd.DataFrame(X, columns=FEATURE_COLUMNS)
    y   = df[label_col].values.astype(int)
    ts  = df["date"].values if "date" in df.columns else None
    print(f"  samples={len(y):,}  positive_rate={y.mean():.1%}")

    cat_params = dict(
        iterations=500, learning_rate=0.03, depth=6,
        l2_leaf_reg=3.0, random_seed=42, verbose=0, thread_count=-1,
    )

    tscv = TimeSeriesSplit(n_splits=5)
    aucs = []
    for k, (tr, va) in enumerate(tscv.split(X)):
        m = CatBoostClassifier(**cat_params)
        m.fit(Xdf.iloc[tr], y[tr],
              sample_weight=recency_weights(ts[tr] if ts is not None else None))
        a = roc_auc_score(y[va], m.predict_proba(Xdf.iloc[va])[:, 1]) \
            if y[va].sum() > 0 else 0.5
        aucs.append(a)
        print(f"    fold {k+1}: AUC={a:.3f}")
    mean_auc = float(np.mean(aucs))
    print(f"  mean AUC = {mean_auc:.3f}")

    # Final model + Platt calibration on last time-series holdout
    final = CatBoostClassifier(**cat_params)
    final.fit(Xdf, y, sample_weight=recency_weights(ts))
    *_, (_, hold) = TimeSeriesSplit(n_splits=5).split(X)
    cal = CalibratedLGBM(final)          # wrapper works for any sklearn-compatible model
    cal.fit_calibration(Xdf.iloc[hold], y[hold])
    cal.feature_names_ = list(FEATURE_COLUMNS)

    probs = cal.predict_proba(Xdf.iloc[hold])[:, 1]
    std   = float(probs.std())
    thr   = find_threshold(probs, y[hold])
    print(f"  calibrated prob: min={probs.min():.3f} max={probs.max():.3f} "
          f"mean={probs.mean():.3f} std={std:.3f}")
    print(f"  best threshold: {thr}")

    return {"model": cal, "auc": mean_auc, "std": std,
            "threshold": thr["threshold"], "expectancy": thr["expectancy"],
            "wr": thr["wr"], "trades": thr["trades"]}


def backup_existing():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(MODEL_DIR, f"backup_{stamp}")
    saved = []
    for fn in ("champion_ce_lgbm.pkl", "champion_pe_lgbm.pkl",
               "champion_ce_lgbm_threshold.txt", "champion_pe_lgbm_threshold.txt",
               "champion_ce_cat.pkl", "champion_pe_cat.pkl"):
        src = os.path.join(MODEL_DIR, fn)
        if os.path.exists(src):
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(src, os.path.join(bdir, fn))
            saved.append(fn)
    if saved:
        print(f"\n[BACKUP] {len(saved)} files -> {bdir}")
    return bdir


def deploy(res, name):
    model_path = os.path.join(MODEL_DIR, f"{name}.pkl")
    joblib.dump(res["model"], model_path)
    with open(model_path.replace(".pkl", "_threshold.txt"), "w") as f:
        f.write(str(res["threshold"]))
    with open(model_path.replace(".pkl", "_features.txt"), "w") as f:
        f.write("\n".join(FEATURE_COLUMNS))
    print(f"  [DEPLOYED] {model_path}  threshold={res['threshold']}")


def candidate(res, name):
    p = os.path.join(MODEL_DIR, f"{name}_candidate.pkl")
    joblib.dump(res["model"], p)
    print(f"  [CANDIDATE ONLY] {p}  (NOT deployed — failed gate)")


def main():
    print("=" * 64)
    print("  DIRECTIONAL TRAINER v3  (de-saturated, deploy-gated)")
    print("=" * 64)
    df = pd.read_csv(DATA_PATH).dropna()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0

    # ── LightGBM ──────────────────────────────────────────────────────
    ce = train_one(df, "label_ce", "champion_ce_lgbm")
    pe = train_one(df, "label_pe", "champion_pe_lgbm")

    print(f"\n{'='*64}")
    print(f"  LGBM  CE: AUC={ce['auc']:.3f} std={ce['std']:.3f} thr={ce['threshold']} "
          f"WR={ce['wr']:.1%} exp=Rs{ce['expectancy']}")
    print(f"  LGBM  PE: AUC={pe['auc']:.3f} std={pe['std']:.3f} thr={pe['threshold']} "
          f"WR={pe['wr']:.1%} exp=Rs{pe['expectancy']}")

    def passes(r):
        return r["auc"] >= MIN_AUC and r["std"] >= MIN_STD and r["expectancy"] > 0

    ce_ok, pe_ok = passes(ce), passes(pe)
    print(f"\n  LGBM GATE  CE: {'PASS' if ce_ok else 'FAIL'}   PE: {'PASS' if pe_ok else 'FAIL'}")
    print(f"  (need AUC>={MIN_AUC}, std>={MIN_STD}, expectancy>0)")

    if ce_ok and pe_ok:
        backup_existing()
        deploy(ce, "champion_ce_lgbm")
        deploy(pe, "champion_pe_lgbm")
        print("\n  [LGBM RESULT] New LightGBM models DEPLOYED.")
    else:
        candidate(ce, "champion_ce_lgbm")
        candidate(pe, "champion_pe_lgbm")
        print("\n  [LGBM RESULT] Gate FAILED — LGBM champions left UNCHANGED.")
        print("                Inspect candidates / adjust TARGET or START_DATE and rerun.")

    # ── CatBoost (optional — skipped if library not installed) ────────
    print(f"\n{'='*64}")
    if not _CATBOOST_AVAILABLE:
        print("  [CATBOOST] Library not installed — skipping.")
        print("             Install with:  pip install catboost")
    else:
        ce_cat = train_one_cat(df, "label_ce", "champion_ce_cat")
        pe_cat = train_one_cat(df, "label_pe", "champion_pe_cat")

        print(f"\n{'='*64}")
        print(f"  CAT   CE: AUC={ce_cat['auc']:.3f} std={ce_cat['std']:.3f} "
              f"thr={ce_cat['threshold']} WR={ce_cat['wr']:.1%} exp=Rs{ce_cat['expectancy']}")
        print(f"  CAT   PE: AUC={pe_cat['auc']:.3f} std={pe_cat['std']:.3f} "
              f"thr={pe_cat['threshold']} WR={pe_cat['wr']:.1%} exp=Rs{pe_cat['expectancy']}")

        ce_cat_ok, pe_cat_ok = passes(ce_cat), passes(pe_cat)
        print(f"\n  CAT  GATE  CE: {'PASS' if ce_cat_ok else 'FAIL'}   "
              f"PE: {'PASS' if pe_cat_ok else 'FAIL'}")

        if ce_cat_ok and pe_cat_ok:
            deploy(ce_cat, "champion_ce_cat")
            deploy(pe_cat, "champion_pe_cat")
            print("\n  [CAT RESULT] CatBoost models DEPLOYED. "
                  "Predictor will use LGBM+CAT_ENSEMBLE.")
        else:
            candidate(ce_cat, "champion_ce_cat")
            candidate(pe_cat, "champion_pe_cat")
            print("\n  [CAT RESULT] Gate FAILED — CatBoost candidates saved, "
                  "LGBM-only mode unchanged.")

    print("=" * 64)


if __name__ == "__main__":
    main()
