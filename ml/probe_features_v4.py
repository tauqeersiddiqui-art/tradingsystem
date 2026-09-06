# ml/probe_features_v4.py
#
# "How do I increase ML prediction power?" — answered empirically.
#
# probe_direction.py established that conditional direction (P(up first | a
# barrier is touched)) sits at OOS AUC 0.49-0.52 across 8 target/horizon pairs,
# 6 regime slices and 4 raw return horizons. That is a coin flip. The question
# this script answers is whether that is a FEATURE problem (the current 35 are
# all single-timeframe 1m derivatives of the same close series) or an
# INFORMATION problem (1m spot OHLC does not contain the answer).
#
# It adds 30 features of genuinely different kinds — multi-timeframe trend,
# overnight/prior-day context, rolling autocorrelation and variance ratios,
# range position, time-of-day interactions, streak structure — and re-runs the
# same conditional-direction test. If AUC stays at 0.51 the ceiling is the DATA,
# not the feature engineering, and the remedy is new data sources (options
# premium/OI, order-flow, higher-timeframe context), not more indicators.
#
# RUN: python ml/probe_features_v4.py

import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

warnings.filterwarnings("ignore")

from ml.feature_config import FEATURE_COLUMNS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_ROOT, "ml", "models", "training_dataset_v3.csv")

PARAMS = dict(n_estimators=500, learning_rate=0.03, max_depth=7, num_leaves=63,
              subsample=0.8, colsample_bytree=0.7, min_child_samples=50,
              reg_alpha=0.05, reg_lambda=0.1, verbose=-1, random_state=42,
              n_jobs=-1)


def add_new_features(df):
    """30 features that are NOT re-scalings of the existing 35."""
    df = df.copy()
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    g = df.groupby(df["date"].dt.date)

    # ── Multi-timeframe trend (the current set is 1m only) ────────────
    for w in (15, 30, 60, 120):
        df[f"ret_{w}"] = c.pct_change(w)
        df[f"ema_slope_{w}"] = (c.ewm(span=w).mean().diff(5)) / c
    for w in (30, 120):
        df[f"pos_in_range_{w}"] = ((c - l.rolling(w).min())
                                   / (h.rolling(w).max() - l.rolling(w).min() + 1e-9))

    # ── Variance-ratio / mean-reversion vs trending structure ─────────
    r1 = c.pct_change()
    for w in (30, 60):
        df[f"vr_{w}"] = (r1.rolling(w).sum().abs()
                         / (r1.abs().rolling(w).sum() + 1e-9))
        df[f"acf1_{w}"] = r1.rolling(w).apply(
            lambda s: pd.Series(s).autocorr(1) if s.std() > 0 else 0.0, raw=False)

    # ── Prior-day / overnight context ─────────────────────────────────
    day_open = g["open"].transform("first")
    day_high = g["high"].transform("cummax")
    day_low = g["low"].transform("cummin")
    df["from_day_open"] = (c - day_open) / c
    df["day_range_pos"] = (c - day_low) / (day_high - day_low + 1e-9)
    df["day_range_atr"] = (day_high - day_low) / (df["atr"] + 1e-9)
    # Previous-day close is a DATE-keyed shift, not a positional one: a
    # positional shift of a within-day broadcast would let day D's FINAL close
    # leak into every intraday bar (that "feature" = (open - day_final)/c is
    # the day's whole move — textbook label leakage).
    prev_close = g["close"].last().shift(1)
    df["gap_from_prev"] = (day_open - df["date"].dt.date.map(prev_close)) / c

    # ── Streak / persistence structure ────────────────────────────────
    up = (c.diff() > 0).astype(int)
    df["up_streak"] = up.groupby((up != up.shift()).cumsum()).cumcount() + 1
    df["up_streak"] *= np.where(up == 1, 1, -1)
    for w in (10, 30):
        df[f"up_frac_{w}"] = up.rolling(w).mean()

    # ── Volatility term structure ─────────────────────────────────────
    for w in (10, 60):
        df[f"rv_{w}"] = r1.rolling(w).std()
    df["vol_ratio_10_60"] = df["rv_10"] / (df["rv_60"] + 1e-9)
    df["atr_expand"] = df["atr"] / (df["atr"].rolling(60).mean() + 1e-9)

    # ── Time-of-day interactions ──────────────────────────────────────
    df["tod_sin"] = np.sin(2 * np.pi * df["mins_since_open"] / 375.0)
    df["tod_cos"] = np.cos(2 * np.pi * df["mins_since_open"] / 375.0)
    df["adx_x_tod"] = df["adx"] * df["mins_since_open"] / 375.0
    df["st_x_adx"] = df["supertrend_dir"] * df["adx"]
    df["vwap_x_adx"] = df["price_vs_vwap"] * df["adx"]

    new = [x for x in df.columns
           if x not in FEATURE_COLUMNS and x not in
           ("date", "open", "high", "low", "close", "volume", "label_ce",
            "label_pe", "ce_eligible", "pe_eligible", "_d")]
    return df, new


def label_cond(df, tgt=50, look=12):
    ce = np.zeros(len(df), dtype=np.int8)
    pe = np.zeros(len(df), dtype=np.int8)
    for _, g in df.groupby(df["date"].dt.date, sort=False):
        pos = g.index.values
        cl = g["close"].values.astype(float)
        hi = g["high"].values.astype(float)
        lo = g["low"].values.astype(float)
        n = len(cl)
        for i in range(n - look):
            up, dn = cl[i] + tgt, cl[i] - tgt
            uh = dh = -1
            for j in range(i + 1, i + look + 1):
                if uh < 0 and hi[j] >= up:
                    uh = j
                if dh < 0 and lo[j] <= dn:
                    dh = j
                if uh >= 0 and dh >= 0:
                    break
            if uh >= 0 and (dh < 0 or uh <= dh):
                ce[pos[i]] = 1
            elif dh >= 0 and (uh < 0 or dh < uh):
                pe[pos[i]] = 1
    return ce, pe


def run(tr, te, cols, ytr, yte, tag):
    m = lgb.LGBMClassifier(**PARAMS)
    m.fit(tr[cols], ytr)
    p = m.predict_proba(te[cols])[:, 1]
    auc = roc_auc_score(yte, p)
    print(f"  {tag:<46} {len(cols):3d} feats  AUC={auc:.4f}")
    return auc, m, p


def main():
    print("=" * 78)
    print("  FEATURE-EXPANSION PROBE — can MORE/BETTER features find direction?")
    print("=" * 78)
    df = pd.read_csv(_DATA).dropna()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    print("[1] engineering 30 new multi-timeframe / context features ...")
    df, new_cols = add_new_features(df)
    ce, pe = label_cond(df)
    touch = (ce == 1) | (pe == 1)
    df["y"] = ce
    df = df.replace([np.inf, -np.inf], np.nan)
    all_cols = list(FEATURE_COLUMNS) + new_cols
    df[all_cols] = df[all_cols].fillna(0.0)

    cut = int(len(df) * 0.8)
    tr_m, te_m = touch[:cut], touch[cut:]
    tr = df.iloc[:cut][tr_m]
    te = df.iloc[cut:][te_m]
    ytr, yte = tr["y"].values, te["y"].values
    print(f"    new features: {len(new_cols)}")
    print(f"    conditional rows: train {len(tr):,}  test {len(te):,}  "
          f"pos={ytr.mean():.3f}")

    print("\n[2] CONDITIONAL DIRECTION — feature sets compared")
    a1, _, _ = run(tr, te, list(FEATURE_COLUMNS), ytr, yte, "baseline 35 features")
    a2, m2, p2 = run(tr, te, new_cols, ytr, yte, "30 NEW features only")
    a3, m3, p3 = run(tr, te, all_cols, ytr, yte, "all 65 features")

    print("\n[3] top 15 features of the 65-feature model")
    imp = pd.Series(m3.feature_importances_, index=all_cols).sort_values(ascending=False)
    for k, v in imp.head(15).items():
        mark = " (NEW)" if k in new_cols else ""
        print(f"    {k:<24} {int(v):6,}{mark}")

    print("\n[4] Does it help WHERE IT MATTERS? (high-confidence rows only)")
    te2 = te.copy()
    te2["p"] = p3
    for q in (0.50, 0.80, 0.90, 0.95, 0.99):
        thr_hi = te2["p"].quantile(q)
        thr_lo = te2["p"].quantile(1 - q)
        sel = te2[(te2["p"] >= thr_hi) | (te2["p"] <= thr_lo)]
        if len(sel) < 100:
            continue
        # accuracy of taking the model's side
        pred = (sel["p"] >= 0.5).astype(int)
        acc = (pred == sel["y"]).mean()
        print(f"    top/bottom {100*(1-q):4.0f}%: n={len(sel):6,} directional accuracy={acc:.3%}")

    print("\n" + "=" * 78)
    best = max(a1, a2, a3)
    print(f"  baseline={a1:.4f}  new-only={a2:.4f}  combined={a3:.4f}   best={best:.4f}")
    if best < 0.53:
        print("  VERDICT: 30 new features of 6 different KINDS moved nothing.")
        print("           The ceiling is the DATA (1m spot OHLC), not the features.")
        print("           More indicators derived from the same series cannot help.")
    else:
        print("  VERDICT: features DO help — rebuild the training set with them.")
    print("=" * 78)


if __name__ == "__main__":
    main()
