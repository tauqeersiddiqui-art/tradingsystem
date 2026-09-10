# backtest/sweep_gates.py
#
# Threshold / edge-margin sweep over the CACHED probability scan.
#
# Reads data/diagnostics/bn_prob_scan.csv (produced by scan_probs.py) and, for
# every (CHAMPION_THRESHOLD, ML_EDGE_MARGIN) pair, replays the live gate chain:
#     side   = argmax(ce_adj, pe_adj)
#     edge   = |ce_adj - pe_adj| >= ML_EDGE_MARGIN
#     prob   >= min(threshold, 0.65)
#     HTF5 SuperTrend must not oppose
#     price_vs_vwap must not oppose (tol 0.0015)
# then applies the session caps live uses (MAX_TRADES_PER_DAY, re-entry
# cooldown) and scores each accepted candle in PREMIUM space with the SAME
# geometry risk_manager will place:
#     stop_pts   = premium_stop_pts(atr)          (delta 0.5, mult 1.5, cap 20)
#     target_pts = stop_pts * TARGET_R
#     hold       = MAX_HOLD_SECONDS / 60 bars, then mark-to-market
# Spot->premium uses delta 0.5, which is what compute_entry_stops assumes.
#
# WHY NOT JUST RE-RUN THE BACKTEST N TIMES
#   Feature building + 4 model calls per candle dominates the runtime and does
#   not depend on either knob. This reads the cache, so a 40-cell sweep takes
#   seconds instead of hours. The winning cell is then confirmed by the real
#   backtest_engine run.
#
# RUN: python backtest/sweep_gates.py

import os
import sys
import argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.risk.risk_manager import premium_stop_pts, _TARGET_R
from engine.config.config import Config as _LiveConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEF_SCAN = os.path.join(_ROOT, "data", "diagnostics", "bn_prob_scan.csv")

_CFG = _LiveConfig()
LOT = int(getattr(_CFG, "LOT_SIZE", 30) or 30)
COST = float(getattr(_CFG, "COST_PER_LOT", 66.0) or 66.0)
MAX_TRADES = int(getattr(_CFG, "MAX_TRADES_PER_DAY", 4) or 4)
COOLDOWN_BARS = int(getattr(_CFG, "REENTRY_COOLDOWN", 300) or 300) // 60
HOLD_BARS = max(1, int(getattr(_CFG, "MAX_HOLD_SECONDS", 300) or 300) // 60)
_VWAP_TOL = 0.0015
_DELTA = 0.5


def score_trade(fwd_h, fwd_l, fwd_c, side, stop_pts, target_pts):
    """
    Premium-space first-touch over HOLD_BARS, delta-converted from spot.
    Returns gross premium points (before cost).

    CE: premium rises with spot  -> stop hit on spot DOWN move, target on UP
    PE: premium rises as spot falls -> mirrored
    """
    sp_stop = stop_pts / _DELTA
    sp_tgt  = target_pts / _DELTA
    for k in range(len(fwd_c)):
        if side == "CE":
            adverse = fwd_l[k]      # lowest spot in bar
            favour  = fwd_h[k]
            hit_stop = adverse <= -sp_stop
            hit_tgt  = favour  >= sp_tgt
        else:
            adverse = fwd_h[k]
            favour  = fwd_l[k]
            hit_stop = adverse >= sp_stop
            hit_tgt  = favour  <= -sp_tgt
        # Stop first when both touch in the same bar — conservative.
        if hit_stop:
            return -stop_pts
        if hit_tgt:
            return target_pts
    move = fwd_c[-1]
    return (move if side == "CE" else -move) * _DELTA


def build_forward(df):
    """Per-day forward spot excursions relative to the entry close."""
    out = []
    for day, g in df.groupby("day", sort=True):
        g = g.reset_index(drop=True)
        c = g["close"].values.astype(float)
        h = g["high"].values.astype(float)
        l = g["low"].values.astype(float)
        n = len(g)
        fh = np.full((n, HOLD_BARS), np.nan)
        fl = np.full((n, HOLD_BARS), np.nan)
        fc = np.full((n, HOLD_BARS), np.nan)
        for k in range(HOLD_BARS):
            idx = np.arange(n) + k + 1
            ok = idx < n
            fh[ok, k] = h[idx[ok]] - c[ok]
            fl[ok, k] = l[idx[ok]] - c[ok]
            fc[ok, k] = c[idx[ok]] - c[ok]
        g["_fh"] = list(fh)
        g["_fl"] = list(fl)
        g["_fc"] = list(fc)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def run_cell(df, thr, edge, verbose=False):
    thr_eff = min(thr, 0.65)
    ce = df["ce_adj"].values
    pe = df["pe_adj"].values
    side_ce = ce >= pe
    prob = np.where(side_ce, ce, pe)
    gap = np.abs(ce - pe)
    htf = df["htf5_dir"].values
    pvw = df["price_vs_vwap"].values

    ok = (gap >= edge) & (prob >= thr_eff)
    ok &= ~((side_ce & (htf == -1)) | (~side_ce & (htf == 1)))
    ok &= ~((side_ce & (pvw < -_VWAP_TOL)) | (~side_ce & (pvw > _VWAP_TOL)))

    trades = []
    cur_day = None
    n_day = 0
    last_i = -10 ** 9
    for i in np.flatnonzero(ok):
        d = df["day"].values[i]
        if d != cur_day:
            cur_day, n_day, last_i = d, 0, -10 ** 9
        if n_day >= MAX_TRADES:
            continue
        if i - last_i < COOLDOWN_BARS:
            continue
        fc = df["_fc"].values[i]
        if np.isnan(fc).all():
            continue
        fh = np.nan_to_num(df["_fh"].values[i], nan=0.0)
        fl = np.nan_to_num(df["_fl"].values[i], nan=0.0)
        fc = np.nan_to_num(fc, nan=0.0)
        atr = float(df["atr"].values[i])
        stop_pts = premium_stop_pts(atr)
        tgt_pts = stop_pts * _TARGET_R
        side = "CE" if side_ce[i] else "PE"
        gross_pts = score_trade(fh, fl, fc, side, stop_pts, tgt_pts)
        gross = gross_pts * LOT
        net = gross - COST
        trades.append({"day": d, "side": side, "prob": prob[i], "stop": stop_pts,
                       "gross": gross, "net": net})
        n_day += 1
        last_i = i

    if not trades:
        return {"thr": thr, "edge": edge, "n": 0, "net": 0.0, "wr": 0.0,
                "pf": 0.0, "ev": 0.0, "ce_pct": 0.0, "gross": 0.0,
                "raw": int(ok.sum())}
    t = pd.DataFrame(trades)
    wins = t[t["net"] > 0]["net"].sum()
    loss = -t[t["net"] <= 0]["net"].sum()
    return {"thr": thr, "edge": edge, "n": len(t),
            "net": round(t["net"].sum(), 0),
            "gross": round(t["gross"].sum(), 0),
            "wr": round((t["net"] > 0).mean(), 3),
            "pf": round(wins / loss, 2) if loss > 0 else 99.0,
            "ev": round(t["net"].mean(), 1),
            "ce_pct": round((t["side"] == "CE").mean(), 2),
            "raw": int(ok.sum())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default=_DEF_SCAN)
    args = ap.parse_args()

    df = pd.read_csv(args.scan)
    df["ts"] = pd.to_datetime(df["ts"])
    print(f"[SWEEP] {len(df):,} candles | {df['day'].nunique()} days "
          f"| lot={LOT} cost=Rs{COST}/lot max_trades={MAX_TRADES} "
          f"cooldown={COOLDOWN_BARS}bar hold={HOLD_BARS}bar target={_TARGET_R}R")
    df = build_forward(df)

    thrs = [0.42, 0.45, 0.48, 0.50, 0.52, 0.55]
    edges = [0.02, 0.04, 0.06, 0.08, 0.10, 0.15]

    rows = []
    for thr in thrs:
        for edge in edges:
            rows.append(run_cell(df, thr, edge))
    res = pd.DataFrame(rows)

    print(f"\n{'thr':>5} {'edge':>5} {'raw':>6} {'n':>4} {'WR':>6} {'PF':>6} "
          f"{'EV/tr':>8} {'NET Rs':>10} {'CE%':>5}")
    print("-" * 66)
    for _, r in res.iterrows():
        flag = "  <<<" if r["n"] >= 20 and r["net"] > 0 else ""
        print(f"{r['thr']:5.2f} {r['edge']:5.2f} {int(r['raw']):6,} {int(r['n']):4} "
              f"{r['wr']:6.1%} {r['pf']:6.2f} {r['ev']:8.1f} {r['net']:10,.0f} "
              f"{r['ce_pct']:5.0%}{flag}")

    good = res[(res["n"] >= 20) & (res["net"] > 0)].sort_values("net", ascending=False)
    print(f"\n[SWEEP] {len(good)} net-positive cells with n>=20 trades")
    if len(good):
        b = good.iloc[0]
        print(f"  BEST: thr={b['thr']:.2f} edge={b['edge']:.2f} "
              f"n={int(b['n'])} WR={b['wr']:.1%} PF={b['pf']:.2f} "
              f"EV=Rs{b['ev']:.1f}/trade NET=Rs{b['net']:,.0f}")
    out = os.path.join(_ROOT, "data", "diagnostics", "gate_sweep.csv")
    res.to_csv(out, index=False)
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
