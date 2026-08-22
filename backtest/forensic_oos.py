# backtest/forensic_oos.py
# -------------------------------------------------------------------------
# TRADE-LEVEL FORENSIC ANALYSIS -- OOS ML Stack
#
# PURPOSE: Identify WHY expectancy is negative after costs.
# Does NOT retrain or change anything. Diagnosis only.
#
# RUN:
#   python backtest/forensic_oos.py
#   python backtest/forensic_oos.py > forensic_report.txt 2>&1
# -------------------------------------------------------------------------

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import lightgbm as lgb
import warnings
warnings.filterwarnings("ignore")

from collections import deque
from datetime import time as dtime

from ml.feature_config import FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM
from ml.indicators import supertrend as _supertrend
from engine.execution.profit_manager import manage_position, _cost_rs
from engine.risk.risk_manager import compute_entry_stops
from backtest.backtest_engine import OptionPriceSimulator, _mins_to_close

# -- Parameters (mirror walkforward_oos defaults) ----------------------
DATA         = "ml/models/training_dataset.csv"
LOOKAHEAD    = 12
TARGET_PTS   = 15
EDGE_MARGIN  = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
VWAP_TOL     = 0.0015
MAX_TRADES   = int(os.getenv("BT_MAX_TRADES", "6"))
COOLDOWN_S   = int(os.getenv("BT_COOLDOWN", "300"))
MAX_HOLD_S   = 300
LOT_UNITS    = 65
SPREAD_PTS   = float(os.getenv("BT_SPREAD_PTS", "1.0"))
FOLDS        = int(os.getenv("FOLDS", "4"))
OOS_START    = os.getenv("OOS_START", "2024-01-01")
NO_ENTRY     = dtime(15, 15)
ORB_END      = dtime(9, 30)

# Use a fixed low threshold to generate MORE trades for richer forensics
ENTRY_THR    = float(os.getenv("FORENSIC_THR", "0.50"))

_opt = OptionPriceSimulator()

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


def _regime(adx_val, di_spread_val):
    """Classify bar as trend or range."""
    if adx_val >= 25:
        return "TREND"
    return "RANGE"


def _orb_state(ts):
    """Is this bar during the ORB window (9:15-9:30)?"""
    t = ts.time()
    return "ORB" if t <= ORB_END else "POST_ORB"


def _hour_bucket(ts):
    h = ts.hour
    m = ts.minute
    if h == 9:
        return "09:15-10:00"
    if h == 10:
        return "10:00-11:00"
    if h == 11:
        return "11:00-12:00"
    if h == 12:
        return "12:00-13:00"
    if h == 13:
        return "13:00-14:00"
    if h == 14:
        return "14:00-15:00"
    return "15:00+"


def _conf_bucket(prob):
    if prob >= 0.80:
        return "0.80+"
    if prob >= 0.70:
        return "0.70-0.80"
    if prob >= 0.60:
        return "0.60-0.70"
    return "0.50-0.60"


def _simulate_forensic(test_df, warmup_rows, ce_model, pe_model, thr):
    """
    Same logic as walkforward_oos._simulate but records full trade metadata.
    Returns list of trade dicts.
    """
    trades = []
    buf = deque(warmup_rows, maxlen=120)
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

        # -- manage open position --
        if position is not None:
            cur_spot = row["close"]
            ltp = _opt.premium(position["entry_spot"], cur_spot, position["side"],
                               _mins_to_close(ts))
            held_s = (ts - entry_ts).total_seconds()
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
            if not exit_flag and held_s > MAX_HOLD_S and new_max < 100:
                exit_flag, reason = True, "TIME_EXIT_WEAK"
            if not exit_flag and now >= NO_ENTRY:
                exit_flag, reason = True, "EOD"
            if exit_flag:
                exit_ltp = ltp - SPREAD_PTS / 2.0
                gross_rs = (exit_ltp - position["entry"]) * position["qty"]
                cost_rs = _cost_rs(position["qty"])
                net_rs = gross_rs - cost_rs
                trades.append({
                    "entry_ts":    position["entry_ts"],
                    "exit_ts":     ts,
                    "side":        position["side"],
                    "ml_prob":     round(position["ml_prob"], 4),
                    "conf_bucket": _conf_bucket(position["ml_prob"]),
                    "orb_state":   position["orb_state"],
                    "regime":      position["regime"],
                    "hour_bucket": _hour_bucket(position["entry_ts"]),
                    "hold_secs":   round(held_s, 0),
                    "exit_reason": reason,
                    "entry_prem":  round(position["entry"], 2),
                    "exit_prem":   round(exit_ltp, 2),
                    "entry_spot":  round(position["entry_spot"], 2),
                    "gross_rs":    round(gross_rs, 2),
                    "cost_rs":     round(cost_rs, 2),
                    "net_rs":      round(net_rs, 2),
                    # Label: did the underlying move favour this direction?
                    "label_correct": int(position["label_correct"]),
                })
                last_exit_ts = ts
                position = None
                entry_ts = None
            continue

        # -- entry gates --
        if now < ORB_END or now >= NO_ENTRY:
            continue
        if trades_today >= MAX_TRADES:
            continue
        if last_exit_ts is not None and (ts - last_exit_ts).total_seconds() < COOLDOWN_S:
            continue

        # -- PREDICT-FIRST --
        X = feats_arr[idx:idx + 1]
        Xdf = pd.DataFrame(X, columns=FEATURE_COLUMNS)
        ce_p = float(ce_model.predict_proba(Xdf)[0][1])
        pe_p = float(pe_model.predict_proba(Xdf)[0][1])
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

        # -- enter --
        entry_spot = float(row["close"])
        mtc = _mins_to_close(ts)
        entry_prem = _opt.premium(entry_spot, entry_spot, side, mtc) + SPREAD_PTS / 2.0
        atr_val = float(row.get("atr", entry_spot * 0.01))
        sl, tgt, _ = compute_entry_stops(entry_prem, atr_val, "RANGE")

        # did the label agree with our direction?
        lbl_col = "label_ce" if side == "CE" else "label_pe"
        label_correct = int(row.get(lbl_col, 0))

        adx_v = float(row.get("adx", 20.0))
        di_s  = float(row.get("di_spread", 0.0))

        position = {
            "side": side, "entry": entry_prem, "entry_spot": entry_spot,
            "qty": LOT_UNITS, "stop_loss": sl, "target": tgt,
            "max_pnl": 0.0, "ml_prob": prob,
            "entry_ts": ts, "entry_idx": idx,
            "orb_state": _orb_state(ts),
            "regime": _regime(adx_v, di_s),
            "label_correct": label_correct,
        }
        entry_ts = ts
        trades_today += 1

    return trades


def _pf(arr):
    """Profit factor from array of net PnLs."""
    a = np.array(arr, float)
    wins = a[a > 0].sum()
    loss = abs(a[a <= 0].sum())
    return round(wins / loss, 3) if loss > 0 else float("inf")


def _wr(arr):
    a = np.array(arr, float)
    return round((a > 0).mean() * 100, 1) if len(a) else 0.0


def _exp(arr):
    a = np.array(arr, float)
    return round(float(a.mean()), 1) if len(a) else 0.0


def _group_stat(df, col, pnl_col="net_rs"):
    """Print profit factor, win%, expectancy grouped by col."""
    print(f"\n  {'Bucket':<22} {'N':>5} {'WR%':>6} {'Exp/T':>9} {'PF':>8} {'Net Rs':>10}")
    print(f"  {'-'*22} {'-'*5} {'-'*6} {'-'*9} {'-'*8} {'-'*10}")
    for key, grp in sorted(df.groupby(col)):
        pnls = grp[pnl_col].tolist()
        print(f"  {str(key):<22} {len(pnls):>5} {_wr(pnls):>5.1f}% "
              f"{_exp(pnls):>9.1f} {_pf(pnls):>8.3f} {sum(pnls):>10.0f}")


def main():
    print("=" * 72)
    print("  TRADE-LEVEL FORENSIC OOS ANALYSIS")
    print(f"  OOS from {OOS_START} | threshold={ENTRY_THR} | edge_margin={EDGE_MARGIN}")
    print("=" * 72)

    df = pd.read_csv(DATA)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0

    oos = df[df["date"] >= pd.Timestamp(OOS_START)].reset_index(drop=True)
    print(f"  OOS rows: {len(oos):,}  ({oos['date'].min().date()} to {oos['date'].max().date()})")
    bounds = np.linspace(0, len(oos), FOLDS + 1, dtype=int)

    all_trades = []

    for k in range(FOLDS):
        lo, hi = bounds[k], bounds[k + 1]
        if hi - lo < 500:
            continue
        test_fold = oos.iloc[lo:hi].reset_index(drop=True)
        test_start = test_fold["date"].iloc[0]
        embargo_cut = df["date"].searchsorted(test_start) - LOOKAHEAD
        train = df.iloc[:max(embargo_cut, 0)]
        if len(train) < 20000:
            continue

        print(f"\n  Training fold {k+1}  (test: {test_start.date()} to "
              f"{test_fold['date'].iloc[-1].date()})  train_rows={len(train):,}")
        ce_m = _train_side(train, "label_ce")
        pe_m = _train_side(train, "label_pe")
        if ce_m is None or pe_m is None:
            print("    [SKIP] training failed")
            continue

        warmup = df.iloc[max(embargo_cut, 0) - 120:max(embargo_cut, 0)].to_dict("records")
        trades = _simulate_forensic(test_fold, warmup, ce_m, pe_m, ENTRY_THR)
        print(f"    trades this fold: {len(trades)}")
        all_trades.extend(trades)

    if not all_trades:
        print("\n  [ERROR] No trades generated -- try lowering FORENSIC_THR or FOLDS.")
        return

    t = pd.DataFrame(all_trades)
    print(f"\n\n{'='*72}")
    print(f"  TOTAL TRADES: {len(t)}")
    print(f"{'='*72}")

    # -- 1. Overview ----------------------------------------------------
    net  = t["net_rs"].values
    wins = net[net > 0]
    loss = net[net <= 0]

    print(f"\n{'-'*72}")
    print(f"  OVERVIEW")
    print(f"{'-'*72}")
    print(f"  Total trades  : {len(net)}")
    print(f"  Win rate      : {_wr(net):.1f}%")
    print(f"  Expectancy    : Rs {_exp(net):.1f} / trade")
    print(f"  Profit factor : {_pf(net):.3f}")
    print(f"  Net PnL total : Rs {net.sum():.0f}")
    print(f"  Max drawdown  : Rs {(np.cumsum(net) - np.maximum.accumulate(np.cumsum(net))).min():.0f}")
    print(f"")
    print(f"  --- Winners ({len(wins)}) ---")
    if len(wins):
        print(f"  Average win   : Rs {wins.mean():.1f}")
        print(f"  Median  win   : Rs {np.median(wins):.1f}")
        print(f"  Largest win   : Rs {wins.max():.1f}")
    print(f"")
    print(f"  --- Losers ({len(loss)}) ---")
    if len(loss):
        print(f"  Average loss  : Rs {loss.mean():.1f}")
        print(f"  Median  loss  : Rs {np.median(loss):.1f}")
        print(f"  Largest loss  : Rs {loss.min():.1f}")

    print(f"\n  --- Cost vs Gross ---")
    print(f"  Total cost paid : Rs {t['cost_rs'].sum():.0f}")
    print(f"  Total gross PnL : Rs {t['gross_rs'].sum():.0f}")
    print(f"  Cost as % of |gross| : {abs(t['cost_rs'].sum())/max(1,abs(t['gross_rs'].sum()))*100:.1f}%")
    print(f"  Avg cost/trade  : Rs {t['cost_rs'].mean():.1f}")
    print(f"  Avg gross/trade : Rs {t['gross_rs'].mean():.1f}")

    # -- 2. Direction accuracy ------------------------------------------
    print(f"\n{'-'*72}")
    print(f"  DIRECTION ACCURACY (label_correct = label agreed with our trade)")
    print(f"{'-'*72}")
    correct = t[t["label_correct"] == 1]
    wrong   = t[t["label_correct"] == 0]
    print(f"  Label correct trades: {len(correct)}  "
          f"WR={_wr(correct['net_rs'].tolist()):.1f}%  "
          f"Exp={_exp(correct['net_rs'].tolist()):.1f}")
    print(f"  Label wrong   trades: {len(wrong)}  "
          f"WR={_wr(wrong['net_rs'].tolist()):.1f}%  "
          f"Exp={_exp(wrong['net_rs'].tolist()):.1f}")
    if len(correct) + len(wrong) > 0:
        dir_acc = len(correct) / (len(correct) + len(wrong)) * 100
        print(f"  Overall direction accuracy: {dir_acc:.1f}%")
        print(f"  to Even CORRECT direction trades: Exp={_exp(correct['net_rs'].tolist()):.1f} Rs")

    # -- 3. Breakdowns -------------------------------------------------
    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY CONFIDENCE BUCKET")
    print(f"{'-'*72}")
    _group_stat(t, "conf_bucket")

    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY HOUR OF DAY")
    print(f"{'-'*72}")
    _group_stat(t, "hour_bucket")

    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY ORB STATE")
    print(f"{'-'*72}")
    _group_stat(t, "orb_state")

    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY REGIME (TREND vs RANGE)")
    print(f"{'-'*72}")
    _group_stat(t, "regime")

    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY SIDE (CE vs PE)")
    print(f"{'-'*72}")
    _group_stat(t, "side")

    print(f"\n{'-'*72}")
    print(f"  PROFIT FACTOR BY EXIT REASON")
    print(f"{'-'*72}")
    _group_stat(t, "exit_reason")

    print(f"\n{'-'*72}")
    print(f"  HOLDING TIME ANALYSIS")
    print(f"{'-'*72}")
    winners_hold = t[t["net_rs"] > 0]["hold_secs"]
    losers_hold  = t[t["net_rs"] <= 0]["hold_secs"]
    print(f"  Avg hold -- winners : {winners_hold.mean():.0f}s  "
          f"median={winners_hold.median():.0f}s") if len(winners_hold) else None
    print(f"  Avg hold -- losers  : {losers_hold.mean():.0f}s  "
          f"median={losers_hold.median():.0f}s") if len(losers_hold) else None

    # -- 4. Top 20% / Bottom 20% ---------------------------------------
    n20 = max(1, int(len(t) * 0.20))
    top20    = t.nlargest(n20, "net_rs")
    bottom20 = t.nsmallest(n20, "net_rs")

    print(f"\n{'-'*72}")
    print(f"  TOP 20% BEST TRADES (n={n20}) -- COMMON CHARACTERISTICS")
    print(f"{'-'*72}")
    _describe_group(top20, "BEST")

    print(f"\n{'-'*72}")
    print(f"  BOTTOM 20% WORST TRADES (n={n20}) -- COMMON CHARACTERISTICS")
    print(f"{'-'*72}")
    _describe_group(bottom20, "WORST")

    # -- 5. Root cause verdict -----------------------------------------
    print(f"\n{'='*72}")
    print(f"  ROOT CAUSE DIAGNOSTIC VERDICT")
    print(f"{'='*72}")
    _verdict(t, correct, wrong)

    # -- 6. Full trade log ---------------------------------------------
    log_path = "backtest/forensic_trades.csv"
    t.to_csv(log_path, index=False)
    print(f"\n  [SAVED] Full trade log to {log_path}  ({len(t)} rows)")
    print("=" * 72)


def _describe_group(grp, label):
    if grp.empty:
        print(f"  (empty)")
        return
    pnls = grp["net_rs"].tolist()
    print(f"  Avg net PnL  : Rs {np.mean(pnls):.1f}")
    print(f"  Avg hold     : {grp['hold_secs'].mean():.0f}s")
    print(f"  Side split   : CE={( grp['side']=='CE').sum()}  PE={( grp['side']=='PE').sum()}")
    print(f"  Regime split : TREND={(grp['regime']=='TREND').sum()}  "
          f"RANGE={(grp['regime']=='RANGE').sum()}")
    print(f"  Hour split   :")
    for h, cnt in grp["hour_bucket"].value_counts().items():
        print(f"    {h:<22} {cnt}")
    print(f"  ORB split    : {grp['orb_state'].value_counts().to_dict()}")
    print(f"  Avg ML prob  : {grp['ml_prob'].mean():.3f}  "
          f"min={grp['ml_prob'].min():.3f}  max={grp['ml_prob'].max():.3f}")
    print(f"  Dir correct  : {grp['label_correct'].mean()*100:.1f}% of these trades")
    print(f"  Exit reasons : {grp['exit_reason'].value_counts().to_dict()}")
    print(f"  Avg entry prem : Rs {grp['entry_prem'].mean():.2f}")
    print(f"  Avg gross/trade: Rs {grp['gross_rs'].mean():.1f}")


def _verdict(t, correct, wrong):
    net = t["net_rs"].values
    gross = t["gross_rs"].values
    cost  = t["cost_rs"].values

    # A) direction accuracy
    dir_acc = len(correct) / max(1, len(t)) * 100
    # B) option friction: even when gross > 0, is net < 0?
    gross_pos = t[t["gross_rs"] > 0]
    friction_kills = (gross_pos["net_rs"] <= 0).sum()
    # C) exit: stops killing winners (avg winner < avg cost)
    wins = net[net > 0]
    avg_win = wins.mean() if len(wins) else 0
    avg_cost = cost.mean()
    # D) overtrading: trades per day
    days = t["entry_ts"].dt.date.nunique()
    tpd = len(t) / max(1, days)

    print(f"\n  A) DIRECTION ACCURACY: {dir_acc:.1f}%  (label agreed with trade)")
    print(f"     to Even correct-direction trades: Exp={_exp(correct['net_rs'].tolist()):.1f} Rs")
    if dir_acc < 50:
        print(f"     *** DIRECTION IS WRONG more than half the time -- primary cause ***")
    elif dir_acc >= 50 and _exp(correct["net_rs"].tolist()) < 0:
        print(f"     *** Direction OK but even correct trades lose to option friction primary ***")

    print(f"\n  B) OPTION FRICTION:")
    print(f"     Avg cost/trade = Rs {avg_cost:.1f}  vs  Avg gross = Rs {gross.mean():.1f}")
    print(f"     Trades where gross>0 but net<=0 (cost kills it): {friction_kills}")
    if gross.mean() < avg_cost:
        print(f"     *** GROSS < COST -- avg move smaller than friction. Primary cause: FRICTION ***")

    print(f"\n  C) EXIT QUALITY:")
    print(f"     Avg winner = Rs {avg_win:.1f}  vs  Avg cost = Rs {avg_cost:.1f}")
    stop_exits = t[t["exit_reason"] == "STOP"]
    eod_exits  = t[t["exit_reason"] == "EOD"]
    print(f"     Stop exits: {len(stop_exits)}  EOD exits: {len(eod_exits)}")
    if len(stop_exits):
        print(f"     Stop-exit avg net: Rs {stop_exits['net_rs'].mean():.1f}")
    if avg_win > 0 and avg_win < avg_cost * 1.5:
        print(f"     *** Winners too small to overcome average loss -- exit/target issue ***")

    print(f"\n  D) OVERTRADING:")
    print(f"     Trading days: {days}   Avg trades/day: {tpd:.1f}")
    if tpd > 4:
        print(f"     *** HIGH trade frequency -- costs compound fast ***")

    print(f"\n  E) REGIME MISMATCH:")
    trend_trades = t[t["regime"] == "TREND"]
    range_trades = t[t["regime"] == "RANGE"]
    te = _exp(trend_trades["net_rs"].tolist()) if len(trend_trades) else 0
    re = _exp(range_trades["net_rs"].tolist()) if len(range_trades) else 0
    print(f"     TREND trades: {len(trend_trades)}  Exp={te:.1f} Rs")
    print(f"     RANGE trades: {len(range_trades)}  Exp={re:.1f} Rs")
    if re < te - 50:
        print(f"     *** RANGE regime is draining -- entries in choppy markets ***")

    print(f"\n  F) CE vs PE ASYMMETRY:")
    ce = t[t["side"] == "CE"]
    pe = t[t["side"] == "PE"]
    print(f"     CE: n={len(ce)}  Exp={_exp(ce['net_rs'].tolist()):.1f}  "
          f"WR={_wr(ce['net_rs'].tolist()):.1f}%")
    print(f"     PE: n={len(pe)}  Exp={_exp(pe['net_rs'].tolist()):.1f}  "
          f"WR={_wr(pe['net_rs'].tolist()):.1f}%")
    if abs(_exp(ce["net_rs"].tolist()) - _exp(pe["net_rs"].tolist())) > 100:
        print(f"     *** Strong CE/PE asymmetry -- one side is the drain ***")

    print(f"\n{'-'*72}")
    print(f"  SUMMARY SCORE (primary to secondary causes):")
    causes = []
    if dir_acc < 50:
        causes.append("A) BAD DIRECTION (model wrong > half the time)")
    if gross.mean() > 0 and gross.mean() < avg_cost:
        causes.append("B) OPTION FRICTION (move too small vs cost)")
    if avg_win < avg_cost * 1.5:
        causes.append("C) POOR EXITS (winners cut too small)")
    if tpd > 4:
        causes.append("E) OVERTRADING (cost compounds)")
    if re < te - 50 and len(range_trades) > len(trend_trades) * 0.3:
        causes.append("F) REGIME MISMATCH (range entries killing PnL)")
    if not causes:
        causes.append("Cannot isolate single cause -- multiple factors")
    for i, c in enumerate(causes, 1):
        print(f"    #{i}: {c}")
    print(f"{'-'*72}")


if __name__ == "__main__":
    main()
