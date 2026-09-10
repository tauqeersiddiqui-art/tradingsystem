# backtest/sweep_geometry.py
#
# Joint sweep of the STOP/TARGET/HOLD geometry against the cached prob scan.
#
# WHY THIS EXISTS
#   sweep_gates.py showed every (threshold, edge) cell is net-negative. That
#   sweep held the geometry FIXED at the configured values, and the configured
#   values are structurally unwinnable:
#
#       stop   = clamp(0.5*ATR*1.5, 4, 20) -> median 16.4 premium pts
#       target = stop * 3.5                -> median 57.3 premium pts
#       delta  = 0.5  =>  needs +115 SPOT pts, risks -33 SPOT pts
#       clock  = MAX_HOLD_SECONDS 300 = 5 bars
#
#   Measured on the same 16,071 candles the models score:
#       P(favourable 5-bar excursion >= 115 pts) =  0.96%
#       P(adverse    5-bar excursion >=  33 pts) = 28.01%
#   The target is unreachable ~99% of the time while the stop is 29x more
#   likely. No threshold can rescue that, which is why all 36 cells lost.
#
#   So sweep the geometry itself: TARGET_R x HOLD_BARS x SL_ATR_MULT, scoring
#   with the same premium-space first-touch and the same Rs66/lot round trip.
#
# RUN: python backtest/sweep_geometry.py [--thr 0.42] [--edge 0.04]

import os
import sys
import argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config.config import Config as _LiveConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEF_SCAN = os.path.join(_ROOT, "data", "diagnostics", "bn_prob_scan.csv")

_CFG = _LiveConfig()
LOT = int(getattr(_CFG, "LOT_SIZE", 30) or 30)
COST = float(getattr(_CFG, "COST_PER_LOT", 66.0) or 66.0)
MAX_TRADES = int(getattr(_CFG, "MAX_TRADES_PER_DAY", 4) or 4)
_VWAP_TOL = 0.0015
_DELTA = 0.5
_SL_MIN, _SL_MAX = 4.0, 20.0


def stop_pts_for(atr, mult, cap):
    return np.clip(_DELTA * atr * mult, _SL_MIN, cap)


def prep(df, max_hold):
    """Forward spot excursion matrices, sized for the longest hold tested."""
    fhs, fls, fcs = [], [], []
    for day, g in df.groupby("day", sort=False):
        c = g["close"].values.astype(float)
        h = g["high"].values.astype(float)
        l = g["low"].values.astype(float)
        n = len(g)
        fh = np.full((n, max_hold), np.nan)
        fl = np.full((n, max_hold), np.nan)
        fc = np.full((n, max_hold), np.nan)
        for k in range(max_hold):
            idx = np.arange(n) + k + 1
            ok = idx < n
            fh[ok, k] = h[idx[ok]] - c[ok]
            fl[ok, k] = l[idx[ok]] - c[ok]
            fc[ok, k] = c[idx[ok]] - c[ok]
        fhs.append(fh); fls.append(fl); fcs.append(fc)
    return np.vstack(fhs), np.vstack(fls), np.vstack(fcs)


def gate_mask(df, thr, edge):
    ce, pe = df["ce_adj"].values, df["pe_adj"].values
    side_ce = ce >= pe
    prob = np.where(side_ce, ce, pe)
    gap = np.abs(ce - pe)
    htf, pvw = df["htf5_dir"].values, df["price_vs_vwap"].values
    ok = (gap >= edge) & (prob >= min(thr, 0.65))
    ok &= ~((side_ce & (htf == -1)) | (~side_ce & (htf == 1)))
    ok &= ~((side_ce & (pvw < -_VWAP_TOL)) | (~side_ce & (pvw > _VWAP_TOL)))
    return ok, side_ce, prob


def score(fh, fl, fc, idxs, side_ce, atr, hold, mult, cap, tgt_r):
    """Vectorless per-trade first touch (few hundred trades — clarity wins)."""
    out = []
    for i in idxs:
        sp = float(stop_pts_for(atr[i], mult, cap))
        tp = sp * tgt_r
        s_sp, s_tp = sp / _DELTA, tp / _DELTA
        ce = side_ce[i]
        res = None
        for k in range(hold):
            hi, lo = fh[i, k], fl[i, k]
            if np.isnan(hi):
                break
            adverse = -lo if ce else hi
            favour = hi if ce else -lo
            if adverse >= s_sp:
                res = -sp
                break
            if favour >= s_tp:
                res = tp
                break
        if res is None:
            last = np.nan
            for k in range(min(hold, fc.shape[1]) - 1, -1, -1):
                if not np.isnan(fc[i, k]):
                    last = fc[i, k]
                    break
            if np.isnan(last):
                continue
            res = (last if ce else -last) * _DELTA
        out.append(res * LOT - COST)
    return np.array(out)


def apply_caps(idxs, days, cooldown_bars):
    keep, cur, n_day, last = [], None, 0, -10 ** 9
    for i in idxs:
        d = days[i]
        if d != cur:
            cur, n_day, last = d, 0, -10 ** 9
        if n_day >= MAX_TRADES or i - last < cooldown_bars:
            continue
        keep.append(i)
        n_day += 1
        last = i
    return np.array(keep, dtype=int)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default=_DEF_SCAN)
    ap.add_argument("--thr", type=float, default=0.42)
    ap.add_argument("--edge", type=float, default=0.04)
    args = ap.parse_args()

    df = pd.read_csv(args.scan)
    holds = [5, 10, 15, 20, 30, 45]
    max_hold = max(holds)
    fh, fl, fc = prep(df, max_hold)
    days = df["day"].values
    atr = df["atr"].values

    ok, side_ce, prob = gate_mask(df, args.thr, args.edge)
    raw = np.flatnonzero(ok)
    print(f"[GEOM] {len(df):,} candles | {df['day'].nunique()} days | "
          f"thr={args.thr} edge={args.edge} -> {len(raw):,} raw signals")
    print(f"       lot={LOT} cost=Rs{COST}/lot max_trades={MAX_TRADES}/day\n")

    rows = []
    for mult, cap in [(0.6, 12.0), (1.0, 16.0), (1.5, 20.0), (2.0, 30.0)]:
        for tgt_r in [0.8, 1.0, 1.5, 2.0, 3.5]:
            for hold in holds:
                idxs = apply_caps(raw, days, max(5, hold))
                pnl = score(fh, fl, fc, idxs, side_ce, atr, hold, mult, cap, tgt_r)
                if len(pnl) == 0:
                    continue
                w = pnl[pnl > 0].sum()
                lo = -pnl[pnl <= 0].sum()
                rows.append({
                    "mult": mult, "cap": cap, "R": tgt_r, "hold": hold,
                    "n": len(pnl), "wr": (pnl > 0).mean(),
                    "pf": (w / lo) if lo > 0 else 99.0,
                    "ev": pnl.mean(), "net": pnl.sum(),
                })
    res = pd.DataFrame(rows).sort_values("net", ascending=False)

    print(f"{'mult':>5} {'cap':>5} {'R':>4} {'hold':>5} {'n':>4} {'WR':>6} "
          f"{'PF':>6} {'EV/tr':>8} {'NET Rs':>10}")
    print("-" * 62)
    for _, r in res.head(25).iterrows():
        print(f"{r['mult']:5.1f} {r['cap']:5.0f} {r['R']:4.1f} {int(r['hold']):5} "
              f"{int(r['n']):4} {r['wr']:6.1%} {r['pf']:6.2f} {r['ev']:8.1f} "
              f"{r['net']:10,.0f}")

    pos = res[res["net"] > 0]
    print(f"\n[GEOM] {len(pos)}/{len(res)} geometry cells net-positive")
    out = os.path.join(_ROOT, "data", "diagnostics", "geometry_sweep.csv")
    res.to_csv(out, index=False)
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
