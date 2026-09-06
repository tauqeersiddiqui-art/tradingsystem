# backtest/sweep_confirm.py
#
# Robustness check on the geometry_sweep winner. A 120-cell sweep over 371
# signals will find SOMETHING positive by chance — this asks whether the
# winner survives (a) a chronological split, (b) neighbouring parameter
# values, and (c) a re-sweep of thr/edge at the new geometry.
#
# A parameter set that only works on one half of the days, or that collapses
# when a knob moves one notch, is curve-fit and must not be deployed.
#
# RUN: python backtest/sweep_confirm.py

import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.sweep_geometry import (
    prep, gate_mask, score, apply_caps, LOT, COST, MAX_TRADES, _DEF_SCAN,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cell(df, fh, fl, fc, days, atr, thr, edge, mult, cap, tgt_r, hold,
         day_subset=None):
    ok, side_ce, _ = gate_mask(df, thr, edge)
    if day_subset is not None:
        ok &= np.isin(days, day_subset)
    raw = np.flatnonzero(ok)
    idxs = apply_caps(raw, days, max(5, hold))
    pnl = score(fh, fl, fc, idxs, side_ce, atr, hold, mult, cap, tgt_r)
    if len(pnl) == 0:
        return None
    w = pnl[pnl > 0].sum()
    lo = -pnl[pnl <= 0].sum()
    return {"n": len(pnl), "wr": (pnl > 0).mean(),
            "pf": (w / lo) if lo > 0 else 99.0,
            "ev": pnl.mean(), "net": pnl.sum()}


def show(tag, r):
    if r is None:
        print(f"  {tag:<34} no trades")
        return
    print(f"  {tag:<34} n={r['n']:4d} WR={r['wr']:5.1%} PF={r['pf']:5.2f} "
          f"EV={r['ev']:+7.1f} NET={r['net']:+9,.0f}")


def main():
    df = pd.read_csv(_DEF_SCAN)
    holds = 45
    fh, fl, fc = prep(df, holds)
    days = df["day"].values
    atr = df["atr"].values
    uniq = sorted(pd.unique(days))
    h1, h2 = uniq[:len(uniq) // 2], uniq[len(uniq) // 2:]

    BEST = dict(thr=0.42, edge=0.04, mult=1.0, cap=16.0, tgt_r=3.5, hold=20)
    print("=" * 72)
    print("  ROBUSTNESS: geometry_sweep winner")
    print(f"  {BEST}")
    print("=" * 72)

    print("\n[1] CHRONOLOGICAL SPLIT  (23 days / 24 days)")
    show("full 47 days", cell(df, fh, fl, fc, days, atr, **BEST))
    show(f"first half  ({h1[0]}..{h1[-1]})",
         cell(df, fh, fl, fc, days, atr, **BEST, day_subset=h1))
    show(f"second half ({h2[0]}..{h2[-1]})",
         cell(df, fh, fl, fc, days, atr, **BEST, day_subset=h2))

    print("\n[2] PARAMETER NEIGHBOURHOOD  (one knob at a time)")
    for hold in (10, 15, 20, 25, 30):
        show(f"hold={hold}", cell(df, fh, fl, fc, days, atr,
                                  **{**BEST, "hold": hold}))
    print()
    for r in (2.0, 2.5, 3.0, 3.5, 4.0):
        show(f"target_R={r}", cell(df, fh, fl, fc, days, atr,
                                   **{**BEST, "tgt_r": r}))
    print()
    for mult, cap in ((0.8, 14.0), (1.0, 16.0), (1.2, 18.0), (1.5, 20.0)):
        show(f"SL mult={mult} cap={cap}",
             cell(df, fh, fl, fc, days, atr,
                  **{**BEST, "mult": mult, "cap": cap}))

    print("\n[3] THRESHOLD x EDGE RE-SWEEP AT THE NEW GEOMETRY")
    print(f"  {'thr':>5} {'edge':>5} {'n':>4} {'WR':>6} {'PF':>6} {'EV':>8} {'NET':>10}")
    print("  " + "-" * 50)
    grid = []
    for thr in (0.38, 0.40, 0.42, 0.45, 0.48, 0.50):
        for edge in (0.02, 0.04, 0.06, 0.08, 0.10):
            r = cell(df, fh, fl, fc, days, atr,
                     **{**BEST, "thr": thr, "edge": edge})
            if r is None:
                continue
            grid.append({"thr": thr, "edge": edge, **r})
            print(f"  {thr:5.2f} {edge:5.2f} {r['n']:4d} {r['wr']:6.1%} "
                  f"{r['pf']:6.2f} {r['ev']:8.1f} {r['net']:10,.0f}")
    g = pd.DataFrame(grid)
    pos = g[g["net"] > 0]
    print(f"\n  {len(pos)}/{len(g)} cells net-positive at this geometry")
    if len(pos):
        print(f"  positive region: thr {pos['thr'].min():.2f}-{pos['thr'].max():.2f}, "
              f"edge {pos['edge'].min():.2f}-{pos['edge'].max():.2f}")
        print(f"  median EV across positive cells: Rs{pos['ev'].median():.1f}/trade")
    out = os.path.join(_ROOT, "data", "diagnostics", "confirm_sweep.csv")
    g.to_csv(out, index=False)
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
