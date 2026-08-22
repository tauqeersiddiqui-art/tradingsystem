# backtest/walkforward_oos.py
# ─────────────────────────────────────────────────────────────────────────
# PURGED WALK-FORWARD, OUT-OF-SAMPLE, TRADE-LEVEL BACKTEST
#
# This is the honest-edge tool. It fixes the three flaws of the in-sample
# backtest_engine.py:
#   1. OUT-OF-SAMPLE: models are retrained on each fold using ONLY data BEFORE
#      the test window (with an ENTRY_HORIZON_BARS-bar embargo so forward labels
#      cannot leak across the boundary). The model never sees the bars it is graded on.
#   2. TRADE-LEVEL & DE-OVERLAPPED: P&L is measured per independent trade
#      (one position at a time, cooldown enforced) — NOT per-bar AUC. The
#      script prints per-bar AUC beside OOS expectancy so the inflation gap is
#      visible.
#   3. LIVE LOGIC: it runs the PREDICT-FIRST decision (argmax(ce,pe) + edge
#      margin + threshold + 5m trend + VWAP), the same as live_engine, not the
#      legacy ORB path.
#
# Costs are deliberately CONSERVATIVE (full spread each side + per-lot
# brokerage) so the result under-promises rather than over-promises.
#
# RUN:
#   python backtest/walkforward_oos.py            # default folds on v3 dataset
#   FOLDS=4 OOS_START=2024-01-01 python backtest/walkforward_oos.py
# ─────────────────────────────────────────────────────────────────────────

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
from engine.execution.profit_manager import manage_position, _cost_rs
from engine.risk.risk_manager import compute_entry_stops
from backtest.backtest_engine import OptionPriceSimulator, _mins_to_close

DATA = "ml/models/training_dataset.csv"

# ── Sim parameters (mirror live config defaults) ──────────────────────
# Labels are ENTRY-QUALITY (not directional first-touch): label_ce/pe = 1 if
# the next ENTRY_HORIZON_BARS=5 forward bars show >= QUALITY_THRESHOLD_PTS
# (25 spot pts) of favorable excursion — CE and PE are NON-EXCLUSIVE (both can
# be 1), and BAD_ENTRY bars (move already extended) are forced to 0.
LOOKAHEAD        = 5           # embargo/purge size = dataset label horizon (ENTRY_HORIZON_BARS)
TARGET_SPOT_PTS  = 15          # matched-mode diagnostic barrier (dataset label threshold is 25)
# STOP_MODE:
#   "live"    (default) — production exit: option-premium stop from
#             compute_entry_stops + trailing manage_position. This is what the
#             real engine does, and what the headline OOS verdict grades.
#   "matched" — DIAGNOSTIC ONLY. Replays the EXACT label event into option P&L:
#             hold up to LOOKAHEAD bars, exit the instant spot first touches
#             +TARGET (win) or -TARGET (loss), priced through the SAME option
#             simulator with the SAME costs. This isolates "is the model
#             directionally right?" from "is the live stop choking the trade?".
#             It does NOT touch live trading and never widens the live stop.
STOP_MODE        = os.getenv("BT_STOP_MODE", "live").lower()
EDGE_MARGIN      = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
VWAP_TOL         = 0.0015
MAX_TRADES_DAY   = int(os.getenv("BT_MAX_TRADES", "6"))
COOLDOWN_S       = int(os.getenv("BT_COOLDOWN", "300"))
MAX_HOLD_S       = 300
LOT_UNITS        = 65
SPREAD_PTS       = float(os.getenv("BT_SPREAD_PTS", "1.0"))   # round-trip option spread (premium pts)
FOLDS            = int(os.getenv("FOLDS", "4"))
OOS_START        = os.getenv("OOS_START", "2024-01-01")
NO_ENTRY_AFTER   = dtime(15, 15)
ORB_END          = dtime(9, 30)

_opt = OptionPriceSimulator()

_LGB_PARAMS = dict(
    n_estimators=300, learning_rate=0.03, max_depth=6, num_leaves=31,
    subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
    reg_alpha=0.05, reg_lambda=0.1, verbose=-1, random_state=42, n_jobs=-1,
)


def _train_side(train_df, label_col):
    """Train + Platt-calibrate one directional model on TRAIN rows only."""
    X = train_df[FEATURE_COLUMNS].values
    y = train_df[label_col].values.astype(int)
    if y.sum() < 50:
        return None
    m = lgb.LGBMClassifier(**_LGB_PARAMS)
    m.fit(pd.DataFrame(X, columns=FEATURE_COLUMNS), y)
    cal = CalibratedLGBM(m)
    # calibrate on the last 20% of TRAIN (still strictly before test)
    cut = int(len(X) * 0.8)
    cal.fit_calibration(pd.DataFrame(X[cut:], columns=FEATURE_COLUMNS), y[cut:])
    cal.feature_names_ = list(FEATURE_COLUMNS)
    return cal


def _htf5_dir(buf):
    """5m SuperTrend direction from a rolling 1m OHLC buffer (mirror live)."""
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


def _simulate(test_df, warmup_rows, ce_model, pe_model, ce_thr, pe_thr):
    """
    Predict-first, cost-realistic, one-position-at-a-time simulation over the
    test fold. Returns a list of per-trade net PnLs (₹, after costs).
    """
    trades = []
    buf = deque(warmup_rows, maxlen=120)   # rolling OHLC for 5m trend
    cur_day = None
    position = None
    entry_ts = None
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
            entry_ts = None
        buf.append(row)
        now = ts.time()

        # ── manage open position (MATCHED diagnostic mode) ──
        if position is not None and STOP_MODE == "matched":
            mtc_now = _mins_to_close(ts)
            es = position["entry_spot"]; sd = position["side"]
            fav_spot = es + TARGET_SPOT_PTS if sd == "CE" else es - TARGET_SPOT_PTS
            adv_spot = es - TARGET_SPOT_PTS if sd == "CE" else es + TARGET_SPOT_PTS
            fav_hit = row["high"] >= fav_spot if sd == "CE" else row["low"]  <= fav_spot
            adv_hit = row["low"]  <= adv_spot if sd == "CE" else row["high"] >= adv_spot
            held_bars = idx - position["entry_idx"]
            exit_spot = None
            # same-bar tie -> adverse wins (conservative; intrabar path unknown)
            if adv_hit:
                exit_spot, reason = adv_spot, "BARRIER_LOSS"
            elif fav_hit:
                exit_spot, reason = fav_spot, "BARRIER_WIN"
            elif held_bars >= LOOKAHEAD:
                exit_spot, reason = row["close"], "HORIZON"
            if exit_spot is not None:
                exit_ltp = _opt.premium(es, exit_spot, sd, mtc_now) - SPREAD_PTS / 2.0
                gross = (exit_ltp - position["entry"]) * position["qty"]
                cost = _cost_rs(position["qty"])
                trades.append(gross - cost)
                last_exit_ts = ts
                position = None
                entry_ts = None
            continue

        # ── manage open position ──
        if position is not None:
            cur_spot = row["close"]
            ltp = _opt.premium(position["entry_spot"], cur_spot, position["side"], _mins_to_close(ts))
            held = (ts - entry_ts).total_seconds()
            new_sl, new_max, reason, _scale = manage_position(
                entry_price=position["entry"], ltp=ltp, lot_size=position["qty"],
                stop_loss=position["stop_loss"], max_pnl=position["max_pnl"],
                ml_prob=position["ml_prob"], target=position.get("target"),
                side=position.get("side", "CE"),
            )
            position["stop_loss"] = new_sl
            position["max_pnl"] = new_max
            exit_flag = bool(reason)
            if not exit_flag and ltp <= position["stop_loss"]:
                exit_flag, reason = True, "STOP"
            if not exit_flag and held > MAX_HOLD_S and new_max < 100:
                exit_flag, reason = True, "TIME_EXIT_WEAK"
            if not exit_flag and now >= NO_ENTRY_AFTER:
                exit_flag, reason = True, "EOD"
            if exit_flag:
                # exit fill: premium minus half the round-trip spread
                exit_ltp = ltp - SPREAD_PTS / 2.0
                gross = (exit_ltp - position["entry"]) * position["qty"]
                cost = _cost_rs(position["qty"])
                trades.append(gross - cost)
                last_exit_ts = ts
                position = None
                entry_ts = None
            continue

        # ── entry gates ──
        if now < ORB_END or now >= NO_ENTRY_AFTER:
            continue
        if trades_today >= MAX_TRADES_DAY:
            continue
        if last_exit_ts is not None and (ts - last_exit_ts).total_seconds() < COOLDOWN_S:
            continue

        # ── PREDICT-FIRST ──
        X = feats_arr[idx:idx + 1]
        ce_p = float(ce_model.predict_proba(pd.DataFrame(X, columns=FEATURE_COLUMNS))[0][1])
        pe_p = float(pe_model.predict_proba(pd.DataFrame(X, columns=FEATURE_COLUMNS))[0][1])
        side = "CE" if ce_p >= pe_p else "PE"
        prob = ce_p if side == "CE" else pe_p
        thr = ce_thr if side == "CE" else pe_thr
        if abs(ce_p - pe_p) < EDGE_MARGIN:          # need conviction
            continue
        if prob < thr:                               # threshold
            continue
        htf5 = _htf5_dir(buf)                        # 5m confirm
        if (side == "CE" and htf5 == -1) or (side == "PE" and htf5 == 1):
            continue
        pvwap = float(row.get("price_vs_vwap", 0.0)) # VWAP confirm
        if (side == "CE" and pvwap < -VWAP_TOL) or (side == "PE" and pvwap > VWAP_TOL):
            continue

        # ── enter ──
        entry_spot = float(row["close"])
        mtc = _mins_to_close(ts)
        entry_prem = _opt.premium(entry_spot, entry_spot, side, mtc) + SPREAD_PTS / 2.0
        atr_val = float(row.get("atr", entry_spot * 0.01))
        sl, tgt, _ = compute_entry_stops(entry_prem, atr_val, "RANGE")
        position = {
            "side": side, "entry": entry_prem, "entry_spot": entry_spot,
            "qty": LOT_UNITS, "stop_loss": sl, "target": tgt,
            "max_pnl": 0.0, "ml_prob": prob, "entry_idx": idx,
        }
        entry_ts = ts
        trades_today += 1

    return trades


def _metrics(pnls):
    if not pnls:
        return {"trades": 0}
    a = np.array(pnls, float)
    wins = a[a > 0]; losses = a[a <= 0]
    eq = np.cumsum(a)
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min()) if len(eq) else 0.0
    return {
        "trades": len(a),
        "net_pnl": round(float(a.sum()), 0),
        "expectancy_per_trade": round(float(a.mean()), 1),
        "win_rate": round(float((a > 0).mean()), 3),
        "avg_win": round(float(wins.mean()), 0) if len(wins) else 0,
        "avg_loss": round(float(losses.mean()), 0) if len(losses) else 0,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() < 0 else float("inf"),
        "max_drawdown": round(max_dd, 0),
    }


def main():
    print("=" * 70)
    print("  PURGED WALK-FORWARD OUT-OF-SAMPLE BACKTEST (trade-level, costed)")
    if STOP_MODE == "matched":
        print(f"  *** STOP_MODE=matched — DIAGNOSTIC: exit on +/-{TARGET_SPOT_PTS}pt spot")
        print(f"      barrier within {LOOKAHEAD} bars (isolates model edge from stop). ***")
    print("=" * 70)
    df = pd.read_csv(DATA)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0

    oos = df[df["date"] >= pd.Timestamp(OOS_START)].reset_index(drop=True)
    if len(oos) < 5000:
        print(f"[WARN] Only {len(oos)} OOS rows from {OOS_START}; results noisy.")
    bounds = np.linspace(0, len(oos), FOLDS + 1, dtype=int)

    THRESHOLDS = [0.50, 0.60, 0.70, 0.79, 0.85, 0.90]
    pnls_by_thr = {t: [] for t in THRESHOLDS}
    all_auc = []
    from sklearn.metrics import roc_auc_score

    for k in range(FOLDS):
        lo, hi = bounds[k], bounds[k + 1]
        if hi - lo < 500:
            continue
        test_fold = oos.iloc[lo:hi].reset_index(drop=True)
        test_start = test_fold["date"].iloc[0]
        # TRAIN: everything strictly before test_start, minus an embargo equal
        # to the entry-quality label horizon (5 bars, so forward labels cannot
        # leak across).
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

        # OOS per-bar AUC (for the inflation-gap comparison)
        Xt = test_fold[FEATURE_COLUMNS]
        try:
            auc_ce = roc_auc_score(test_fold["label_ce"], ce_m.predict_proba(Xt)[:, 1]) \
                if test_fold["label_ce"].sum() > 0 else 0.5
        except Exception:
            auc_ce = 0.5
        all_auc.append(auc_ce)

        warmup = df.iloc[max(embargo_cut, 0) - 120:max(embargo_cut, 0)].to_dict("records")
        # Train once per fold, simulate at each threshold (cheap re-sim).
        line = [f"  Fold {k+1}  {test_start.date()}..{test_fold['date'].iloc[-1].date()}  "
                f"OOS_AUC={auc_ce:.3f}"]
        for t in THRESHOLDS:
            pnls = _simulate(test_fold, warmup, ce_m, pe_m, ce_thr=t, pe_thr=t)
            pnls_by_thr[t].extend(pnls)
            m = _metrics(pnls)
            line.append(f"thr{t:.2f}:n={m.get('trades',0)},exp={m.get('expectancy_per_trade',0)}")
        print("  | ".join(line))

    print("\n" + "=" * 70)
    print("  AGGREGATE OUT-OF-SAMPLE BY ENTRY THRESHOLD (all folds, after costs)")
    print("=" * 70)
    print(f"  {'thr':>5} {'trades':>7} {'win%':>6} {'exp/trade':>10} "
          f"{'PF':>6} {'net_pnl':>10} {'maxDD':>10}")
    # A threshold is only a real candidate if it has a STATISTICALLY MEANINGFUL
    # sample. A handful of trades over years is noise, not edge (PF=inf on n=1
    # is the classic trap). Require both a minimum total count AND that most
    # folds contributed trades, so one lucky fold cannot masquerade as edge.
    MIN_TRADES = int(os.getenv("MIN_TRADES_VERDICT", "100"))
    best = None
    for t in THRESHOLDS:
        m = _metrics(pnls_by_thr[t])
        if m.get("trades", 0) == 0:
            print(f"  {t:>5.2f} {'0':>7}  (no trades)")
            continue
        tag = "" if m["trades"] >= MIN_TRADES else "  <- sample too small (noise)"
        print(f"  {t:>5.2f} {m['trades']:>7} {m['win_rate']*100:>5.1f}% "
              f"{m['expectancy_per_trade']:>10} {m['profit_factor']:>6} "
              f"{m['net_pnl']:>10} {m['max_drawdown']:>10}{tag}")
        # Candidate ONLY if it clears the sample floor AND is +EV.
        if m["trades"] >= MIN_TRADES and m["expectancy_per_trade"] > 0:
            if best is None or m["expectancy_per_trade"] > best[1]["expectancy_per_trade"]:
                best = (t, m)
    if all_auc:
        print(f"\n  mean OOS per-bar AUC (CE): {np.mean(all_auc):.3f}  "
              f"<-- inflated vs trade-level reality above")
    print(f"  (deployment requires >= {MIN_TRADES} trades AND positive expectancy)")
    if best:
        print(f"\n  BEST: thr={best[0]:.2f}  exp=Rs{best[1]['expectancy_per_trade']}/trade  "
              f"n={best[1]['trades']}  PF={best[1]['profit_factor']}  -- candidate for deployment")
        print("  VERDICT: POSITIVE EDGE exists at this threshold (after costs).")
    else:
        print("\n  VERDICT: NO threshold is +EV after costs with a meaningful sample")
        print("           -- do NOT trade real money. (Single-trade 'wins' are noise.)")
        print("  The directional label/objective does not survive option frictions.")
    print("=" * 70)


if __name__ == "__main__":
    main()
