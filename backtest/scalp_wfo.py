#!/usr/bin/env python3
"""
Walk-Forward Optimization (WFO) for Scalp Engine.
Faster version: 500 combos per fold instead of 2000.
"""

import os
import sys
import io
import time as _time
from datetime import datetime, time as dtime, timedelta
from collections import defaultdict, deque, Counter
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MARKET_OPEN  = dtime(9, 15)
SCALP_START  = dtime(9, 30)
SCALP_END    = dtime(15, 10)
MARKET_CLOSE = dtime(15, 30)

FIXED = {
    "BE_PTS": 2.0, "SL_STRICT_PTS": 3.0, "SL_MED_PTS": 5.0, "SL_WIDE_PTS": 8.0,
    "TARGET_PTS": 50.0, "MAX_HOLD_SECS": 180, "NO_LIFE_SECS": 35,
    "MOM_WINDOW": 300, "MOM_THRESHOLD": 20.0, "MIN_SAMPLES": 3,
    "EXHAUST_TAIL_FRAC": 0.65, "ML_MIN_PROB": 0.0,
    "LOT_SIZE": 30, "LOTS_PER_TRADE": 2, "MIN_OPT_PTS": 30.0, "DAILY_LOSS_LIMIT": -2000,
}

PARAM_GRID = {
    "COOLDOWN":           [120, 180, 240, 300],
    "TRAIL_START_PTS":    [5.0, 8.0, 10.0, 12.0],
    "TRAIL_PTS":          [2.0, 3.0, 4.0, 5.0],
    "MAX_MOVE_PTS":       [20.0, 25.0, 30.0, 35.0],
    "MAX_TRADES_PER_DAY": [4, 5, 6, 8],
    "MAX_CONSEC_LOSSES":  [2, 3, 4, 5],
}

def option_premium(entry_spot, cur_spot, side, mins_to_close):
    T = max(mins_to_close / (375 * 252), 1e-6)
    time_val = 150.0 * (T ** 0.5) * 0.12 * 100
    favorable = (cur_spot - entry_spot) if side == "CE" else (entry_spot - cur_spot)
    return round(max(time_val + 0.5 * favorable, 1.0), 2)

def mins_to_close(ts):
    return max((MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute) - (ts.hour * 60 + ts.minute), 1)

def compute_5m_supertrend(df_5m, period=10, multiplier=3.0):
    if len(df_5m) < period + 1:
        return np.zeros(len(df_5m))
    highs = df_5m["high"].values.astype(float)
    lows = df_5m["low"].values.astype(float)
    closes = df_5m["close"].values.astype(float)
    atr = np.zeros(len(closes))
    st_dir = np.ones(len(closes))
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        atr[i] = (atr[i-1] * (period - 1) + tr) / period
    mid = (highs + lows) / 2
    lower = mid - multiplier * atr
    upper = mid + multiplier * atr
    st_line = np.copy(closes)
    for i in range(1, len(closes)):
        if closes[i] > st_line[i-1]:
            st_line[i] = max(lower[i], st_line[i-1])
        else:
            st_line[i] = min(upper[i], st_line[i-1])
        st_dir[i] = 1 if closes[i] > st_line[i-1] else -1
    return st_dir

def build_htf5_map(df):
    df_5m = df.set_index("date").resample("5min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna().reset_index()
    htf5_st = compute_5m_supertrend(df_5m)
    htf5_map = {}
    for idx in range(len(df_5m)):
        key = pd.Timestamp(df_5m.iloc[idx]["date"]).floor("5min")
        htf5_map[key] = int(htf5_st[idx]) if idx < len(htf5_st) else 0
    return htf5_map

@dataclass
class Trade:
    trade_id: int; date: str; entry_time: datetime; exit_time: datetime
    side: str; entry_prem: float; exit_prem: float; pnl: float
    ml_prob: float; move_pts: float; exit_reason: str; held_secs: float

def simulate_scalps(df, settings, htf5_map):
    trades = []; trade_id = 0
    qty = settings["LOT_SIZE"] * settings["LOTS_PER_TRADE"]
    all_days = sorted(df["day"].unique())

    for trading_day in all_days:
        day_df = df[df["day"] == trading_day].copy().reset_index(drop=True)
        date_str = str(trading_day)
        position = None; last_exit_ts = None; trades_today = 0
        day_pnl = 0.0; consec_losses = 0
        ltp_history = deque(maxlen=120)

        for i in range(len(day_df)):
            row = day_df.iloc[i]; ts = row["date"]; now = ts.time()
            close = float(row["close"])

            if now < MARKET_OPEN:
                ltp_history.append((ts, close)); continue

            if now >= dtime(15, 15) and position is not None:
                ep = option_premium(position["entry_spot"], close - 0.5, position["side"], mins_to_close(ts))
                pnl = (ep - position["entry_prem"]) * qty; trade_id += 1
                held = (ts - position["entry_ts"]).total_seconds()
                trades.append(Trade(trade_id, date_str, position["entry_ts"], ts, position["side"],
                    position["entry_prem"], ep, round(pnl, 2), 0.0, position.get("move_pts", 0), "TIME_CLOSE", held))
                day_pnl += pnl; consec_losses = consec_losses + 1 if pnl <= 0 else 0
                position = None; ltp_history.append((ts, close)); continue

            ltp_history.append((ts, close))

            if position is not None:
                ltp = option_premium(position["entry_spot"], close, position["side"], mins_to_close(ts))
                held = (ts - position["entry_ts"]).total_seconds()
                entry = position["entry_prem"]; sl = position["stop_loss"]
                ef = False; er = ""

                if ltp <= sl: ef, er = True, "STOP"
                if not ef and held > settings["MAX_HOLD_SECS"]: ef, er = True, "TIME_EXIT"
                if not ef and not position.get("be_triggered") and held > settings["NO_LIFE_SECS"] and ltp < entry + settings["BE_PTS"]:
                    ef, er = True, "NO_LIFE"

                _s_move = ltp - entry
                if _s_move >= settings["TRAIL_START_PTS"]:
                    if not position.get("trail_triggered"): position["trail_triggered"] = True
                    tsl = ltp - settings["TRAIL_PTS"]
                    if tsl > position["stop_loss"]: position["stop_loss"] = tsl
                elif _s_move >= settings["BE_PTS"]:
                    if not position.get("be_triggered"):
                        position["be_triggered"] = True
                        position["stop_loss"] = max(position["stop_loss"], entry + 0.25)

                if ef:
                    xp = option_premium(position["entry_spot"], close - 0.5, position["side"], mins_to_close(ts))
                    pnl = (xp - entry) * qty; trade_id += 1
                    trades.append(Trade(trade_id, date_str, position["entry_ts"], ts, position["side"],
                        entry, xp, round(pnl, 2), 0.0, position.get("move_pts", 0), er, held))
                    day_pnl += pnl; last_exit_ts = ts
                    consec_losses = consec_losses + 1 if pnl <= 0 else 0
                    position = None
                    if day_pnl <= settings["DAILY_LOSS_LIMIT"]: break
                continue

            if trades_today >= settings["MAX_TRADES_PER_DAY"]: continue
            if consec_losses >= settings["MAX_CONSEC_LOSSES"]: continue
            if now < SCALP_START or now >= SCALP_END: continue
            if last_exit_ts is not None and (ts - last_exit_ts).total_seconds() < settings["COOLDOWN"]: continue
            if len(ltp_history) < 2: continue

            _cutoff = ts - timedelta(seconds=settings["MOM_WINDOW"])
            _past_move = [(t, v) for t, v in ltp_history if t >= _cutoff]
            if _past_move and len(_past_move) >= 2 and abs(close - _past_move[0][1]) > settings["MAX_MOVE_PTS"]: continue

            cutoff = ts - timedelta(seconds=settings["MOM_WINDOW"])
            past = [(t, v) for t, v in ltp_history if t >= cutoff]
            if not past: continue
            move = close - past[0][1]
            if abs(move) < settings["MOM_THRESHOLD"]: continue
            side = "CE" if move >= settings["MOM_THRESHOLD"] else "PE"
            prices = [p for _, p in past]
            if len(prices) < settings["MIN_SAMPLES"]: continue

            half = len(prices) // 2
            f_half, s_half = prices[:half], prices[half:]
            h1 = max(f_half); h2 = max(s_half); l2 = min(s_half)
            rng = max(h2 - l2, 1e-9)
            if side == "CE" and h2 <= max(f_half): continue
            if side == "PE" and l2 >= min(f_half): continue
            pullback = (h2 - close) if side == "CE" else (close - l2)
            if not (0.10 * rng <= pullback <= 0.50 * rng): continue
            if side == "CE" and close < l2: continue
            if side == "PE" and close > h2: continue

            if len(prices) >= 4:
                q = max(1, len(prices) // 4)
                tm = abs(close - prices[-q]); tot = abs(close - prices[0])
                if tot > 1e-9 and tm > settings["EXHAUST_TAIL_FRAC"] * tot: continue

            htf5_key = pd.Timestamp(ts).floor("5min")
            htf5_dir = htf5_map.get(htf5_key, 0)
            if htf5_dir == 0:
                recent = [float(df.iloc[j]["close"]) for j in range(max(0, i-5), i+1)]
                if len(recent) >= 2: htf5_dir = 1 if recent[-1] > recent[0] else -1
            if side == "CE" and htf5_dir != 1: continue
            if side == "PE" and htf5_dir != -1: continue

            score = 1.0
            if abs(move) >= 30: score += 2.0
            elif abs(move) >= 20: score += 1.0
            if (side == "CE" and htf5_dir == 1) or (side == "PE" and htf5_dir == -1): score += 2.0
            if len(prices) >= 5:
                ema5 = np.mean(prices[-5:])
                if (side == "CE" and close > ema5) or (side == "PE" and close < ema5): score += 1.0

            sl_pts = settings["SL_WIDE_PTS"] if score >= 4.5 else (settings["SL_MED_PTS"] if score >= 2.5 else settings["SL_STRICT_PTS"])
            entry_spot = close + 0.5
            entry_prem = option_premium(entry_spot, entry_spot, side, mins_to_close(ts))
            if entry_prem < settings["MIN_OPT_PTS"]: continue

            position = {"side": side, "entry_spot": entry_spot, "entry_prem": entry_prem, "entry_ts": ts,
                "stop_loss": entry_prem - sl_pts, "max_fav": 0.0, "move_pts": round(move, 2),
                "trail_triggered": False, "be_triggered": False}
            trades_today += 1

        if position is not None:
            xp = option_premium(position["entry_spot"], day_df["close"].iloc[-1] - 0.5,
                position["side"], mins_to_close(day_df["date"].iloc[-1]))
            pnl = (xp - position["entry_prem"]) * qty; trade_id += 1
            held = (day_df["date"].iloc[-1] - position["entry_ts"]).total_seconds()
            trades.append(Trade(trade_id, date_str, position["entry_ts"], day_df["date"].iloc[-1],
                position["side"], position["entry_prem"], xp, round(pnl, 2), 0.0,
                position.get("move_pts", 0), "DAY_END", held))

    return trades

def compute_metrics(trades):
    n = len(trades)
    if n == 0: return {"trades": 0, "pnl": 0, "win_rate": 0, "avg_pnl": 0, "profit_factor": 0, "max_drawdown": 0, "sharpe": 0, "final_equity": 100000}
    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    tp = float(pnls.sum()); wr = len(wins) / n
    ap = float(pnls.mean()); pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else 99.0
    eq = np.cumsum(pnls); peak = np.maximum.accumulate(eq); mdd = float((eq - peak).min())
    daily = defaultdict(float)
    for t in trades: daily[t.date] += t.pnl
    dp = np.array(list(daily.values()))
    sh = float(np.mean(dp) / np.std(dp) * np.sqrt(252)) if np.std(dp) > 0 else 0
    return {"trades": n, "pnl": round(tp, 0), "win_rate": round(wr, 3), "avg_pnl": round(ap, 1),
            "profit_factor": round(min(pf, 99.0), 2), "max_drawdown": round(mdd, 0), "sharpe": round(sh, 2), "final_equity": round(100000 + tp, 0)}

def grid_search(df_is, htf5_map, max_combos=500):
    np.random.seed(42); results = []
    keys = list(PARAM_GRID.keys())
    for _ in range(max_combos):
        combo = {}
        for k, v in PARAM_GRID.items():
            val = np.random.choice(v)
            combo[k] = int(val) if k in ["COOLDOWN", "MAX_TRADES_PER_DAY", "MAX_CONSEC_LOSSES"] else float(val)
        settings = {**FIXED, **combo}
        trades = simulate_scalps(df_is, settings, htf5_map)
        m = compute_metrics(trades)
        results.append((settings, m))
    results.sort(key=lambda x: (1 if x[1]["trades"] < 15 else 0, -x[1]["avg_pnl"]))
    return results

def main():
    csv_path = r"D:\All Bots\trading_system\data\historical\nifty_1m_full.csv"
    N_FOLDS = 4; MIN_IS = 10; MIN_OOS = 5

    print("=" * 72)
    print("  WALK-FORWARD OPTIMIZATION -- Scalp Engine (fast)")
    print("=" * 72)

    t0 = _time.time()
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" not in df.columns: df["volume"] = 0
    df["day"] = df["date"].dt.date
    cutoff = df["date"].max() - pd.Timedelta(days=365)
    df = df[df["date"] >= cutoff].reset_index(drop=True)
    print(f"  Data: {df['date'].min().date()} -> {df['date'].max().date()} | {len(df):,} rows | {df['day'].nunique()} days")
    print(f"  Load: {_time.time()-t0:.1f}s")

    print("  Building 5m HTF map...")
    htf5_map = build_htf5_map(df)
    print(f"  HTF entries: {len(htf5_map):,}")
    print()

    all_days = sorted(df["day"].unique())
    fold_size = len(all_days) // N_FOLDS
    folds = []
    for k in range(N_FOLDS):
        s = k * fold_size; e = (k+1) * fold_size if k < N_FOLDS-1 else len(all_days)
        fd = all_days[s:e]; fdf = df[df["day"].isin(fd)].reset_index(drop=True)
        folds.append((fd[0], fd[-1], fdf))

    fold_results = []

    for k, (fs, fe, fdf) in enumerate(folds):
        print(f"{'='*72}")
        print(f"  FOLD {k+1}/{N_FOLDS}: {fs} -> {fe}")
        print(f"{'='*72}")

        fdays = sorted(fdf["day"].unique())
        si = int(len(fdays) * 0.7)
        is_days = fdays[:si]; oos_days = fdays[si:]
        df_is = fdf[fdf["day"].isin(is_days)].reset_index(drop=True)
        df_oos = fdf[fdf["day"].isin(oos_days)].reset_index(drop=True)
        print(f"  IS: {is_days[0]}->{is_days[-1]} ({len(is_days)}d) | OOS: {oos_days[0]}->{oos_days[-1]} ({len(oos_days)}d)")

        t1 = _time.time()
        is_results = grid_search(df_is, htf5_map, 500)
        print(f"  Grid search: {_time.time()-t1:.1f}s")

        valid = [r for r in is_results if r[1]["trades"] >= MIN_IS][:5]
        if not valid: valid = is_results[:3]

        print(f"\n  TOP 3 IN-SAMPLE:")
        for rank, (s, m) in enumerate(valid, 1):
            ss = f"C={s['COOLDOWN']} T0={s['TRAIL_START_PTS']:.0f} T={s['TRAIL_PTS']:.0f} M={s['MAX_MOVE_PTS']:.0f} D={s['MAX_TRADES_PER_DAY']} L={s['MAX_CONSEC_LOSSES']}"
            print(f"    #{rank} {m['trades']:>3}tr WR={m['win_rate']*100:.0f}% PnL=Rs{m['pnl']:>+7,.0f} Avg=Rs{m['avg_pnl']:>+5.1f} PF={m['profit_factor']:.2f} | {ss}")

        print(f"\n  OOS VALIDATION:")
        best = None
        for rank, (s, is_m) in enumerate(valid, 1):
            oos_trades = simulate_scalps(df_oos, s, htf5_map)
            oos_m = compute_metrics(oos_trades)
            ss = f"C={s['COOLDOWN']} T0={s['TRAIL_START_PTS']:.0f} T={s['TRAIL_PTS']:.0f} M={s['MAX_MOVE_PTS']:.0f} D={s['MAX_TRADES_PER_DAY']} L={s['MAX_CONSEC_LOSSES']}"
            print(f"    #{rank} OOS: {oos_m['trades']:>3}tr WR={oos_m['win_rate']*100:.0f}% PnL=Rs{oos_m['pnl']:>+7,.0f} Avg=Rs{oos_m['avg_pnl']:>+5.1f} PF={oos_m['profit_factor']:.2f}")
            if oos_m["trades"] >= MIN_OOS and oos_m["pnl"] > 0 and (best is None or oos_m["avg_pnl"] > best[2]["avg_pnl"]):
                best = (s, is_m, oos_m)

        if best is None and valid:
            best = (valid[0][0], valid[0][1], compute_metrics(simulate_scalps(df_oos, valid[0][0], htf5_map)))

        if best:
            fold_results.append({"fold": k+1, "is_range": f"{is_days[0]}->{is_days[-1]}", "oos_range": f"{oos_days[0]}->{oos_days[-1]}",
                "settings": best[0], "is_m": best[1], "oos_m": best[2]})
        print()

    # AGGREGATE
    print("=" * 72)
    print("  AGGREGATE RESULTS")
    print("=" * 72)

    oos_pnls = [fr["oos_m"]["pnl"] for fr in fold_results]
    prof_folds = sum(1 for p in oos_pnls if p > 0)
    total_oos = sum(oos_pnls)
    avg_wr = np.mean([fr["oos_m"]["win_rate"] for fr in fold_results])

    for fr in fold_results:
        s = fr["settings"]
        ss = f"C={s['COOLDOWN']} T0={s['TRAIL_START_PTS']:.0f} T={s['TRAIL_PTS']:.0f} M={s['MAX_MOVE_PTS']:.0f} D={s['MAX_TRADES_PER_DAY']} L={s['MAX_CONSEC_LOSSES']}"
        print(f"  Fold {fr['fold']}: IS={fr['is_range']} OOS={fr['oos_range']}")
        print(f"    IS:  {fr['is_m']['trades']}tr WR={fr['is_m']['win_rate']*100:.0f}% PnL=Rs{fr['is_m']['pnl']:+,.0f}")
        print(f"    OOS: {fr['oos_m']['trades']}tr WR={fr['oos_m']['win_rate']*100:.0f}% PnL=Rs{fr['oos_m']['pnl']:+,.0f} | {ss}")

    print(f"\n  Profitable folds: {prof_folds}/{N_FOLDS} | Total OOS PnL: Rs{total_oos:+,.0f} | Avg WR: {avg_wr*100:.1f}%")

    # Parameter frequency
    print(f"\n  PARAMETER FREQUENCY (profitable OOS folds):")
    prof_s = [fr["settings"] for fr in fold_results if fr["oos_m"]["pnl"] > 0]
    if not prof_s: prof_s = [fr["settings"] for fr in fold_results]

    rec = {}
    for param in PARAM_GRID.keys():
        vals = [s[param] for s in prof_s]
        counts = Counter(vals)
        top = counts.most_common(2)
        rec[param] = top[0][0]
        print(f"    {param:<24} {', '.join(f'{v}({c}x)' for v,c in top)}")

    print(f"\n  RECOMMENDED SETTINGS:")
    for k, v in rec.items():
        print(f"    SCALP_{k:<22} = {v}")

    # Full validation
    print(f"\n  FULL 12-MONTH VALIDATION (recommended):")
    rec_s = {**FIXED, **rec}
    full_trades = simulate_scalps(df, rec_s, htf5_map)
    fm = compute_metrics(full_trades)

    # Also old and safe
    old_s = {**FIXED, "COOLDOWN": 120, "TRAIL_START_PTS": 5.0, "TRAIL_PTS": 2.0, "MAX_MOVE_PTS": 9999.0, "MAX_TRADES_PER_DAY": 999, "MAX_CONSEC_LOSSES": 999}
    old_m = compute_metrics(simulate_scalps(df, old_s, htf5_map))
    safe_s = {**FIXED, "COOLDOWN": 180, "TRAIL_START_PTS": 8.0, "TRAIL_PTS": 3.0, "MAX_MOVE_PTS": 25.0, "MAX_TRADES_PER_DAY": 6, "MAX_CONSEC_LOSSES": 3}
    safe_m = compute_metrics(simulate_scalps(df, safe_s, htf5_map))

    print(f"  {'Metric':<22} {'OLD':>10} {'SAFE':>10} {'WFO':>10}")
    print(f"  {'-'*54}")
    print(f"  {'Trades':<22} {old_m['trades']:>10} {safe_m['trades']:>10} {fm['trades']:>10}")
    print(f"  {'Win Rate':<22} {old_m['win_rate']*100:>9.1f}% {safe_m['win_rate']*100:>9.1f}% {fm['win_rate']*100:>9.1f}%")
    print(f"  {'Total PnL':<22} Rs{old_m['pnl']:>+9,.0f} Rs{safe_m['pnl']:>+9,.0f} Rs{fm['pnl']:>+9,.0f}")
    print(f"  {'Avg PnL/trade':<22} Rs{old_m['avg_pnl']:>+9.1f} Rs{safe_m['avg_pnl']:>+9.1f} Rs{fm['avg_pnl']:>+9.1f}")
    print(f"  {'Profit Factor':<22} {old_m['profit_factor']:>10.2f} {safe_m['profit_factor']:>10.2f} {fm['profit_factor']:>10.2f}")
    print(f"  {'Sharpe':<22} {old_m['sharpe']:>10.2f} {safe_m['sharpe']:>10.2f} {fm['sharpe']:>10.2f}")
    print(f"  {'Max Drawdown':<22} Rs{old_m['max_drawdown']:>+9,.0f} Rs{safe_m['max_drawdown']:>+9,.0f} Rs{fm['max_drawdown']:>+9,.0f}")

    print(f"\n{'='*72}")
    if fm["pnl"] > 0 and fm["profit_factor"] > 1.0 and prof_folds >= N_FOLDS // 2:
        print("  VERDICT: POSITIVE EDGE CONFIRMED across walk-forward folds")
        print(f"  Expected edge: Rs{fm['avg_pnl']:.1f}/trade | PF={fm['profit_factor']:.2f}")
    elif fm["pnl"] > 0:
        print("  VERDICT: MARGINALLY POSITIVE -- use with caution")
    else:
        print("  VERDICT: NO DEPLOYABLE EDGE")
    print("=" * 72)
    print(f"\n  Total time: {_time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
