# ml/probe_direction.py
#
# Is DIRECTION learnable from the current 35-feature vector at all?
#
# WHY THIS PROBE EXISTS
#   The deployed champions report OOS AUC 0.6446 (CE) / 0.6207 (PE) on a clean
#   243-day holdout, yet a replay of those same rows produced ZERO net-positive
#   parameter cells out of 32 geometries, and the raw signed forward move of the
#   argmax side was -0.26 spot points against a 4.40-point cost hurdle.
#
#   The decomposition explains the contradiction:
#       corr(ce_adj, pe_adj)                              = +0.783
#       AUC of (ce+pe) predicting "any 50pt barrier hit"  =  0.754
#       AUC of (ce-pe) predicting DIRECTION, touched rows =  0.515
#   Both heads are the same VOLATILITY forecast wearing two labels. label_ce=0
#   and label_pe=0 on the identical "quiet" bars (36.9% of rows), so both models
#   score high AUC by learning which bars are active. argmax over two activity
#   forecasts carries no directional information — which is exactly what the
#   replay measured.
#
#   Before rebuilding anything, establish the ceiling: train a model on the
#   CONDITIONAL problem (direction GIVEN a barrier is touched) and see whether
#   any horizon / target / regime slice clears AUC 0.55. If nothing does, no
#   amount of threshold or geometry tuning can produce a profitable directional
#   bot from this feature set, and the honest recommendation is to stop trading
#   direction and trade what IS predictable.
#
# RUN: python ml/probe_direction.py
#      python ml/probe_direction.py --quick   (horizons only)

import os
import sys
import argparse
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

PARAMS = dict(n_estimators=400, learning_rate=0.03, max_depth=6, num_leaves=31,
              subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
              reg_alpha=0.05, reg_lambda=0.1, verbose=-1, random_state=42,
              n_jobs=-1)


def first_touch(close, high, low, tgt, look):
    """Vectorised-ish first-touch: returns (up_first, dn_first) int8 arrays."""
    n = len(close)
    ce = np.zeros(n, dtype=np.int8)
    pe = np.zeros(n, dtype=np.int8)
    for i in range(n - look):
        up, dn = close[i] + tgt, close[i] - tgt
        uh = dh = -1
        for j in range(i + 1, i + look + 1):
            if uh < 0 and high[j] >= up:
                uh = j
            if dh < 0 and low[j] <= dn:
                dh = j
            if uh >= 0 and dh >= 0:
                break
        if uh >= 0 and (dh < 0 or uh <= dh):
            ce[i] = 1
        elif dh >= 0 and (uh < 0 or dh < uh):
            pe[i] = 1
    return ce, pe


def label_by_day(df, tgt, look):
    ce = np.zeros(len(df), dtype=np.int8)
    pe = np.zeros(len(df), dtype=np.int8)
    df = df.copy()
    df["_d"] = df["date"].dt.date
    for _, g in df.groupby("_d", sort=False):
        pos = g.index.values
        c, pe_ = first_touch(g["close"].values.astype(float),
                             g["high"].values.astype(float),
                             g["low"].values.astype(float), tgt, look)
        ce[pos] = c
        pe[pos] = pe_
    return ce, pe


def probe(tr, te, ycol_tr, ycol_te, tag, mask_tr=None, mask_te=None):
    Xtr = tr[FEATURE_COLUMNS]
    Xte = te[FEATURE_COLUMNS]
    ytr, yte = ycol_tr, ycol_te
    if mask_tr is not None:
        Xtr, ytr = Xtr[mask_tr], ytr[mask_tr]
    if mask_te is not None:
        Xte, yte = Xte[mask_te], yte[mask_te]
    if len(np.unique(ytr)) < 2 or len(np.unique(yte)) < 2 or len(ytr) < 500:
        print(f"  {tag:<44} insufficient data")
        return np.nan
    m = lgb.LGBMClassifier(**PARAMS)
    m.fit(Xtr, ytr)
    p = m.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p)
    print(f"  {tag:<44} n_tr={len(ytr):7,} n_te={len(yte):6,} "
          f"pos={np.mean(ytr):.3f}  AUC={auc:.4f}")
    return auc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    df = pd.read_csv(_DATA).dropna()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    cut = int(len(df) * 0.8)
    print("=" * 78)
    print("  DIRECTION LEARNABILITY PROBE")
    print("=" * 78)
    print(f"  train {cut:,} rows  ({df.date.iloc[0].date()} .. {df.date.iloc[cut-1].date()})")
    print(f"  test  {len(df)-cut:,} rows  ({df.date.iloc[cut].date()} .. {df.date.iloc[-1].date()})")
    print(f"  features: {len(FEATURE_COLUMNS)}")

    print("\n[1] CONDITIONAL DIRECTION across horizons and barrier sizes")
    print("    P(up_first | a barrier is touched)  -- 0.55 is the tradability bar")
    res = []
    for tgt, look in [(25, 6), (50, 12), (50, 20), (50, 30),
                      (75, 20), (75, 30), (100, 30), (100, 45)]:
        ce, pe = label_by_day(df, tgt, look)
        touch = ((ce == 1) | (pe == 1))
        tr, te = df.iloc[:cut], df.iloc[cut:]
        auc = probe(tr, te, ce[:cut].astype(int), ce[cut:].astype(int),
                    f"tgt={tgt}pt look={look}b  (touch {touch.mean():.1%})",
                    mask_tr=touch[:cut], mask_te=touch[cut:])
        res.append({"tgt": tgt, "look": look, "auc": auc,
                    "touch_rate": touch.mean()})
        if args.quick and len(res) >= 3:
            break

    print("\n[2] ACTIVITY / VOLATILITY — the same features, different question")
    print("    P(any barrier touched)  -- for contrast")
    for tgt, look in [(50, 12), (75, 20), (100, 30)]:
        ce, pe = label_by_day(df, tgt, look)
        touch = ((ce == 1) | (pe == 1)).astype(int)
        probe(df.iloc[:cut], df.iloc[cut:], touch[:cut], touch[cut:],
              f"tgt={tgt}pt look={look}b  ACTIVITY")

    if not args.quick:
        print("\n[3] CONDITIONAL DIRECTION inside REGIME SLICES")
        print("    (maybe direction is only learnable when trending)")
        ce, pe = label_by_day(df, 50, 12)
        touch = ((ce == 1) | (pe == 1))
        tr, te = df.iloc[:cut], df.iloc[cut:]
        for name, cond in [
            ("ADX > 25 (trending)", df["adx"] > 25),
            ("ADX < 20 (ranging)", df["adx"] < 20),
            ("first 90 min", df["mins_since_open"] < 90),
            ("last 90 min", df["mins_to_close"] < 90),
            ("high vol (atr>p75)", df["atr"] > df["atr"].quantile(0.75)),
            ("low vol (atr<p25)", df["atr"] < df["atr"].quantile(0.25)),
        ]:
            m = touch & cond.values
            probe(tr, te, ce[:cut].astype(int), ce[cut:].astype(int),
                  name, mask_tr=m[:cut], mask_te=m[cut:])

        print("\n[4] SIGN OF THE NEXT N-BAR RETURN  (no barrier at all)")
        for H in (5, 12, 30, 60):
            fwd = np.full(len(df), np.nan)
            d2 = df.copy()
            d2["_d"] = d2["date"].dt.date
            for _, g in d2.groupby("_d", sort=False):
                c = g["close"].values.astype(float)
                pos = g.index.values
                if len(c) > H:
                    fwd[pos[:len(c) - H]] = c[H:] - c[:len(c) - H]
            ok = ~np.isnan(fwd)
            y = (fwd > 0).astype(int)
            probe(df.iloc[:cut], df.iloc[cut:], y[:cut], y[cut:],
                  f"sign(close[t+{H}] - close[t])",
                  mask_tr=ok[:cut], mask_te=ok[cut:])

    r = pd.DataFrame(res)
    best = r["auc"].max()
    print("\n" + "=" * 78)
    print(f"  BEST CONDITIONAL-DIRECTION AUC = {best:.4f}")
    if best < 0.53:
        print("  VERDICT: direction is NOT learnable from these 35 features.")
        print("           The 0.64 headline AUC is a volatility forecast.")
        print("           No threshold or geometry change can fix a coin flip.")
    elif best < 0.56:
        print("  VERDICT: marginal. Edge too small to clear Rs66/lot round trip.")
    else:
        print("  VERDICT: direction IS learnable — rebuild on the conditional label.")
    print("=" * 78)
    out = os.path.join(_ROOT, "data", "diagnostics", "direction_probe.csv")
    r.to_csv(out, index=False)
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
