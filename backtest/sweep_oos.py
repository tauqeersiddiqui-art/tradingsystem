# backtest/sweep_oos.py
#
# The 47-day scan is too small to settle anything: the winning geometry cell
# earned 93% of its P&L from 3 trades (bootstrap 95% CI on EV was
# [-Rs102, +Rs248], P(EV<=0)=0.23), and the profitable probability band
# (0.42-0.48) was positive in 28 of 43 days with the top 3 days supplying
# 43% of the total. Any conclusion drawn there is noise.
#
# This sweep uses the LAST 20% of training_dataset_v3.csv instead:
#     90,252 rows | 243 trading days | 2024-11-28 -> 2026-09-02
# Those are exactly the rows `holdout_split()` withheld from the final fit, so
# the deployed champions never saw them, and the file already carries OHLC plus
# all 35 features — no re-scan needed.
#
# 243 days vs 47 is a 5x larger sample and spans two regimes rather than one
# summer. It also contains the 2026 price levels the live engine actually sees.
#
# RUN: python backtest/sweep_oos.py
#      python backtest/sweep_oos.py --stage gates      (thr x edge only)

import os
import sys
import argparse
import numpy as np
import pandas as pd
import joblib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.feature_config import FEATURE_COLUMNS
from engine.config.config import Config as _LiveConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_ROOT, "ml", "models", "training_dataset_v3.csv")
_CACHE = os.path.join(_ROOT, "data", "diagnostics", "oos_scan.csv")

_CFG = _LiveConfig()
LOT = int(getattr(_CFG, "LOT_SIZE", 30) or 30)
COST = float(getattr(_CFG, "COST_PER_LOT", 66.0) or 66.0)
MAX_TRADES = int(getattr(_CFG, "MAX_TRADES_PER_DAY", 4) or 4)
_VWAP_TOL = 0.0015
_DELTA = 0.5
CAL_FRAC = 0.20
MAX_H = 30


def load_scan(force=False):
    """Score the OOS tail with the deployed ensemble. Cached — it is slow."""
    if os.path.exists(_CACHE) and not force:
        return pd.read_csv(_CACHE)

    df = pd.read_csv(_DATA).dropna()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    cut = int(len(df) * (1.0 - CAL_FRAC))
    df = df.iloc[cut:].reset_index(drop=True)
    print(f"[OOS] {len(df):,} rows | {df['date'].dt.date.nunique()} days | "
          f"{df['date'].min().date()} -> {df['date'].max().date()}")

    X = pd.DataFrame(df[FEATURE_COLUMNS].values, columns=FEATURE_COLUMNS)
    m = {k: joblib.load(os.path.join(_ROOT, "ml", "models", f"champion_{k}.pkl"))
         for k in ("ce_lgbm", "pe_lgbm", "ce_cat", "pe_cat")}
    print("[OOS] scoring ensemble ...")
    df["ce_adj"] = (m["ce_lgbm"].predict_proba(X)[:, 1]
                    + m["ce_cat"].predict_proba(X)[:, 1]) / 2
    df["pe_adj"] = (m["pe_lgbm"].predict_proba(X)[:, 1]
                    + m["pe_cat"].predict_proba(X)[:, 1]) / 2

    keep = ["date", "open", "high", "low", "close", "atr", "supertrend_dir",
            "price_vs_vwap", "adx", "label_ce", "label_pe", "ce_adj", "pe_adj"]
    out = df[keep].copy()
    out["day"] = out["date"].dt.date.astype(str)
    # 5m SuperTrend proxy: the live HTF gate. The 1m dir over a 5-bar mode is
    # the closest thing available without re-deriving from raw OHLC here; the
    # sign is what the gate reads.
    out["htf5_dir"] = (out.groupby("day")["supertrend_dir"]
                       .transform(lambda s: s.rolling(5, min_periods=1).mean())
                       .apply(lambda v: 1 if v > 0.2 else (-1 if v < -0.2 else 0)))
    os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
    out.to_csv(_CACHE, index=False)
    print(f"[OOS] cached -> {_CACHE}")
    return out


def prep(df):
    fhs, fls, fcs = [], [], []
    for _, g in df.groupby("day", sort=False):
        c = g["close"].values.astype(float)
        h = g["high"].values.astype(float)
        l = g["low"].values.astype(float)
        n = len(g)
        fh = np.full((n, MAX_H), np.nan)
        fl = np.full((n, MAX_H), np.nan)
        fc = np.full((n, MAX_H), np.nan)
        for k in range(MAX_H):
            i = np.arange(n) + k + 1
            ok = i < n
            fh[ok, k] = h[i[ok]] - c[ok]
            fl[ok, k] = l[i[ok]] - c[ok]
            fc[ok, k] = c[i[ok]] - c[ok]
        fhs.append(fh); fls.append(fl); fcs.append(fc)
    return np.vstack(fhs), np.vstack(fls), np.vstack(fcs)


def sim(df, FH, FL, FC, thr, edge, tgt_spot, stop_spot, hold, day_mask=None):
    ce, pe = df["ce_adj"].values, df["pe_adj"].values
    sce = ce >= pe
    prob = np.where(sce, ce, pe)
    gap = np.abs(ce - pe)
    htf, pvw = df["htf5_dir"].values, df["price_vs_vwap"].values
    days = df["day"].values

    ok = (gap >= edge) & (prob >= min(thr, 0.65))
    ok &= ~((sce & (htf == -1)) | (~sce & (htf == 1)))
    ok &= ~((sce & (pvw < -_VWAP_TOL)) | (~sce & (pvw > _VWAP_TOL)))
    # Live only trades 09:30-15:00; the dataset spans the whole session.
    mins = df["date"].dt.hour * 60 + df["date"].dt.minute
    ok &= (mins >= 570) & (mins < 900)
    if day_mask is not None:
        ok &= day_mask

    keep, cur, nd, last = [], None, 0, -10 ** 9
    for i in np.flatnonzero(ok):
        d = days[i]
        if d != cur:
            cur, nd, last = d, 0, -10 ** 9
        if nd >= MAX_TRADES or i - last < max(5, hold):
            continue
        keep.append(i)
        nd += 1
        last = i

    pnl, sides = [], []
    for i in keep:
        res = None
        for k in range(hold):
            hi, lo = FH[i, k], FL[i, k]
            if np.isnan(hi):
                break
            adv = -lo if sce[i] else hi
            fav = hi if sce[i] else -lo
            if adv >= stop_spot:
                res = -stop_spot * _DELTA
                break
            if fav >= tgt_spot:
                res = tgt_spot * _DELTA
                break
        if res is None:
            lc = np.nan
            for k in range(min(hold, MAX_H) - 1, -1, -1):
                if not np.isnan(FC[i, k]):
                    lc = FC[i, k]
                    break
            if np.isnan(lc):
                continue
            res = (lc if sce[i] else -lc) * _DELTA
        pnl.append(res * LOT - COST)
        sides.append("CE" if sce[i] else "PE")
    return np.array(pnl), sides


def stats(pnl):
    if len(pnl) == 0:
        return None
    w = pnl[pnl > 0].sum()
    lo = -pnl[pnl <= 0].sum()
    return {"n": len(pnl), "wr": (pnl > 0).mean(),
            "pf": (w / lo) if lo > 0 else 99.0,
            "ev": pnl.mean(), "net": pnl.sum(),
            "sd": pnl.std(ddof=1) if len(pnl) > 1 else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--stage", default="all")
    args = ap.parse_args()

    df = load_scan(force=args.force)
    df["date"] = pd.to_datetime(df["date"])
    print(f"[OOS] {len(df):,} rows | {df['day'].nunique()} days | "
          f"close {df['close'].min():.0f}-{df['close'].max():.0f}")
    print(f"      lot={LOT} cost=Rs{COST}/lot cap={MAX_TRADES}/day\n")
    FH, FL, FC = prep(df)

    print("GEOMETRY SWEEP  (thr=0.42 edge=0.04, spot-space target/stop)")
    print(f"{'tgt':>5} {'stop':>5} {'hold':>5} {'n':>5} {'WR':>6} {'PF':>6} "
          f"{'EV/tr':>8} {'NET Rs':>11} {'t-stat':>7}")
    print("-" * 68)
    rows = []
    for tgt, stop in [(40, 40), (50, 50), (50, 35), (60, 30), (70, 35),
                      (80, 40), (100, 50), (120, 60)]:
        for hold in (12, 15, 20, 30):
            p, _ = sim(df, FH, FL, FC, 0.42, 0.04, tgt, stop, hold)
            s = stats(p)
            if not s or s["n"] < 30:
                continue
            t = s["ev"] / (s["sd"] / np.sqrt(s["n"])) if s["sd"] > 0 else 0.0
            rows.append({"tgt": tgt, "stop": stop, "hold": hold, **s, "t": t})
            print(f"{tgt:5} {stop:5} {hold:5} {s['n']:5,} {s['wr']:6.1%} "
                  f"{s['pf']:6.2f} {s['ev']:8.1f} {s['net']:11,.0f} {t:7.2f}")
    r = pd.DataFrame(rows).sort_values("ev", ascending=False)
    out = os.path.join(_ROOT, "data", "diagnostics", "oos_geometry.csv")
    r.to_csv(out, index=False)
    print(f"\n[OOS] {(r['net'] > 0).sum()}/{len(r)} cells net-positive | wrote {out}")
    if len(r):
        b = r.iloc[0]
        print(f"  BEST EV: tgt={int(b['tgt'])} stop={int(b['stop'])} "
              f"hold={int(b['hold'])} n={int(b['n'])} WR={b['wr']:.1%} "
              f"PF={b['pf']:.2f} EV=Rs{b['ev']:.1f} t={b['t']:.2f}")


if __name__ == "__main__":
    main()
