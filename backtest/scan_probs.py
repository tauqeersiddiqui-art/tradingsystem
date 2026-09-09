# backtest/scan_probs.py
#
# One-pass probability + gate-input scan over a 1-minute OHLC file.
#
# WHY THIS EXISTS
#   The backtest re-derives 35 features and runs two calibrated models per
#   candle. That is the expensive part, and it does NOT depend on
#   CHAMPION_THRESHOLD. Sweeping thresholds by re-running the backtest
#   therefore recomputes identical features N times.
#   This script does the expensive pass ONCE and writes a row per candle:
#       ts, close, atr, ce_prob, pe_prob, ce_adj, pe_adj,
#       htf5_dir, price_vs_vwap, supertrend_dir, adx, day_type
#   Everything downstream (threshold sweeps, gate attribution, EV maths) then
#   reads the CSV in milliseconds.
#
#   It uses BacktestSignalEngine itself for feature building, so the numbers are
#   the SAME ones the backtest and (via _safe_build_live_features) live see.
#
# RUN: python backtest/scan_probs.py [--csv path] [--out path]

import os
import sys
import argparse
from collections import deque

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.backtest_engine import (
    BacktestSignalEngine, BacktestEngine, _MARKET_OPEN, _ORB_END,
    _NO_ENTRY_AFTER,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEF_CSV = os.path.join(_ROOT, "data", "historical", "banknifty_1m_full.csv")
_DEF_OUT = os.path.join(_ROOT, "data", "diagnostics", "bn_prob_scan.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=_DEF_CSV)
    ap.add_argument("--out", default=_DEF_OUT)
    ap.add_argument("--tail", type=int, default=0)
    args = ap.parse_args()

    cfg = BacktestEngine._default_config()
    eng = BacktestEngine(cfg, verbose=False)
    df = eng.load_data(args.csv)
    if args.tail:
        df = df.tail(args.tail)

    se = eng.signal_engine
    rows = []
    prev_close = None
    days = sorted(df["day"].unique())
    print(f"[SCAN] {len(df):,} rows | {len(days)} days")

    for di, d in enumerate(days):
        day_df = df[df["day"] == d].reset_index(drop=True)
        hist = df[df["day"] < d].tail(100)
        se.reset_day(prev_close)
        buf = deque(hist.to_dict("records"), maxlen=300)

        for i in range(len(day_df)):
            row = day_df.iloc[i]
            ts = row["date"]
            now = ts.time()
            buf.append(row.to_dict())
            if now < _MARKET_OPEN:
                continue

            if not se._open_price_set:
                se.learner.set_open_price(row["close"])
                se._open_price_set = True
            se._update_orb(row, ts)
            se._maybe_classify_day(row, ts)

            if now < _ORB_END or now >= _NO_ENTRY_AFTER:
                continue

            window = pd.DataFrame(list(buf))
            feats = se._build_features(window, ts)
            if not feats:
                continue

            try:
                htf5 = se._htf_supertrend_dir(window, tf=5)
            except Exception:
                htf5 = 0

            ce = se.predictor.predict(feats, "CE")
            pe = se.predictor.predict(feats, "PE")
            if ce is None or pe is None:
                continue
            ce_adj, pe_adj = se.learner.get_adjusted_ml_prob(ce, pe, "CE")

            rows.append({
                "ts":             ts,
                "day":            str(d),
                "close":          float(row["close"]),
                "high":           float(row["high"]),
                "low":            float(row["low"]),
                "atr":            float(feats.get("atr", 0.0)),
                "ce_prob":        float(ce),
                "pe_prob":        float(pe),
                "ce_adj":         float(ce_adj),
                "pe_adj":         float(pe_adj),
                "htf5_dir":       int(htf5),
                "price_vs_vwap":  float(feats.get("price_vs_vwap", 0.0)),
                "supertrend_dir": float(feats.get("supertrend_dir", 0.0)),
                "adx":            float(feats.get("adx", 0.0)),
                "day_type":       se.learner.get_day_type(),
            })

        prev_close = float(day_df["close"].iloc[-1])
        if (di + 1) % 20 == 0:
            print(f"  day {di+1}/{len(days)}  rows={len(rows):,}")

    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out.to_csv(args.out, index=False)
    print(f"[SCAN] wrote {len(out):,} rows -> {args.out}")

    # ── Distribution summary — the question the sweep depends on ──────
    for col in ("ce_adj", "pe_adj"):
        s = out[col]
        print(f"  {col}: mean={s.mean():.3f} p50={s.quantile(.50):.3f} "
              f"p90={s.quantile(.90):.3f} p95={s.quantile(.95):.3f} "
              f"p99={s.quantile(.99):.3f} max={s.max():.3f}")
    best = out[["ce_adj", "pe_adj"]].max(axis=1)
    print(f"  max(CE,PE): p50={best.quantile(.50):.3f} p90={best.quantile(.90):.3f} "
          f"p99={best.quantile(.99):.3f} max={best.max():.3f}")
    for t in (0.45, 0.50, 0.55, 0.60, 0.65):
        n = int((best >= t).sum())
        print(f"  >= {t:.2f}: {n:6,} candles ({n/max(len(out),1):.2%}) "
              f"~{n/max(len(days),1):.1f}/day")


if __name__ == "__main__":
    main()
