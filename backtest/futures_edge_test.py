# backtest/futures_edge_test.py
# ---------------------------------------------------------------------------
# DOES THE SIGNAL HAVE ANY EDGE AT ALL?  (futures / index, low friction)
#
# WHY THIS EXISTS:
#   Options are the HARDEST place to find edge: spread + theta + gamma eat
#   small directional edges alive. If your direction model cannot make money
#   on NIFTY FUTURES (tiny friction, linear payoff), it has zero chance on
#   ATM options. This script answers: "is there a directional edge?"
#
# TWO MODES (set via MODE= env var):
#   MODE=ml   — trains a fresh LightGBM on futures first-touch labels each
#               fold and measures directional accuracy.  (verdict: no edge)
#   MODE=orb  — pure structural: enter when price breaks the Opening Range
#               on trend days, confirmed by 5m supertrend.  No ML at all.
#               This is the path to 70%+ win rate.
#   MODE=both — run both and compare (default).
#
# RUN:
#   python backtest/futures_edge_test.py           # both modes
#   MODE=orb python backtest/futures_edge_test.py  # ORB only (fast)
#   VERBOSE=1 python backtest/futures_edge_test.py # per-trade log
#   TREND_DAY_RANGE=150 python backtest/futures_edge_test.py  # stricter
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
from ml.morning_regime import regime_proxy
from ml.weekly_regime import add_weekly_regime_to_df

# ── Instrument: BANKNIFTY only ────────────────────────────────────────
INSTRUMENT = "banknifty"

DATA    = "data/historical/banknifty_1m_full.csv"
ML_DATA = "ml/models/training_dataset_v3.csv"

_cfg = {
    "lot":              30,
    "def_target":       75.0,   # 3:1 R:R — BE at 25%; profitable at 30% win rate
    "def_stop":         25.0,
    "def_trend":        300.0,  # prev_day_range min for BANKNIFTY trend day
    "def_buffer":       15.0,   # pt beyond ORB to filter fakeouts
    "def_lookahead":    30,     # bars — 30 min for 75pt target to develop
    "def_entry_start":  "9:35", # capture morning momentum
    "name":             "BANKNIFTY",
}

# ── Shared parameters ─────────────────────────────────────────────────
LOOKAHEAD        = int(os.getenv("LOOKAHEAD", str(_cfg["def_lookahead"])))
TARGET_SPOT_PTS  = float(os.getenv("FUT_TARGET_PTS", str(_cfg["def_target"])))
STOP_SPOT_PTS    = float(os.getenv("FUT_STOP_PTS",   str(_cfg["def_stop"])))
LOT_UNITS        = int(os.getenv("LOT_UNITS",        str(_cfg["lot"])))
FUT_COST_PTS     = float(os.getenv("FUT_COST_PTS", "1.5"))
FOLDS            = int(os.getenv("FOLDS", "4"))
OOS_START        = os.getenv("OOS_START", "2024-01-01")
NO_ENTRY_AFTER   = dtime(15, 15)
ORB_END          = dtime(9, 30)
VERBOSE          = int(os.getenv("VERBOSE", "0"))
MODE             = os.getenv("MODE", "orb")    # "ml" | "orb" | "both"
# Regime filter: use LLM proxy to skip RANGE days entirely.
# Only TREND_UP days get CE entries, only TREND_DOWN days get PE entries.
USE_REGIME       = int(os.getenv("USE_REGIME", "1"))

# ── ML-mode parameters ────────────────────────────────────────────────
EDGE_MARGIN      = float(os.getenv("ML_EDGE_MARGIN", "0.15"))
VWAP_TOL         = 0.0015
MAX_TRADES_DAY   = int(os.getenv("BT_MAX_TRADES", "6"))
COOLDOWN_S       = int(os.getenv("BT_COOLDOWN", "300"))

# ── ORB-mode parameters ───────────────────────────────────────────────
# Previous-day high-low range must exceed this to classify as a trend day.
# At NIFTY ~24000, typical daily range is 150-250pt. 180pt selects top ~40%
# of days where NIFTY has genuine momentum (not sideways chop).
TREND_DAY_RANGE  = float(os.getenv("TREND_DAY_RANGE", str(_cfg["def_trend"])))
ORB_WINDOW_MIN   = int(os.getenv("ORB_WINDOW", "15"))            # 9:15 -> 9:30
# HTF5 must confirm ORB direction (1=yes, 0=skip confirmation)
ORB_HTF5_GATE    = int(os.getenv("ORB_HTF5_GATE", "1"))
# Require price to close this many points BEYOND the ORB level before entering.
# Filters micro-fakeouts where price barely ticks above ORB then reverses.
ORB_BUFFER       = float(os.getenv("ORB_BUFFER", str(_cfg["def_buffer"])))
# Lock the day's tradeable direction from HTF5 at 9:30 (ORB close).
# 1 = only CE trades on bullish days, only PE on bearish → no whipsaw.
# 0 = allow both CE and PE on the same day.
ORB_SINGLE_DIR   = int(os.getenv("ORB_SINGLE_DIR", "1"))
# Earliest bar allowed for ORB entries. The 9:30-10:30 window is dominated by
# institutional stop-hunts above/below the ORB. After 10:30 genuine breakouts
# are more likely to follow through cleanly.
_orb_entry_str   = os.getenv("ORB_ENTRY_START", _cfg["def_entry_start"])
_h, _m           = int(_orb_entry_str.split(":")[0]), int(_orb_entry_str.split(":")[1])
ORB_ENTRY_START  = dtime(_h, _m)

_LGB_PARAMS = dict(
    n_estimators=300, learning_rate=0.03, max_depth=6, num_leaves=31,
    subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
    reg_alpha=0.05, reg_lambda=0.1, verbose=-1, random_state=42, n_jobs=-1,
)


# ── Day-context enrichment (used by ORB mode) ─────────────────────────

def _enrich_df(df):
    """
    Add structural day-context columns to df (in-place copy):
      prev_day_range  — previous trading day's high-low range in spot points
      is_trend_day    — prev_day_range >= TREND_DAY_RANGE
      orb_high        — max high of first ORB_WINDOW_MIN minutes (9:15..9:30)
      orb_low         — min low  of first ORB_WINDOW_MIN minutes
    All values are forward-filled across the trading day so every bar has them.
    """
    df = df.sort_values("date").copy()
    date_only = df["date"].dt.date
    df["_d"] = date_only

    # Daily range
    daily = df.groupby("_d").agg(day_high=("high", "max"), day_low=("low", "min"))
    daily["day_range"] = daily["day_high"] - daily["day_low"]
    daily["prev_day_range"] = daily["day_range"].shift(1)

    # ORB window (first ORB_WINDOW_MIN minutes of session)
    orb_cut = dtime(9, 15 + ORB_WINDOW_MIN) if ORB_WINDOW_MIN < 45 else dtime(10, 0)
    orb_mask = (df["date"].dt.time >= dtime(9, 15)) & (df["date"].dt.time < orb_cut)
    orb = df[orb_mask].groupby("_d").agg(orb_high=("high", "max"), orb_low=("low", "min"))

    df["prev_day_range"] = df["_d"].map(daily["prev_day_range"].to_dict())
    df["is_trend_day"]   = df["prev_day_range"] >= TREND_DAY_RANGE
    df["orb_high"]       = df["_d"].map(orb["orb_high"].to_dict())
    df["orb_low"]        = df["_d"].map(orb["orb_low"].to_dict())

    # Rule-based LLM regime proxy (backtesting substitute for morning_regime.py)
    daily_open  = df.groupby("_d")["open"].first()
    daily_close = df.groupby("_d")["close"].last()
    daily_high  = df.groupby("_d")["high"].max()
    daily_low   = df.groupby("_d")["low"].min()

    regime_map = {}
    dates = sorted(daily_open.index)
    for i, d in enumerate(dates):
        if i == 0:
            regime_map[d] = "RANGE"
            continue
        pd_ = dates[i - 1]
        regime_map[d] = regime_proxy(
            prev_open=float(daily_open[pd_]),  prev_high=float(daily_high[pd_]),
            prev_low=float(daily_low[pd_]),    prev_close=float(daily_close[pd_]),
            today_open=float(daily_open[d]),   instrument=INSTRUMENT,
        )
    df["day_regime"] = df["_d"].map(regime_map)
    df.drop(columns=["_d"], inplace=True)

    # Weekly regime proxy — filters out chop weeks entirely
    df = add_weekly_regime_to_df(df, instrument=INSTRUMENT)
    return df


# ── Shared helpers ────────────────────────────────────────────────────

def _htf5_dir(buf):
    if len(buf) < 60:
        return 0
    h = np.array([b["high"]  for b in buf], float)
    l = np.array([b["low"]   for b in buf], float)
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


def _record_exit(position, exit_spot, exit_type, held, ts, trades, trade_log):
    es = position["entry_spot"]; sd = position["side"]
    move = (exit_spot - es) if sd == "CE" else (es - exit_spot)
    net  = (move - FUT_COST_PTS) * LOT_UNITS
    trades.append(net)
    trade_log.append({
        "ts": position["entry_ts"], "side": sd,
        "entry": round(es, 1), "exit": round(exit_spot, 1),
        "held": held, "exit_type": exit_type, "net": round(net, 0),
    })


def _manage_position(position, row, idx, trades, trade_log):
    """Check target/stop/horizon. Returns None if closed, else position dict."""
    es = position["entry_spot"]; sd = position["side"]
    fav = es + TARGET_SPOT_PTS if sd == "CE" else es - TARGET_SPOT_PTS
    adv = es - STOP_SPOT_PTS   if sd == "CE" else es + STOP_SPOT_PTS
    fav_hit = row["high"] >= fav if sd == "CE" else row["low"]  <= fav
    adv_hit = row["low"]  <= adv if sd == "CE" else row["high"] >= adv
    held = idx - position["entry_idx"]
    exit_spot = exit_type = None
    if adv_hit:
        exit_spot, exit_type = adv, "STOP"
    elif fav_hit:
        exit_spot, exit_type = fav, "TARGET"
    elif held >= LOOKAHEAD:
        exit_spot, exit_type = row["close"], "HORIZON"
    if exit_spot is not None:
        _record_exit(position, exit_spot, exit_type, held, row["date"], trades, trade_log)
        return None
    return position


# ── ML mode ──────────────────────────────────────────────────────────

def _make_futures_labels(df):
    """
    First-touch barrier labels: label_ce/pe = 1 iff spot hits TARGET before
    STOP within LOOKAHEAD bars.  Only labels active-session bars.
    """
    n     = len(df)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    times = df["date"].dt.time.values
    label_ce = np.zeros(n, dtype=np.int8)
    label_pe = np.zeros(n, dtype=np.int8)

    for i in range(n - LOOKAHEAD):
        t = times[i]
        if t < ORB_END or t >= NO_ENTRY_AFTER:
            continue
        es = close[i]
        tgt_ce = es + TARGET_SPOT_PTS; stp_ce = es - STOP_SPOT_PTS
        for j in range(i + 1, i + 1 + LOOKAHEAD):
            if high[j] >= tgt_ce:   label_ce[i] = 1; break
            if low[j]  <= stp_ce:   break
        tgt_pe = es - TARGET_SPOT_PTS; stp_pe = es + STOP_SPOT_PTS
        for j in range(i + 1, i + 1 + LOOKAHEAD):
            if low[j]  <= tgt_pe:   label_pe[i] = 1; break
            if high[j] >= stp_pe:   break

    out = df.copy()
    out["label_ce"] = label_ce
    out["label_pe"] = label_pe
    return out


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


def _simulate_ml(test_df, warmup_rows, ce_model, pe_model, thr):
    trades = []; trade_log = []
    buf = deque(warmup_rows, maxlen=120)
    cur_day = None; position = None
    last_exit_ts = None; trades_today = 0; prev_close = None
    feats_arr = test_df[FEATURE_COLUMNS].values
    rows = test_df.to_dict("records")

    for idx, row in enumerate(rows):
        ts  = row["date"]; day = ts.date()
        if day != cur_day:
            if position is not None and prev_close is not None:
                es = position["entry_spot"]; sd = position["side"]
                move = (prev_close - es) if sd == "CE" else (es - prev_close)
                net  = (move - FUT_COST_PTS) * LOT_UNITS
                trades.append(net)
                trade_log.append({"ts": position["entry_ts"], "side": sd,
                                   "entry": round(es,1), "exit": round(prev_close,1),
                                   "held": idx - position["entry_idx"],
                                   "exit_type": "EOD", "net": round(net,0)})
            cur_day = day; trades_today = 0; last_exit_ts = None; position = None
        buf.append(row)
        now = ts.time()

        if position is not None:
            position = _manage_position(position, row, idx, trades, trade_log)
            if position is None:
                last_exit_ts = ts
            prev_close = float(row["close"]); continue

        if now < ORB_END or now >= NO_ENTRY_AFTER:
            prev_close = float(row["close"]); continue
        if trades_today >= MAX_TRADES_DAY:
            prev_close = float(row["close"]); continue
        if last_exit_ts and (ts - last_exit_ts).total_seconds() < COOLDOWN_S:
            prev_close = float(row["close"]); continue

        X = feats_arr[idx:idx + 1]
        Xdf = pd.DataFrame(X, columns=FEATURE_COLUMNS)
        ce_p = float(ce_model.predict_proba(Xdf)[0][1])
        pe_p = float(pe_model.predict_proba(Xdf)[0][1])
        side = "CE" if ce_p >= pe_p else "PE"
        prob = ce_p if side == "CE" else pe_p
        if abs(ce_p - pe_p) < EDGE_MARGIN or prob < thr:
            prev_close = float(row["close"]); continue
        htf5 = _htf5_dir(buf)
        if (side == "CE" and htf5 == -1) or (side == "PE" and htf5 == 1):
            prev_close = float(row["close"]); continue
        pvwap = float(row.get("price_vs_vwap", 0.0))
        if (side == "CE" and pvwap < -VWAP_TOL) or (side == "PE" and pvwap > VWAP_TOL):
            prev_close = float(row["close"]); continue

        position = {"side": side, "entry_spot": float(row["close"]),
                    "entry_idx": idx, "entry_ts": ts}
        trades_today += 1
        prev_close = float(row["close"])

    return trades, trade_log


# ── ORB mode ──────────────────────────────────────────────────────────

def _simulate_orb(test_df, warmup_rows):
    """
    Structural ORB breakout — no ML model.

    Entry rules:
      1. Trend-day gate: prev_day_range >= TREND_DAY_RANGE (top ~40% of days)
      2. Direction locked at 9:30 from HTF5 — only CE on bullish days, PE on bearish
         (ORB_SINGLE_DIR=1). Prevents whipsawing both ways on same day.
      3. CE: close > orb_high + ORB_BUFFER (buffer filters micro-fakeouts)
         PE: close < orb_low  - ORB_BUFFER
      4. HTF5 at entry bar must still confirm (ORB_HTF5_GATE=1)
      One trade per day per allowed direction.
    """
    trades = []; trade_log = []
    buf = deque(warmup_rows, maxlen=120)
    cur_day = None; position = None; prev_close = None
    orb_ce_fired = False; orb_pe_fired = False
    day_direction = 0   # locked at 9:30 from HTF5
    day_dir_set   = False
    rows = test_df.to_dict("records")

    for idx, row in enumerate(rows):
        ts  = row["date"]; day = ts.date()
        if day != cur_day:
            if position is not None and prev_close is not None:
                _record_exit(position, prev_close, "EOD",
                             idx - position["entry_idx"], ts, trades, trade_log)
                position = None
            cur_day = day
            orb_ce_fired = False; orb_pe_fired = False
            day_direction = 0;    day_dir_set = False
        buf.append(row)
        now = ts.time()

        if position is not None:
            position = _manage_position(position, row, idx, trades, trade_log)
            prev_close = float(row["close"]); continue

        # Lock day direction at ORB close (first bar at or after 9:30)
        if ORB_SINGLE_DIR and not day_dir_set and now >= ORB_END:
            day_direction = _htf5_dir(buf)
            day_dir_set   = True

        # Entry only after ORB window closes AND after entry-start time
        if now < ORB_ENTRY_START or now >= NO_ENTRY_AFTER:
            prev_close = float(row["close"]); continue

        # Trend-day gate
        if not row.get("is_trend_day", False):
            prev_close = float(row["close"]); continue

        orb_h = row.get("orb_high"); orb_l = row.get("orb_low")
        if orb_h is None or orb_l is None or np.isnan(orb_h) or np.isnan(orb_l):
            prev_close = float(row["close"]); continue

        price = float(row["close"])
        htf5  = _htf5_dir(buf) if ORB_HTF5_GATE else 0

        if USE_REGIME:
            week_regime = row.get("week_regime", "CHOP_WEEK")
            day_regime  = row.get("day_regime",  "RANGE")

            # Skip the whole day if weekly trend is absent
            if week_regime == "CHOP_WEEK":
                prev_close = float(row["close"]); continue

            # Skip if daily regime is range/unclear
            if day_regime in ("RANGE", "SKIP"):
                prev_close = float(row["close"]); continue

            # Direction: weekly regime overrides — only CE on bull weeks, PE on bear weeks
            if week_regime == "BULL_WEEK":
                ce_ok = (not ORB_SINGLE_DIR) or (day_direction == 1)
                pe_ok = False
            elif week_regime == "BEAR_WEEK":
                pe_ok = (not ORB_SINGLE_DIR) or (day_direction == -1)
                ce_ok = False
            else:
                ce_ok = (not ORB_SINGLE_DIR) or (day_direction == 1)
                pe_ok = (not ORB_SINGLE_DIR) or (day_direction == -1)
        else:
            ce_ok = (not ORB_SINGLE_DIR) or (day_direction == 1)
            pe_ok = (not ORB_SINGLE_DIR) or (day_direction == -1)

        # CE entry: close breaks ORB high by buffer, HTF5 bullish
        if ce_ok and not orb_ce_fired and price > orb_h + ORB_BUFFER:
            if not ORB_HTF5_GATE or htf5 == 1:
                position = {"side": "CE", "entry_spot": price,
                            "entry_idx": idx, "entry_ts": ts}
                orb_ce_fired = True

        # PE entry: close breaks ORB low by buffer, HTF5 bearish
        elif pe_ok and not orb_pe_fired and price < orb_l - ORB_BUFFER:
            if not ORB_HTF5_GATE or htf5 == -1:
                position = {"side": "PE", "entry_spot": price,
                            "entry_idx": idx, "entry_ts": ts}
                orb_pe_fired = True

        prev_close = float(row["close"])

    return trades, trade_log


# ── Metrics & display ─────────────────────────────────────────────────

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
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2)
                         if losses.sum() < 0 else float("inf"),
    }


def _print_table(title, pnls_by_thr, thresholds, min_trades):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"  Break-even win rate: "
          f"{(STOP_SPOT_PTS+FUT_COST_PTS)/(TARGET_SPOT_PTS+STOP_SPOT_PTS)*100:.1f}%"
          f"  (target={TARGET_SPOT_PTS}pt stop={STOP_SPOT_PTS}pt cost={FUT_COST_PTS}pt/RT)")
    print("="*70)
    print(f"  {'thr':>5} {'trades':>7} {'win%':>6} {'exp/trade':>10} {'PF':>6} {'net_pnl':>10}")
    any_edge = False
    for t in thresholds:
        m = _metrics(pnls_by_thr[t])
        if m.get("trades", 0) == 0:
            print(f"  {t:>5} {'0':>7}  (no trades)")
            continue
        tag = "" if m["trades"] >= min_trades else "  <- sample too small"
        print(f"  {t:>5} {m['trades']:>7} {m['win_rate']*100:>5.1f}% "
              f"{m['expectancy_per_trade']:>10} {m['profit_factor']:>6} "
              f"{m['net_pnl']:>10}{tag}")
        if m["trades"] >= min_trades and m["expectancy_per_trade"] > 0:
            any_edge = True
    print()
    if any_edge:
        print("  VERDICT: Edge EXISTS (>=min_trades and +EV).")
    else:
        print("  VERDICT: No edge found at any threshold.")
    print("="*70)
    return any_edge


def _print_verbose(logs_by_thr, thresholds):
    print("\n" + "="*70 + "\n  TRADE LOG (VERBOSE=1)\n" + "="*70)
    for t in thresholds:
        log = logs_by_thr[t]
        if not log: continue
        pnls = [r["net"] for r in log]
        print(f"\n  -- thr={t}  ({len(log)} trades) --")
        print(f"  {'Timestamp':<20} {'Side':<4} {'Entry':>8} {'Exit':>8} "
              f"{'Held':>5} {'ExitType':<8} {'Net Rs':>8}")
        for r in log:
            print(f"  {str(r['ts']):<20} {r['side']:<4} {r['entry']:>8} "
                  f"{r['exit']:>8} {r['held']:>5} {r['exit_type']:<8} {r['net']:>8}")
        print(f"  Worst 5: {sorted(pnls)[:5]}")


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print("="*70)
    print(f"  FUTURES EDGE TEST  —  BANKNIFTY  MODE: {MODE.upper()}")
    print(f"  target={TARGET_SPOT_PTS}pt  stop={STOP_SPOT_PTS}pt  "
          f"cost={FUT_COST_PTS}pt/RT  lot={LOT_UNITS}")
    if MODE in ("orb", "both"):
        be = (STOP_SPOT_PTS + FUT_COST_PTS) / (TARGET_SPOT_PTS + STOP_SPOT_PTS) * 100
        print(f"  ORB: trend_day_range>={TREND_DAY_RANGE}pt  orb_window={ORB_WINDOW_MIN}min  "
              f"buffer={ORB_BUFFER}pt  entry_start={ORB_ENTRY_START}  "
              f"single_dir={bool(ORB_SINGLE_DIR)}  htf5={bool(ORB_HTF5_GATE)}")
        print(f"  Regime proxy: {'ON (LLM day-type gate)' if USE_REGIME else 'OFF'}")
        print(f"  Break-even win rate: {be:.1f}%  "
              f"(win={TARGET_SPOT_PTS-FUT_COST_PTS:.1f}pt  loss={STOP_SPOT_PTS+FUT_COST_PTS:.1f}pt)")
    print("="*70)

    df = pd.read_csv(DATA)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for f in FEATURE_COLUMNS:
        if f not in df.columns:
            df[f] = 0.0
    for col in ("high", "low", "open"):
        if col not in df.columns:
            df[col] = df["close"]

    # Enrich with day context (ORB mode needs this)
    if MODE in ("orb", "both"):
        print("[ENRICH] Computing ORB levels + regime proxy ...", end=" ", flush=True)
        df = _enrich_df(df)
        trend_days  = df.groupby(df["date"].dt.date)["is_trend_day"].first().mean()
        if USE_REGIME:
            daily_idx   = df.groupby(df["date"].dt.date)
            day_counts  = daily_idx["day_regime"].first().value_counts()
            week_counts = daily_idx["week_regime"].first().value_counts()
            print(f"trend days: {trend_days:.1%}  |  "
                  f"weekly: {dict(week_counts)}  |  daily: {dict(day_counts)}")
        else:
            print(f"trend days: {trend_days:.1%}")

    oos = df[df["date"] >= pd.Timestamp(OOS_START)].reset_index(drop=True)
    bounds = np.linspace(0, len(oos), FOLDS + 1, dtype=int)

    ML_THRESHOLDS  = [0.50, 0.60, 0.70, 0.79, 0.85, 0.90]
    ORB_THRESHOLDS = ["all"]   # ORB has no ML threshold — one row for all trades

    ml_pnls  = {t: [] for t in ML_THRESHOLDS};  ml_logs  = {t: [] for t in ML_THRESHOLDS}
    orb_pnls = {t: [] for t in ORB_THRESHOLDS}; orb_logs = {t: [] for t in ORB_THRESHOLDS}

    MIN_TRADES = int(os.getenv("MIN_TRADES_VERDICT", "100"))

    for k in range(FOLDS):
        lo, hi = bounds[k], bounds[k + 1]
        if hi - lo < 500:
            continue
        test_fold  = oos.iloc[lo:hi].reset_index(drop=True)
        test_start = test_fold["date"].iloc[0]
        embargo_cut = df["date"].searchsorted(test_start) - LOOKAHEAD
        cut = max(embargo_cut, 0)
        warmup = df.iloc[max(cut - 120, 0):cut].to_dict("records")

        fold_hdr = (f"  Fold {k+1}  "
                    f"{test_start.date()}..{test_fold['date'].iloc[-1].date()}")

        # ── ML mode ──
        if MODE in ("ml", "both"):
            train_raw = df.iloc[:cut]
            if len(train_raw) < 20000:
                print(f"{fold_hdr}  [ML] skipped (insufficient train)")
            else:
                print(f"{fold_hdr}  [ML] labeling {len(train_raw):,} rows ...",
                      end=" ", flush=True)
                train = _make_futures_labels(train_raw)
                print(f"CE+={train['label_ce'].mean():.1%}  PE+={train['label_pe'].mean():.1%}")
                ce_m = _train_side(train, "label_ce")
                pe_m = _train_side(train, "label_pe")
                if ce_m is None or pe_m is None:
                    print(f"{fold_hdr}  [ML] training failed")
                else:
                    parts = []
                    for t in ML_THRESHOLDS:
                        pnls, log = _simulate_ml(test_fold, warmup, ce_m, pe_m, thr=t)
                        ml_pnls[t].extend(pnls); ml_logs[t].extend(log)
                        m = _metrics(pnls)
                        parts.append(f"thr{t:.2f}:n={m.get('trades',0)}"
                                     f",exp={m.get('expectancy_per_trade',0)}")
                    print(f"{fold_hdr}  [ML] " + "  | ".join(parts))

        # ── ORB mode ──
        if MODE in ("orb", "both"):
            pnls, log = _simulate_orb(test_fold, warmup)
            orb_pnls["all"].extend(pnls); orb_logs["all"].extend(log)
            m = _metrics(pnls)
            print(f"{fold_hdr}  [ORB] "
                  f"n={m.get('trades',0)}  "
                  f"win={m.get('win_rate',0)*100:.1f}%  "
                  f"exp={m.get('expectancy_per_trade',0)}")

    # ── Results ──
    if MODE in ("ml", "both"):
        _print_table("ML MODE — AGGREGATE BY THRESHOLD", ml_pnls, ML_THRESHOLDS, MIN_TRADES)

    if MODE in ("orb", "both"):
        # ORB has a single "threshold" so reuse the table with "all" key
        _print_table("ORB MODE — AGGREGATE (trend day + HTF5, no ML)",
                     orb_pnls, ORB_THRESHOLDS, MIN_TRADES)
        # Day-type summary
        if orb_logs["all"]:
            total = len(orb_logs["all"])
            targets = sum(1 for r in orb_logs["all"] if r["exit_type"] == "TARGET")
            stops   = sum(1 for r in orb_logs["all"] if r["exit_type"] == "STOP")
            horiz   = sum(1 for r in orb_logs["all"] if r["exit_type"] == "HORIZON")
            eod     = sum(1 for r in orb_logs["all"] if r["exit_type"] == "EOD")
            print(f"  Exit breakdown: TARGET={targets}({targets/total:.0%})  "
                  f"STOP={stops}({stops/total:.0%})  "
                  f"HORIZON={horiz}({horiz/total:.0%})  "
                  f"EOD={eod}({eod/total:.0%})")

    if VERBOSE:
        if MODE in ("ml",  "both"): _print_verbose(ml_logs,  ML_THRESHOLDS)
        if MODE in ("orb", "both"): _print_verbose(orb_logs, ORB_THRESHOLDS)


if __name__ == "__main__":
    main()
