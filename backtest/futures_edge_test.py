# backtest/futures_edge_test.py
# ---------------------------------------------------------------------------
# DOES THE SIGNAL HAVE ANY EDGE AT ALL?  (futures / index, low friction)
#
# WHY THIS EXISTS:
#   Options are the HARDEST place to find edge: spread + theta + gamma eat
#   small directional edges alive. If your direction model cannot make money
#   on NIFTY FUTURES (tiny friction, linear payoff), it has zero chance on
#   ATM options. This script answers, in one run: "is there a directional
#   edge before option frictions destroy it?"
#
#   It reuses the SAME trained models, features, thresholds and entry gates as
#   walkforward_oos.py, but P&L is measured in SPOT POINTS on a linear
#   futures-style payoff (1 point = LOT_UNITS rupees), with a small, realistic
#   futures cost (a few points round-trip). No option pricing involved.
#
# READING THE RESULT:
#   * Positive expectancy on futures with a meaningful sample (>=100 trades)
#       -> a real directional edge MIGHT exist; options just kill it on cost.
#         Next step: trade futures, or widen targets so options can clear cost.
#   * Negative on futures too
#       -> there is NO directional edge. No option/label tweak will help.
#         Stop optimizing; rethink WHAT you predict (not how you trade it).
#
# RUN:
#   python backtest/futures_edge_test.py
#   FOLDS=4 OOS_START=2024-01-01 python backtest/futures_edge_test.py
# ---------------------------------------------------------------------------

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import lightgbm as lgb
from collections import deque
from datetime import time as dtime
import warnings
warnings.filterwarnings("ignore")

from ml.feature_config import FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM
from ml.indicators import supertrend as _supertrend

DATA = "ml/models/training_dataset_v3.csv"

# ── Sim parameters (mirror walkforward_oos.py entry logic) ───────────
LOOKAHEAD        = 12
TARGET_SPOT_PTS  = float(os.getenv("FUT_TARGET_PTS", "15"))   # take-profit in spot pts
STOP_SPOT_PTS    = float(os.getenv("FUT_STOP_PTS", "15"))     # stop in spot pts
EDGE_MARGIN      = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
VWAP_TOL         = 0.0015
MAX_TRADES_DAY   = int(os.getenv("BT_MAX_TRADES", "6"))
COOLDOWN_S       = int(os.getenv("BT_COOLDOWN", "300"))
LOT_UNITS        = 65
# Futures round-trip cost in SPOT POINTS (brokerage+slippage). NIFTY futures
# frictions are tiny vs options; ~1.5 pts round-trip is conservative.
FUT_COST_PTS     = float(os.getenv("FUT_COST_PTS", "1.5"))
FOLDS            = int(os.getenv("FOLDS", "4"))
OOS_START        = os.getenv("OOS_START", "2024-01-01")
NO_ENTRY_AFTER   = dtime(15, 15)
ORB_END          = dtime(9, 30)

_LGB_PARAMS = dict(
    n_estimators=300, learning_rate=0.03, max_depth=6, num_leaves=31,
    subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
    reg_alpha=0.05, reg_lambda=0.1, verbose=-1, random_state=42, n_jobs=-1,
)


def _train_side(train_df, label_col):
    X = train_df[FEATURE_COLUMNS].values
    y = train_df[label_col].values.astype(int)
    if y.sum() < 50:
        return None
    m = lgb.LGBMClassifier(**_LGB_PARAMS)
    m.fit(pd.DataFrame(X, columns=FEATURE_COLUMNS), y)
    cal = CalibratedLGBM(m)
    cut = int(len(X) * 0.8)
    cal.fit_calibration(pd.DataFrame(X[cut:], columns=FEATURE_COLUMNS), y[cut:])
    cal.feature_names_ = list(FEATURE_COLUMNS)
    return cal


def _htf5_dir(buf):
    if len(buf) < 60:
        return 0
    h = np.array([b["high"] for b in buf], float)
    l = np.array([b["low"] for b in buf], float)
    c = np.array([b["close"] for b in buf], float)
    usable = (len(c) // 5) * 5
    if usable < 60:
        return 0
    h = h[-usable:].reshape(-1, 5).max(axis=1)
    l = l[-usable:].reshape(-1, 5).min(axis=1)
    c = c[-usable:].reshape(-1, 5)[:, -1]
    if len(c) < 12:
        return 0
    d, _ = _supertrend(h, l, c, period=10, multiplier=3.0)
    return int(d[-1])


def _simulate(test_df, warmup_rows, ce_model, pe_model, thr):
    """
    Same entry logic as walkforward_oos, but FUTURES P&L: enter at spot, exit on
    first touch of +TARGET (win) / -STOP (loss) within LOOKAHEAD bars, else exit
    at horizon close. P&L = signed spot move (pts) * LOT_UNITS, minus futures
    cost. One position at a time, cooldown enforced. Returns list of net Rs.
    """
    trades = []
    buf = deque(warmup_rows, maxlen=120)
    cur_day = None
    position = None
    last_exit_ts = None
    trades_today = 0

    feats_arr = test_df[FEATURE_COLUMNS].values
    rows = test_df.to_dict("records")

    for idx, row in enumerate(rows):
        ts = row["date"]
        day = ts.date()
        if day != cur_day:
            cur_day = day
            trades_today = 0
            last_exit_ts = None
            position = None
        buf.append(row)
        now = ts.time()

        # ── manage open futures position (first-touch barrier) ──
        if position is not None:
            es = position["entry_spot"]; sd = position["side"]
            fav = es + TARGET_SPOT_PTS if sd == "CE" else es - TARGET_SPOT_PTS
            adv = es - STOP_SPOT_PTS   if sd == "CE" else es + STOP_SPOT_PTS
            fav_hit = row["high"] >= fav if sd == "CE" else row["low"]  <= fav
            adv_hit = row["low"]  <= adv if sd == "CE" else row["high"] >= adv
            held = idx - position["entry_idx"]
            exit_spot = None
            # same-bar tie -> adverse wins (conservative; intrabar path unknown)
            if adv_hit:
                exit_spot = adv
            elif fav_hit:
                exit_spot = fav
            elif held >= LOOKAHEAD:
                exit_spot = row["close"]
            if exit_spot is not None:
                move = (exit_spot - es) if sd == "CE" else (es - exit_spot)
                net = (move - FUT_COST_PTS) * LOT_UNITS
                trades.append(net)
                last_exit_ts = ts
                position = None
            continue

        # ── entry gates ──
        if now < ORB_END or now >= NO_ENTRY_AFTER:
            continue
        if trades_today >= MAX_TRADES_DAY:
            continue
        if last_exit_ts is not None and (ts - last_exit_ts).total_seconds() < COOLDOWN_S:
            continue

        # ── PREDICT-FIRST (identical to walkforward_oos) ──
        X = feats_arr[idx:idx + 1]
        ce_p = float(ce_model.predict_proba(pd.DataFrame(X, columns=FEATURE_COLUMNS))[0][1])
        pe_p = float(pe_model.predict_proba(pd.DataFrame(X, columns=FEATURE_COLUMNS))[0][1])
        side = "CE" if ce_p >= pe_p else "PE"
        prob = ce_p if side == "CE" else pe_p
        if abs(ce_p - pe_p) < EDGE_MARGIN:
            continue
        if prob < thr:
            continue
        htf5 = _htf5_dir(buf)
        if (side == "CE" and htf5 == -1) or (side == "PE" and htf5 == 1):
            continue
        pvwap = float(row.get("price_vs_vwap", 0.0))
        if (side == "CE" and pvwap < -VWAP_TOL) or (side == "PE" and pvwap > VWAP_TOL):
            continue

        position = {"side": side, "entry_spot": float(row["close"]), "entry_idx": idx}
        trades_today += 1

    return trades


def _metrics(pnls):
    if not pnls:
        return {"trades": 0}
    a = np.array(pnls, float)
    wins = a[a > 0]; losses = a[a <= 0]
    return {
        "trades": len(a),
        "net_pnl": round(float(a.sum()), 0),
        "expectancy_per_trade": round(float(a.mean()), 1),
        "win_rate": round(float((a > 0).mean()), 3),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() < 0 else float("inf"),
    }


def main():
    print("=" * 70)
    print("  FUTURES EDGE TEST  (linear payoff, low friction — does edge EXIST?)")
    print(f"  target={TARGET_SPOT_PTS}pt stop={STOP_SPOT_PTS}pt cost={FUT_COST_PTS}pt/RT")
    print("=" * 70)
    df = pd.read_csv(DATA)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0

    oos = df[df["date"] >= pd.Timestamp(OOS_START)].reset_index(drop=True)
    bounds = np.linspace(0, len(oos), FOLDS + 1, dtype=int)

    THRESHOLDS = [0.50, 0.60, 0.70, 0.79, 0.85, 0.90]
    pnls_by_thr = {t: [] for t in THRESHOLDS}

    for k in range(FOLDS):
        lo, hi = bounds[k], bounds[k + 1]
        if hi - lo < 500:
            continue
        test_fold = oos.iloc[lo:hi].reset_index(drop=True)
        test_start = test_fold["date"].iloc[0]
        embargo_cut = df["date"].searchsorted(test_start) - LOOKAHEAD
        train = df.iloc[:max(embargo_cut, 0)]
        if len(train) < 20000:
            print(f"  Fold {k+1}: insufficient train ({len(train)}), skipping")
            continue

        ce_m = _train_side(train, "label_ce")
        pe_m = _train_side(train, "label_pe")
        if ce_m is None or pe_m is None:
            print(f"  Fold {k+1}: training failed, skipping")
            continue

        warmup = df.iloc[max(embargo_cut, 0) - 120:max(embargo_cut, 0)].to_dict("records")
        line = [f"  Fold {k+1}  {test_start.date()}..{test_fold['date'].iloc[-1].date()}"]
        for t in THRESHOLDS:
            pnls = _simulate(test_fold, warmup, ce_m, pe_m, thr=t)
            pnls_by_thr[t].extend(pnls)
            m = _metrics(pnls)
            line.append(f"thr{t:.2f}:n={m.get('trades',0)},exp={m.get('expectancy_per_trade',0)}")
        print("  | ".join(line))

    print("\n" + "=" * 70)
    print("  AGGREGATE FUTURES EDGE BY THRESHOLD (all folds, after futures cost)")
    print("=" * 70)
    print(f"  {'thr':>5} {'trades':>7} {'win%':>6} {'exp/trade':>10} {'PF':>6} {'net_pnl':>10}")
    MIN_TRADES = int(os.getenv("MIN_TRADES_VERDICT", "100"))
    any_edge = False
    for t in THRESHOLDS:
        m = _metrics(pnls_by_thr[t])
        if m.get("trades", 0) == 0:
            print(f"  {t:>5.2f} {'0':>7}  (no trades)")
            continue
        tag = "" if m["trades"] >= MIN_TRADES else "  <- sample too small (noise)"
        print(f"  {t:>5.2f} {m['trades']:>7} {m['win_rate']*100:>5.1f}% "
              f"{m['expectancy_per_trade']:>10} {m['profit_factor']:>6} "
              f"{m['net_pnl']:>10}{tag}")
        if m["trades"] >= MIN_TRADES and m["expectancy_per_trade"] > 0:
            any_edge = True

    print()
    if any_edge:
        print("  VERDICT: A directional edge EXISTS on futures (>=100 trades, +EV).")
        print("           Options likely kill it on cost. Consider trading futures,")
        print("           or widen targets so options can clear spread+theta.")
    else:
        print("  VERDICT: NO directional edge even on low-friction futures.")
        print("           No option/label/stop tweak will help. The SIGNAL itself")
        print("           has no edge — rethink WHAT you predict, not how you trade it.")
    print("=" * 70)


if __name__ == "__main__":
    main()
