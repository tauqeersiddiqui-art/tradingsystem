# ml/trainer_v3.py
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
# RUN:  python ml/trainer_v3.py   (after dataset_builder_v3.py)

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

from ml.feature_config import ALL_FEATURE_COLUMNS as FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM

DATA_PATH = "ml/models/training_dataset_v3.csv"
MODEL_DIR = "ml/models"

# ── Deploy gate thresholds ────────────────────────────────────────────
MIN_AUC = 0.55       # below this the model has no real edge
MIN_STD = 0.05       # calibrated-prob spread; below = saturated/no signal
RECENCY_CUTOFF = "2024-01-01"
RECENCY_MULT   = 3.0

# ── Calibration / threshold holdout ───────────────────────────────────
# CRITICAL (2026-09-06): the last CAL_FRAC of rows (chronological) are held
# OUT of the final fit and used ONLY for Platt calibration, threshold search,
# and the deploy-gate AUC.
#
# The bug this replaces: the final booster was fit on ALL rows, then
# `*_, (_, hold) = TimeSeriesSplit(5).split(X)` picked the last fold — rows the
# booster had already memorised — and Platt scaling AND find_threshold() ran on
# those in-sample predictions. Consequence, measured on a clean temporal split
# (train < 2025-07-01, test >= 2025-07-01) of training_dataset_v3.csv:
#     shipped LGBM alone      AUC 0.4959   <- below chance
#     shipped CatBoost alone  AUC 0.5281
#     shipped ensemble        AUC 0.5235
#     fresh retrain, same pre-cutoff data AUC 0.6361
# The models were not weak; the procedure was measuring memorisation, so the
# deploy gate passed models with no out-of-sample edge and handed the live
# engine calibrated-looking probabilities that meant nothing.
CAL_FRAC = float(os.getenv("V3_CAL_FRAC", "0.20"))


# ── Disentangled composition wrapper ──────────────────────────────────
class DisentangledModel:
    """
    Composes an ACTIVITY head (P(touch)) with a DIRECTION head (P(up | touch))
    into a single predictor-compatible model:

        p_CE = P(touch) * P(up | touch)
        p_PE = P(touch) * (1 - P(up | touch))

    Drop-in for the live ChampionPredictor: exposes .predict_proba(X),
    .predict(X), .feature_names_, and .base_model (used for raw-prob logging).
    """

    def __init__(self, activity, direction, side):
        self.activity       = activity
        self.direction      = direction
        self.side           = side
        self.feature_names_ = list(FEATURE_COLUMNS)
        self.base_model     = activity.base_model

    def predict_proba(self, X):
        p_touch = np.clip(self.activity.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        p_dir   = np.clip(self.direction.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        if self.side == "CE":
            p = p_touch * p_dir
        else:
            p = p_touch * (1.0 - p_dir)
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return np.column_stack([1.0 - p, p])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


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


def holdout_split(n):
    """
    Chronological (fit_idx, hold_idx). Rows are already date-sorted in main().
    The holdout is a genuine future window: no fit row is ever scored.
    """
    cut = int(n * (1.0 - CAL_FRAC))
    cut = max(min(cut, n - 1), 1)
    return np.arange(cut), np.arange(cut, n)


def train_one(df, label_col, name):
    print(f"\n{'-'*60}\n  {name}  (label={label_col})\n{'-'*60}")
    X = df[FEATURE_COLUMNS].values
    y = df[label_col].values.astype(int)
    ts = df["date"].values if "date" in df.columns else None
    print(f"  samples={len(y):,}  positive_rate={y.mean():.1%}")

    # NOTE: params mirror probe_features_v4.PARAMS — the configuration that
    # measured 65-feature conditional-direction AUC 0.676 OOS (vs 0.51 on
    # the base 35).  The previous weaker params (depth=6/leaves=31/lr=0.02)
    # left the direction head at 0.51 even with the new features.
    params = dict(
        n_estimators=500, learning_rate=0.03, max_depth=7, num_leaves=63,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=50,
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

    # ── Final model: fit on the EARLY portion only ─────────────────────
    fit_idx, hold = holdout_split(len(y))
    Xdf = pd.DataFrame(X, columns=FEATURE_COLUMNS)
    print(f"  holdout: fit={len(fit_idx):,} rows | hold={len(hold):,} rows "
          f"({CAL_FRAC:.0%}, out-of-sample)")

    final = lgb.LGBMClassifier(**params)
    final.fit(Xdf.iloc[fit_idx], y[fit_idx],
              sample_weight=recency_weights(ts[fit_idx] if ts is not None else None))

    # Platt scaling on TRULY unseen rows.
    cal = CalibratedLGBM(final)
    cal.fit_calibration(Xdf.iloc[hold], y[hold])
    cal.feature_names_ = list(FEATURE_COLUMNS)

    probs = cal.predict_proba(Xdf.iloc[hold])[:, 1]
    std = float(probs.std())
    thr = find_threshold(probs, y[hold])
    # Out-of-sample AUC — this, not the walk-forward mean, is what the deploy
    # gate should trust, because it is measured on the same rows the calibration
    # and threshold came from and none of them were fit.
    hold_auc = float(roc_auc_score(y[hold], probs)) if y[hold].sum() > 0 else 0.5
    print(f"  calibrated prob: min={probs.min():.3f} max={probs.max():.3f} "
          f"mean={probs.mean():.3f} std={std:.3f}")
    print(f"  HOLDOUT AUC = {hold_auc:.4f}  (walk-forward mean {mean_auc:.4f})")
    print(f"  best threshold: {thr}")

    return {"model": cal, "auc": mean_auc, "hold_auc": hold_auc, "std": std,
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

    # ── Final model: fit on the EARLY portion only (see CAL_FRAC note) ──
    fit_idx, hold = holdout_split(len(y))
    print(f"  holdout: fit={len(fit_idx):,} rows | hold={len(hold):,} rows "
          f"({CAL_FRAC:.0%}, out-of-sample)")

    final = CatBoostClassifier(**cat_params)
    final.fit(Xdf.iloc[fit_idx], y[fit_idx],
              sample_weight=recency_weights(ts[fit_idx] if ts is not None else None))
    cal = CalibratedLGBM(final)          # wrapper works for any sklearn-compatible model
    cal.fit_calibration(Xdf.iloc[hold], y[hold])
    cal.feature_names_ = list(FEATURE_COLUMNS)

    probs = cal.predict_proba(Xdf.iloc[hold])[:, 1]
    std   = float(probs.std())
    thr   = find_threshold(probs, y[hold])
    hold_auc = float(roc_auc_score(y[hold], probs)) if y[hold].sum() > 0 else 0.5
    print(f"  calibrated prob: min={probs.min():.3f} max={probs.max():.3f} "
          f"mean={probs.mean():.3f} std={std:.3f}")
    print(f"  HOLDOUT AUC = {hold_auc:.4f}  (walk-forward mean {mean_auc:.4f})")
    print(f"  best threshold: {thr}")

    return {"model": cal, "auc": mean_auc, "hold_auc": hold_auc, "std": std,
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


def train_disentangled(df, family="lgbm"):
    """
    Train the FIXED-label heads on the consolidated dataset:

      head A (activity) : label_touch on ALL rows        -> P(touch)
      head D (direction): label_dir on TOUCHED rows only -> P(up | touch)

    Compose per-side probabilities (DisentangledModel) and gate on the
    DIRECTION holdout AUC — the honest edge metric. The legacy fused
    label_ce/label_pe made the two heads learn ACTIVITY (corr ~ +0.78),
    which is why the deployed champions could not pick a direction.
    """
    n   = len(df)
    cut = int(n * (1.0 - CAL_FRAC))
    cut = max(min(cut, n - 1), 1)

    print("\n" + "=" * 64)
    print(f"  DISENTANGLED TRAIN ({family.upper()})  label_touch x label_dir")
    print("=" * 64)

    head = train_one_cat if family == "cat" else train_one
    if family == "cat" and not _CATBOOST_AVAILABLE:
        print("  [CATBOOST] Library not installed — skipping.")
        return None

    act = head(df, "label_touch", f"activity_{family}")

    touched = df[df["label_dir"].notna()].copy()
    print(f"\n  direction head on {len(touched):,} touched rows "
          f"(of {n:,})  touch_rate={len(touched)/n:.1%}")
    dirn = head(touched, "label_dir", f"direction_{family}")

    ce = DisentangledModel(act["model"], dirn["model"], "CE")
    pe = DisentangledModel(act["model"], dirn["model"], "PE")

    # ── Composed metrics on the activity head's chronological holdout ──
    hold      = df.iloc[cut:]
    X_hold    = hold[FEATURE_COLUMNS]
    p_ce      = ce.predict_proba(X_hold)[:, 1]
    p_pe      = pe.predict_proba(X_hold)[:, 1]
    y_ce      = hold["label_ce"].astype(int).values
    y_pe      = hold["label_pe"].astype(int).values
    ce_auc    = float(roc_auc_score(y_ce, p_ce)) if y_ce.sum() > 0 else 0.5
    pe_auc    = float(roc_auc_score(y_pe, p_pe)) if y_pe.sum() > 0 else 0.5
    std       = float(np.std(np.concatenate([p_ce, p_pe])))

    # ── THE edge metric: direction head on its own touched holdout ────
    cut_d = int(len(touched) * (1.0 - CAL_FRAC))
    cut_d = max(min(cut_d, len(touched) - 1), 1)
    t_hold   = touched.iloc[cut_d:]
    p_dir    = dirn["model"].predict_proba(t_hold[FEATURE_COLUMNS])[:, 1]
    y_dir    = t_hold["label_dir"].astype(int).values
    dir_auc  = float(roc_auc_score(y_dir, p_dir)) if y_dir.sum() > 0 else 0.5
    thr      = find_threshold(p_dir, y_dir)
    dir_exp  = thr["expectancy"]

    print("\n" + "-" * 60)
    print(f"  COMPOSED HOLDOUT  (n={len(hold):,})")
    print(f"    CE auc (vs label_ce)   : {ce_auc:.4f}")
    print(f"    PE auc (vs label_pe)   : {pe_auc:.4f}")
    print(f"    composed prob std      : {std:.4f}")
    print(f"  DIRECTION EDGE (touched holdout n={len(t_hold):,})")
    print(f"    dir auc                : {dir_auc:.4f}   (coin flip = 0.50)")
    print(f"    best thr               : {thr['threshold']}  wr={thr['wr']:.1%}  "
          f"exp=Rs{thr['expectancy']}  trades={thr['trades']}")

    ok = (dir_auc >= MIN_AUC and std >= MIN_STD and dir_exp > 0)
    print(f"\n  DISENTANGLED GATE {family.upper()}: {'PASS' if ok else 'FAIL'}")
    print(f"  (need dir_auc>={MIN_AUC}, std>={MIN_STD}, expectancy>0)")

    return {"ce": ce, "pe": pe, "auc": ce_auc, "pe_auc": pe_auc,
            "dir_auc": dir_auc, "std": std, "threshold": thr["threshold"],
            "expectancy": dir_exp, "wr": thr["wr"], "trades": thr["trades"],
            "gate": ok}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Disentangled directional trainer")
    ap.add_argument("--family", choices=["lgbm", "cat", "both"],
                    default="both", help="which model families to train")
    args = ap.parse_args()

    print("=" * 64)
    print("  DIRECTIONAL TRAINER v3  (de-saturated, deploy-gated)")
    print("=" * 64)
    print(f"  Dataset: {DATA_PATH}")
    df = pd.read_csv(DATA_PATH)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0
    # label_dir NaNs are BY DESIGN (untouched bars) — never drop rows on it.
    df = df.dropna(subset=FEATURE_COLUMNS).reset_index(drop=True)

    for lab in ("label_touch", "label_dir", "label_ce", "label_pe"):
        if lab not in df.columns:
            print(f"[ABORT] {lab} missing — run python ml/dataset_builder_v3.py first")
            sys.exit(1)

    print(f"  rows={len(df):,}  {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"  touch_rate={df['label_touch'].mean():.1%}  "
          f"dir rows={int(df['label_dir'].notna().sum()):,}")

    # ── LightGBM (required) ───────────────────────────────────────────
    if args.family in ("lgbm", "both"):
        res = train_disentangled(df, family="lgbm")

        print(f"\n{'='*64}")
        print(f"  LGBM DISENTANGLED: dir_auc={res['dir_auc']:.3f} "
              f"composed_CE_auc={res['auc']:.3f} composed_PE_auc={res['pe_auc']:.3f} "
              f"std={res['std']:.3f} thr={res['threshold']} exp=Rs{res['expectancy']}")

        if res["gate"]:
            backup_existing()
            deploy({"model": res["ce"], "threshold": res["threshold"]},
                   "champion_ce_lgbm")
            deploy({"model": res["pe"], "threshold": res["threshold"]},
                   "champion_pe_lgbm")
            print("\n  [LGBM RESULT] Disentangled LightGBM models DEPLOYED.")
        else:
            candidate({"model": res["ce"], "threshold": res["threshold"]},
                      "champion_ce_lgbm")
            candidate({"model": res["pe"], "threshold": res["threshold"]},
                      "champion_pe_lgbm")
            print("\n  [LGBM RESULT] Gate FAILED — champions left UNCHANGED.")
            print("                Candidates saved; inspect and rerun.")

    # ── CatBoost (optional — skipped if library not installed) ────────
    if args.family in ("cat", "both"):
        print(f"\n{'='*64}")
        res_cat = train_disentangled(df, family="cat") if _CATBOOST_AVAILABLE else None
        if res_cat is None:
            if not _CATBOOST_AVAILABLE:
                print("  [CATBOOST] Library not installed — skipping.")
                print("             Install with:  pip install catboost")
        else:
            print(f"\n{'='*64}")
            print(f"  CAT  DISENTANGLED: dir_auc={res_cat['dir_auc']:.3f} "
                  f"composed_CE_auc={res_cat['auc']:.3f} composed_PE_auc={res_cat['pe_auc']:.3f} "
                  f"std={res_cat['std']:.3f} exp=Rs{res_cat['expectancy']}")
            if res_cat["gate"]:
                deploy({"model": res_cat["ce"], "threshold": res_cat["threshold"]},
                       "champion_ce_cat")
                deploy({"model": res_cat["pe"], "threshold": res_cat["threshold"]},
                       "champion_pe_cat")
                print("\n  [CAT RESULT] Disentangled CatBoost models DEPLOYED. "
                      "Predictor will use LGBM+CAT_ENSEMBLE.")
            else:
                candidate({"model": res_cat["ce"], "threshold": res_cat["threshold"]},
                          "champion_ce_cat")
                candidate({"model": res_cat["pe"], "threshold": res_cat["threshold"]},
                          "champion_pe_cat")
                print("\n  [CAT RESULT] Gate FAILED — CatBoost candidates saved, "
                      "LGBM-only mode unchanged.")

    print("=" * 64)


if __name__ == "__main__":
    main()
