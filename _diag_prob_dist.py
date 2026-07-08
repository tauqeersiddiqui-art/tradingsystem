"""
Probability distribution diagnostic — NO production files modified.
Replays today's session candles through champion models and compares
output distribution against the training validation holdout.
"""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
from sklearn.model_selection import TimeSeriesSplit
from scipy import stats as sp_stats

from ml.feature_config import FEATURE_COLUMNS
from ml.dataset_builder_v2 import compute_all_features

# ── Load models ───────────────────────────────────────────────────────
ce_model = joblib.load("ml/models/champion_ce_lgbm.pkl")
pe_model = joblib.load("ml/models/champion_pe_lgbm.pkl")
print(f"Models loaded.  Feature count: CE={len(ce_model.feature_names_)}  PE={len(pe_model.feature_names_)}")

# ── Load today's candles with warm context ────────────────────────────
df_all = pd.read_csv("data/historical/banknifty_1m_full.csv")
df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
df_all = df_all.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
if "volume" not in df_all.columns:
    df_all["volume"] = 0.0

today = df_all["date"].max().normalize()
df_today = df_all[df_all["date"].dt.normalize() == today]
df_ctx   = df_all[df_all["date"] < today].tail(200)
df_combined = pd.concat([df_ctx, df_today], ignore_index=True)

df_featured = compute_all_features(df_combined.copy())
df_session  = df_featured[df_featured["date"].dt.normalize() == today].copy()
df_session  = df_session[df_session["date"].dt.time >= pd.to_datetime("09:15").time()].reset_index(drop=True)
print(f"Today ({today.date()}): {len(df_session)} session candles  "
      f"{df_session['date'].min().time()} -> {df_session['date'].max().time()}")

missing = [f for f in FEATURE_COLUMNS if f not in df_session.columns]
if missing:
    print(f"MISSING FEATURES: {missing}")
    sys.exit(1)

X_today = df_session[FEATURE_COLUMNS].copy()
for c in FEATURE_COLUMNS:
    X_today[c] = pd.to_numeric(X_today[c], errors="coerce").fillna(0.0)

# ── Probabilities: raw LGBM + Platt calibrated ───────────────────────
ce_raw = ce_model.base_model.predict_proba(X_today)[:, 1]
pe_raw = pe_model.base_model.predict_proba(X_today)[:, 1]
ce_cal = ce_model.predict_proba(X_today)[:, 1]
pe_cal = pe_model.predict_proba(X_today)[:, 1]

# ── Validation holdout (last TimeSeriesSplit fold) ────────────────────
df_train = pd.read_csv("ml/models/training_dataset_v3.csv")
df_train["date"] = pd.to_datetime(df_train["date"], errors="coerce")
df_train = df_train.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
for c in FEATURE_COLUMNS:
    if c not in df_train.columns:
        df_train[c] = 0.0

X_full = df_train[FEATURE_COLUMNS].values
*_, (_, hold) = TimeSeriesSplit(n_splits=5).split(X_full)
X_hold = pd.DataFrame(X_full[hold], columns=FEATURE_COLUMNS)

print(f"Validation holdout: {len(X_hold)} rows  "
      f"({df_train['date'].iloc[hold[0]].date()} -> {df_train['date'].iloc[hold[-1]].date()})")

ce_val_raw = ce_model.base_model.predict_proba(X_hold)[:, 1]
pe_val_raw = pe_model.base_model.predict_proba(X_hold)[:, 1]
ce_val_cal = ce_model.predict_proba(X_hold)[:, 1]
pe_val_cal = pe_model.predict_proba(X_hold)[:, 1]

# ── Stats helper ──────────────────────────────────────────────────────
def stats(arr, label):
    return dict(label=label, n=len(arr),
                mean=np.mean(arr), median=np.median(arr),
                max=np.max(arr), p95=np.percentile(arr, 95),
                p99=np.percentile(arr, 99), std=np.std(arr),
                pct_above_079=np.mean(arr >= 0.79)*100,
                pct_above_050=np.mean(arr >= 0.50)*100)

all_stats = [
    stats(ce_raw,     "TODAY_CE_RAW_LGBM"),
    stats(pe_raw,     "TODAY_PE_RAW_LGBM"),
    stats(ce_cal,     "TODAY_CE_CALIBRATED"),
    stats(pe_cal,     "TODAY_PE_CALIBRATED"),
    stats(ce_val_raw, "VALID_CE_RAW_LGBM"),
    stats(pe_val_raw, "VALID_PE_RAW_LGBM"),
    stats(ce_val_cal, "VALID_CE_CALIBRATED"),
    stats(pe_val_cal, "VALID_PE_CALIBRATED"),
]

# ── Distribution table ────────────────────────────────────────────────
print("\n" + "="*90)
print("  PROBABILITY DISTRIBUTION ANALYSIS")
print("="*90)
hdr = f"{'Label':<26} {'N':>6} {'Mean':>6} {'Median':>6} {'Max':>6} {'P95':>6} {'P99':>6} {'Std':>6} {'>0.79%':>7} {'>0.50%':>7}"
print(hdr)
print("-"*90)
for s in all_stats:
    if s['label'].startswith("VALID") and not all_stats[all_stats.index(s)-1]['label'].startswith("VALID"):
        print()
    print(f"{s['label']:<26} {s['n']:>6} {s['mean']:>6.3f} {s['median']:>6.3f} "
          f"{s['max']:>6.3f} {s['p95']:>6.3f} {s['p99']:>6.3f} {s['std']:>6.3f} "
          f"{s['pct_above_079']:>6.1f}% {s['pct_above_050']:>6.1f}%")

# ── KS tests ─────────────────────────────────────────────────────────
print("\n" + "="*90)
print("  KS TEST  (today vs validation)   p > 0.05 = statistically similar")
print("="*90)
pairs = [
    (ce_raw,  ce_val_raw, "CE RAW LGBM"),
    (pe_raw,  pe_val_raw, "PE RAW LGBM"),
    (ce_cal,  ce_val_cal, "CE CALIBRATED"),
    (pe_cal,  pe_val_cal, "PE CALIBRATED"),
]
for today_arr, val_arr, label in pairs:
    ks, p = sp_stats.ks_2samp(today_arr, val_arr)
    verdict = "SIMILAR" if p > 0.05 else ("MARGINAL" if p > 0.001 else "DIVERGED")
    print(f"  {label:<22}  KS={ks:.4f}  p={p:.2e}  -> {verdict}")

# ── ASCII histograms ──────────────────────────────────────────────────
def histogram(arr, label, bins=10):
    counts, edges = np.histogram(arr, bins=bins, range=(0.0, 1.0))
    total = len(arr)
    print(f"\n  {label}")
    for lo, hi, c in zip(edges, edges[1:], counts):
        bar = "█" * int(c / max(counts) * 40)
        print(f"    [{lo:.1f}-{hi:.1f}]  {bar:<40}  {c:>6} ({c/total*100:5.1f}%)")

print("\n" + "="*90)
print("  HISTOGRAMS — CE CALIBRATED (Platt output, 0-1)")
print("="*90)
histogram(ce_cal,     "TODAY session")
histogram(ce_val_cal, "VALIDATION holdout")

print("\n" + "="*90)
print("  HISTOGRAMS — PE CALIBRATED")
print("="*90)
histogram(pe_cal,     "TODAY session")
histogram(pe_val_cal, "VALIDATION holdout")

print("\n" + "="*90)
print("  HISTOGRAMS — CE RAW LGBM (before Platt)")
print("="*90)
histogram(ce_raw,     "TODAY session")
histogram(ce_val_raw, "VALIDATION holdout")

# ── Feature mean-shift (where is today different from validation?) ─────
print("\n" + "="*90)
print("  TOP-15 FEATURE MEAN SHIFT  (today session vs validation holdout)")
print("  Large relative delta = feature is in a different regime today")
print("="*90)
today_means = X_today.mean()
val_means   = pd.DataFrame(X_hold, columns=FEATURE_COLUMNS).mean()
rel_delta   = ((today_means - val_means) / (val_means.abs() + 1e-9)).abs()
print(f"  {'Feature':<28} {'Today mean':>12} {'Val mean':>12} {'Abs rel delta':>14}")
print(f"  {'-'*28}  {'-'*12}  {'-'*12}  {'-'*14}")
for feat in rel_delta.sort_values(ascending=False).head(15).index:
    print(f"  {feat:<28}  {today_means[feat]:>12.5f}  {val_means[feat]:>12.5f}  {rel_delta[feat]:>14.2f}x")

# ── Per-candle table (first 20 + last 5) ─────────────────────────────
print("\n" + "="*90)
print("  PER-CANDLE PROBABILITIES (first 20 candles)")
print("="*90)
print(f"  {'Time':<8}  {'CE_raw':>7}  {'PE_raw':>7}  {'CE_cal':>7}  {'PE_cal':>7}  {'signal?':>10}")
print(f"  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*10}")
for i in range(min(20, len(df_session))):
    ts  = df_session["date"].iloc[i].strftime("%H:%M")
    sig = ">>> FIRE" if (ce_cal[i] >= 0.79 or pe_cal[i] >= 0.79) else ""
    print(f"  {ts:<8}  {ce_raw[i]:>7.4f}  {pe_raw[i]:>7.4f}  {ce_cal[i]:>7.4f}  {pe_cal[i]:>7.4f}  {sig}")

if len(df_session) > 25:
    print(f"  ... ({len(df_session)-25} rows omitted) ...")
    for i in range(len(df_session)-5, len(df_session)):
        ts  = df_session["date"].iloc[i].strftime("%H:%M")
        sig = ">>> FIRE" if (ce_cal[i] >= 0.79 or pe_cal[i] >= 0.79) else ""
        print(f"  {ts:<8}  {ce_raw[i]:>7.4f}  {pe_raw[i]:>7.4f}  {ce_cal[i]:>7.4f}  {pe_cal[i]:>7.4f}  {sig}")

print("\nDone.")
