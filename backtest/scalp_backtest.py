#!/usr/bin/env python3
"""
Scalp Engine Backtest — OLD vs NEW settings comparison.

Simulates the SCALP_MOM momentum strategy on NIFTY 1-minute historical data
and compares the impact of Aug-18 fixes:

  OLD: no circuit breaker, no daily cap, no exhaustion filter,
       120s cooldown, 5pt trail start, 2pt trail distance
  NEW: 3-loss circuit breaker, 6 trade/day cap, 25pt exhaustion cap,
       180s cooldown, 8pt trail start, 3pt trail distance

Both runs use identical entry logic (momentum + structure + pullback + exhaustion
tail filter) — only the risk controls differ.

NOTE: Live engine ticks every 2s (15 ticks per 30s window). With 1-minute bars,
the window is scaled to 5min (5 ticks) and MIN_SAMPLES to 3.
"""

import os
import sys
import logging
import time as _time
from datetime import datetime, time as dtime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional, List

import numpy as np
import pandas as pd

# Force UTF-8 output on Windows
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING)

# ================================================================
# SETTINGS
# ================================================================

# For 1-minute data simulation:
# - Window scaled 30s -> 300s (5min) to get ~5 ticks per window
# - MIN_SAMPLES scaled 6 -> 3 for 1m data resolution
# - All other risk controls remain the same as live settings

OLD_SETTINGS = {
    "COOLDOWN":            120,
    "TRAIL_START_PTS":     5.0,
    "TRAIL_PTS":           2.0,
    "BE_PTS":              2.0,
    "SL_STRICT_PTS":       3.0,
    "SL_MED_PTS":          5.0,
    "SL_WIDE_PTS":         8.0,
    "TARGET_PTS":          50.0,
    "MAX_HOLD_SECS":       180,
    "NO_LIFE_SECS":        35,
    "MOM_WINDOW":          300,    # 5min for 1m data (live: 30s = 15 ticks)
    "MOM_THRESHOLD":       20.0,
    "MIN_SAMPLES":         3,      # 3 for 1m data (live: 6 for 2s ticks)
    "EXHAUST_TAIL_FRAC":   0.65,
    "MAX_MOVE_PTS":        9999.0, # OLD: no exhaustion cap
    "MAX_TRADES_PER_DAY":  999,    # OLD: no daily limit
    "MAX_CONSEC_LOSSES":   999,    # OLD: no circuit breaker
    "ML_MIN_PROB":         0.0,    # OLD: no ML gating
    "LOT_SIZE":            30,
    "LOTS_PER_TRADE":      2,
    "MIN_OPT_PTS":         30.0,
}

NEW_SETTINGS = {
    "COOLDOWN":            180,
    "TRAIL_START_PTS":     8.0,
    "TRAIL_PTS":           3.0,
    "BE_PTS":              2.0,
    "SL_STRICT_PTS":       3.0,
    "SL_MED_PTS":          5.0,
    "SL_WIDE_PTS":         8.0,
    "TARGET_PTS":          50.0,
    "MAX_HOLD_SECS":       180,
    "NO_LIFE_SECS":        35,
    "MOM_WINDOW":          300,    # 5min for 1m data
    "MOM_THRESHOLD":       20.0,
    "MIN_SAMPLES":         3,      # 3 for 1m data
    "EXHAUST_TAIL_FRAC":   0.65,
    "MAX_MOVE_PTS":        25.0,   # NEW: exhaustion cap
    "MAX_TRADES_PER_DAY":  6,      # NEW: daily trade limit
    "MAX_CONSEC_LOSSES":   3,      # NEW: circuit breaker
    "ML_MIN_PROB":         0.42,   # NEW: ML gating (simulated)
    "LOT_SIZE":            30,
    "LOTS_PER_TRADE":      2,
    "MIN_OPT_PTS":         30.0,
}

# ================================================================
# MARKET CONSTANTS
# ================================================================

MARKET_OPEN  = dtime(9, 15)
SCALP_START  = dtime(9, 30)
SCALP_END    = dtime(15, 10)
MARKET_CLOSE = dtime(15, 30)


# ================================================================
# OPTION PRICE SIMULATOR
# ================================================================

def option_premium(entry_spot, cur_spot, side, mins_to_close):
    base_premium = 150.0
    atm_vol = 0.12
    delta = 0.5
    T = max(mins_to_close / (375 * 252), 1e-6)
    time_val = base_premium * (T ** 0.5) * atm_vol * 100
    favorable = (cur_spot - entry_spot) if side == "CE" else (entry_spot - cur_spot)
    return round(max(time_val + delta * favorable, 1.0), 2)


def mins_to_close(ts):
    return max((MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute) - (ts.hour * 60 + ts.minute), 1)


# ================================================================
# SIMPLE 5m SUPERTREND
# ================================================================

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
    upper = mid + multiplier * atr
    lower = mid - multiplier * atr

    st_line = np.copy(closes)
    for i in range(1, len(closes)):
        if closes[i] > st_line[i-1]:
            st_line[i] = max(lower[i], st_line[i-1])
        else:
            st_line[i] = min(upper[i], st_line[i-1])

        if closes[i] > st_line[i-1]:
            st_dir[i] = 1
        else:
            st_dir[i] = -1

    return st_dir


# ================================================================
# SIMULATED ML PROBABILITY
# ================================================================

def simulate_ml_prob(side, move_pts, htf5_dir, rsi):
    prob = 0.5
    if abs(move_pts) >= 30:
        prob += 0.15
    elif abs(move_pts) >= 25:
        prob += 0.10
    elif abs(move_pts) >= 20:
        prob += 0.05
    if (side == "CE" and htf5_dir == 1) or (side == "PE" and htf5_dir == -1):
        prob += 0.12
    elif htf5_dir != 0:
        prob -= 0.10
    if side == "CE" and 40 < rsi < 70:
        prob += 0.05
    elif side == "PE" and 30 < rsi < 60:
        prob += 0.05
    if side == "CE" and rsi > 75:
        prob -= 0.15
    elif side == "PE" and rsi < 25:
        prob -= 0.15
    return round(max(0.1, min(0.95, prob)), 3)


# ================================================================
# SCALP BACKTEST ENGINE
# ================================================================

@dataclass
class ScalpTrade:
    trade_id:   int
    date:       str
    entry_time: datetime
    exit_time:  datetime
    side:       str
    entry_prem: float
    exit_prem:  float
    qty:        int
    pnl:        float
    ml_prob:    float
    move_pts:   float
    exit_reason: str
    sl_mode:    str
    held_secs:  float
    max_favorable: float


@dataclass
class DayResult:
    date:   str
    trades: int
    pnl:    float
    wins:   int
    losses: int
    killed: bool
    consec_losses: int
    daily_cap_hit: bool


def run_scalp_backtest(df, settings, label=""):
    trades = []
    day_results = []
    trade_id = 0

    # Pre-compute 5-minute Supertrend from 1m data
    df_5m = df.set_index("date").resample("5min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna().reset_index()
    htf5_st = compute_5m_supertrend(df_5m)
    htf5_map = {}
    for idx in range(len(df_5m)):
        ts_5m = df_5m.iloc[idx]["date"]
        key = pd.Timestamp(ts_5m).floor("5min")
        htf5_map[key] = int(htf5_st[idx]) if idx < len(htf5_st) else 0

    all_days = sorted(df["day"].unique())
    lot_size = settings["LOT_SIZE"]
    qty = lot_size * settings["LOTS_PER_TRADE"]
    ml_simulated = settings["ML_MIN_PROB"] > 0

    for trading_day in all_days:
        day_df = df[df["day"] == trading_day].copy().reset_index(drop=True)
        date_str = str(trading_day)

        position = None
        last_exit_ts = None
        trades_today = 0
        day_pnl = 0.0
        consec_losses = 0
        day_cap_hit = False
        day_killed = False

        ltp_history = deque(maxlen=120)

        for i in range(len(day_df)):
            row = day_df.iloc[i]
            ts = row["date"]
            now = ts.time()
            close = float(row["close"])

            # Skip pre-market
            if now < MARKET_OPEN:
                ltp_history.append((ts, close))
                continue

            # Force exit at 15:15
            if now >= dtime(15, 15) and position is not None:
                exit_prem = option_premium(position["entry_spot"], close - 0.5,
                                           position["side"], mins_to_close(ts))
                pnl = (exit_prem - position["entry_prem"]) * qty
                trade_id += 1
                held = (ts - position["entry_ts"]).total_seconds()
                max_fav = max(position.get("max_fav", 0.0), exit_prem - position["entry_prem"])
                trades.append(ScalpTrade(
                    trade_id=trade_id, date=date_str, entry_time=position["entry_ts"],
                    exit_time=ts, side=position["side"], entry_prem=position["entry_prem"],
                    exit_prem=exit_prem, qty=qty, pnl=round(pnl, 2),
                    ml_prob=position.get("ml_prob", 0.0), move_pts=position.get("move_pts", 0),
                    exit_reason="TIME_CLOSE", sl_mode=position.get("sl_mode", "STRICT"),
                    held_secs=held, max_favorable=round(max_fav, 2)
                ))
                day_pnl += pnl
                if pnl > 0:
                    consec_losses = 0
                else:
                    consec_losses += 1
                position = None
                ltp_history.append((ts, close))
                continue

            ltp_history.append((ts, close))

            # POSITION MANAGEMENT
            if position is not None:
                cur_spot = close
                ltp = option_premium(position["entry_spot"], cur_spot,
                                     position["side"], mins_to_close(ts))
                held = (ts - position["entry_ts"]).total_seconds()
                entry = position["entry_prem"]
                sl = position["stop_loss"]
                max_fav = max(position.get("max_fav", 0.0), ltp - entry)
                position["max_fav"] = max_fav

                exit_flag = False
                exit_reason = ""

                if ltp <= sl:
                    exit_flag = True
                    exit_reason = "STOP"
                if not exit_flag and ltp >= entry + settings["TARGET_PTS"]:
                    exit_flag = True
                    exit_reason = "TARGET"
                if not exit_flag and held > settings["MAX_HOLD_SECS"]:
                    exit_flag = True
                    exit_reason = "TIME_EXIT"
                if (not exit_flag
                        and not position.get("be_triggered")
                        and held > settings["NO_LIFE_SECS"]
                        and ltp < entry + settings["BE_PTS"]):
                    exit_flag = True
                    exit_reason = "NO_LIFE"

                # TRAILING STOP
                _s_move = ltp - entry
                if _s_move >= settings["TRAIL_START_PTS"]:
                    if not position.get("trail_triggered"):
                        position["trail_triggered"] = True
                    trail_sl = ltp - settings["TRAIL_PTS"]
                    if trail_sl > position["stop_loss"]:
                        position["stop_loss"] = trail_sl
                elif _s_move >= settings["BE_PTS"]:
                    if not position.get("be_triggered"):
                        position["be_triggered"] = True
                        position["stop_loss"] = max(position["stop_loss"], entry + 0.25)

                if exit_flag:
                    exit_spot = cur_spot - 0.5
                    exit_prem = option_premium(position["entry_spot"], exit_spot,
                                               position["side"], mins_to_close(ts))
                    pnl = (exit_prem - entry) * qty
                    trade_id += 1
                    max_fav = max(position.get("max_fav", 0.0), exit_prem - entry)
                    trades.append(ScalpTrade(
                        trade_id=trade_id, date=date_str, entry_time=position["entry_ts"],
                        exit_time=ts, side=position["side"], entry_prem=entry,
                        exit_prem=exit_prem, qty=qty, pnl=round(pnl, 2),
                        ml_prob=position.get("ml_prob", 0.0), move_pts=position.get("move_pts", 0),
                        exit_reason=exit_reason, sl_mode=position.get("sl_mode", "STRICT"),
                        held_secs=held, max_favorable=round(max_fav, 2)
                    ))
                    day_pnl += pnl
                    last_exit_ts = ts
                    if pnl > 0:
                        consec_losses = 0
                    else:
                        consec_losses += 1
                    position = None
                    if day_pnl <= -2000:
                        day_killed = True
                        break

            # ENTRY LOGIC
            if position is not None:
                continue
            if trades_today >= settings["MAX_TRADES_PER_DAY"]:
                day_cap_hit = True
                continue
            if consec_losses >= settings["MAX_CONSEC_LOSSES"]:
                continue
            if now < SCALP_START or now >= SCALP_END:
                continue

            if last_exit_ts is not None:
                elapsed = (ts - last_exit_ts).total_seconds()
                if elapsed < settings["COOLDOWN"]:
                    continue

            if len(ltp_history) < 2:
                continue

            # EXHAUSTION CAP
            _cutoff_move = ts - timedelta(seconds=settings["MOM_WINDOW"])
            _past_move = [(t, ltp) for t, ltp in ltp_history if t >= _cutoff_move]
            if _past_move and len(_past_move) >= 2:
                _earliest = _past_move[0][1]
                _total_move = abs(close - _earliest)
                if _total_move > settings["MAX_MOVE_PTS"]:
                    continue

            # MOMENTUM DETECTION
            cutoff = ts - timedelta(seconds=settings["MOM_WINDOW"])
            past = [(t, ltp) for t, ltp in ltp_history if t >= cutoff]
            if not past:
                continue

            earliest_ltp = past[0][1]
            move = close - earliest_ltp

            if abs(move) < settings["MOM_THRESHOLD"]:
                continue

            side = "CE" if move >= settings["MOM_THRESHOLD"] else "PE"

            # STRUCTURE + PULLBACK + EXHAUSTION CONFIRMATION
            prices = [p for _, p in past]
            if len(prices) < settings["MIN_SAMPLES"]:
                continue

            half = len(prices) // 2
            first_half, second_half = prices[:half], prices[half:]
            h1, l1 = max(first_half), min(first_half)
            h2, l2 = max(second_half), min(second_half)
            rng = max(h2 - l2, 1e-9)

            if side == "CE" and h2 <= h1:
                continue
            if side == "PE" and l2 >= l1:
                continue

            pullback = (h2 - close) if side == "CE" else (close - l2)
            if not (0.10 * rng <= pullback <= 0.50 * rng):
                continue
            if side == "CE" and close < l2:
                continue
            if side == "PE" and close > h2:
                continue

            if len(prices) >= 4:
                q = max(1, len(prices) // 4)
                tail_move = abs(close - prices[-q])
                total_move = abs(close - prices[0])
                if total_move > 1e-9 and tail_move > settings["EXHAUST_TAIL_FRAC"] * total_move:
                    continue

            # HTF AGREEMENT
            htf5_key = pd.Timestamp(ts).floor("5min")
            htf5_dir = htf5_map.get(htf5_key, 0)
            if htf5_dir == 0 and len(df_5m) > 1:
                recent_5m = df_5m[df_5m["date"] <= ts].tail(3)
                if len(recent_5m) >= 2:
                    if recent_5m["close"].iloc[-1] > recent_5m["close"].iloc[0]:
                        htf5_dir = 1
                    else:
                        htf5_dir = -1

            if side == "CE" and htf5_dir != 1:
                continue
            if side == "PE" and htf5_dir != -1:
                continue

            # ML PROBABILITY
            ml_prob = 0.0
            if ml_simulated:
                rsi = 50.0
                if len(prices) >= 15:
                    gains, losses_r = [], []
                    for j in range(-14, 0):
                        d = prices[j] - prices[j-1]
                        if d > 0:
                            gains.append(abs(d))
                        else:
                            losses_r.append(abs(d))
                    avg_g = np.mean(gains) if gains else 1e-6
                    avg_l = np.mean(losses_r) if losses_r else 1e-6
                    rsi = 100 - (100 / (1 + avg_g / avg_l))
                ml_prob = simulate_ml_prob(side, move, htf5_dir, rsi)
                if ml_prob < settings["ML_MIN_PROB"]:
                    continue

            # ADAPTIVE SL
            score = 0.0
            if abs(move) >= 30:
                score += 2.0
            elif abs(move) >= 20:
                score += 1.0
            if (side == "CE" and htf5_dir == 1) or (side == "PE" and htf5_dir == -1):
                score += 2.0
            elif htf5_dir != 0:
                score += 0.5
            if len(prices) >= 20:
                ema20 = np.mean(prices[-20:])
                if (side == "CE" and close > ema20) or (side == "PE" and close < ema20):
                    score += 1.0
            if ml_simulated:
                score += 1.0

            if score >= 4.5:
                sl_pts = settings["SL_WIDE_PTS"]
                sl_mode = "WIDE"
            elif score >= 2.5:
                sl_pts = settings["SL_MED_PTS"]
                sl_mode = "MED"
            else:
                sl_pts = settings["SL_STRICT_PTS"]
                sl_mode = "STRICT"

            entry_spot = close + 0.5
            entry_prem = option_premium(entry_spot, entry_spot, side, mins_to_close(ts))

            if entry_prem < settings["MIN_OPT_PTS"]:
                continue

            stop_loss = entry_prem - sl_pts
            target = entry_prem + settings["TARGET_PTS"]

            position = {
                "side": side,
                "entry_spot": entry_spot,
                "entry_prem": entry_prem,
                "entry_ts": ts,
                "stop_loss": stop_loss,
                "target": target,
                "max_pnl": 0.0,
                "max_fav": 0.0,
                "ml_prob": ml_prob,
                "move_pts": round(move, 2),
                "sl_mode": sl_mode,
                "trail_triggered": False,
                "be_triggered": False,
            }
            trades_today += 1

        # End of day force-close
        if position is not None:
            exit_spot = day_df["close"].iloc[-1] - 0.5
            exit_prem = option_premium(position["entry_spot"], exit_spot,
                                       position["side"], mins_to_close(day_df["date"].iloc[-1]))
            pnl = (exit_prem - position["entry_prem"]) * qty
            trade_id += 1
            held = (day_df["date"].iloc[-1] - position["entry_ts"]).total_seconds()
            max_fav = max(position.get("max_fav", 0.0), exit_prem - position["entry_prem"])
            trades.append(ScalpTrade(
                trade_id=trade_id, date=date_str, entry_time=position["entry_ts"],
                exit_time=day_df["date"].iloc[-1], side=position["side"],
                entry_prem=position["entry_prem"], exit_prem=exit_prem,
                qty=qty, pnl=round(pnl, 2),
                ml_prob=position.get("ml_prob", 0.0), move_pts=position.get("move_pts", 0),
                exit_reason="DAY_END", sl_mode=position.get("sl_mode", "STRICT"),
                held_secs=held, max_favorable=round(max_fav, 2)
            ))
            day_pnl += pnl
            if pnl > 0:
                consec_losses = 0
            else:
                consec_losses += 1

        day_results.append(DayResult(
            date=date_str, trades=trades_today, pnl=round(day_pnl, 2),
            wins=sum(1 for t in trades if t.date == date_str and t.pnl > 0),
            losses=sum(1 for t in trades if t.date == date_str and t.pnl <= 0),
            killed=day_killed, consec_losses=consec_losses, daily_cap_hit=day_cap_hit
        ))

    return trades, day_results


# ================================================================
# METRICS
# ================================================================

def compute_metrics(trades, day_results, label):
    n = len(trades)
    if n == 0:
        return {"label": label, "total_trades": 0, "total_pnl": 0, "win_rate": 0,
                "wins": 0, "losses": 0, "avg_win": 0, "avg_loss": 0, "avg_pnl": 0,
                "profit_factor": 0, "sharpe": 0, "max_drawdown": 0, "avg_held_secs": 0,
                "avg_mfe_pts": 0, "avg_move_pts": 0, "max_consec_losses": 0,
                "trading_days": 0, "days_killed": 0, "days_cap_hit": 0,
                "by_exit_reason": {}, "by_sl_mode": {}, "final_equity": 100000}

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)

    win_rate = len(wins) / n
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    avg_pnl = np.mean(pnls)
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    avg_held = np.mean([t.held_secs for t in trades])
    avg_mfe = np.mean([t.max_favorable for t in trades])
    avg_move = np.mean([t.move_pts for t in trades])

    by_exit = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        by_exit[t.exit_reason]["n"] += 1
        by_exit[t.exit_reason]["pnl"] += t.pnl

    by_sl = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        by_sl[t.sl_mode]["n"] += 1
        by_sl[t.sl_mode]["pnl"] += t.pnl
        by_sl[t.sl_mode]["wins"] += 1 if t.pnl > 0 else 0

    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t.date] += t.pnl
    dp = np.array(list(daily_pnl.values()))
    sharpe = (np.mean(dp) / np.std(dp) * np.sqrt(252)) if np.std(dp) > 0 else 0

    equity = 100000
    peak = equity
    max_dd = 0
    for d in day_results:
        equity += d.pnl
        peak = max(peak, equity)
        dd = equity - peak
        max_dd = min(max_dd, dd)

    max_consec = 0
    cur_consec = 0
    for t in trades:
        if t.pnl <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    return {
        "label": label,
        "total_trades": n,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 3),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_pnl": round(avg_pnl, 2),
        "profit_factor": round(profit_factor, 3),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 2),
        "avg_held_secs": round(avg_held, 1),
        "avg_mfe_pts": round(avg_mfe, 2),
        "avg_move_pts": round(avg_move, 2),
        "max_consec_losses": max_consec,
        "trading_days": len(day_results),
        "days_killed": sum(1 for d in day_results if d.killed),
        "days_cap_hit": sum(1 for d in day_results if d.daily_cap_hit),
        "by_exit_reason": dict(by_exit),
        "by_sl_mode": dict(by_sl),
        "final_equity": round(100000 + total_pnl, 2),
    }


# ================================================================
# MAIN
# ================================================================

def main():
    csv_path = r"D:\All Bots\trading_system\data\historical\nifty_1m_full.csv"

    print("=" * 72)
    print("  SCALP ENGINE BACKTEST -- OLD vs NEW Settings")
    print("=" * 72)
    print(f"  Data: {csv_path}")
    print()

    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0
    df["day"] = df["date"].dt.date

    # Filter to last 12 months
    cutoff = df["date"].max() - pd.Timedelta(days=365)
    df = df[df["date"] >= cutoff].reset_index(drop=True)
    print(f"  Data range: {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"  Rows: {len(df):,} | Trading days: {df['day'].nunique()}")
    print()

    # Run OLD settings
    t0 = _time.time()
    old_trades, old_days = run_scalp_backtest(df, OLD_SETTINGS, "OLD")
    old_time = _time.time() - t0
    old_m = compute_metrics(old_trades, old_days, "OLD (pre-fix)")
    print(f"  OLD settings completed in {old_time:.1f}s")

    # Run NEW settings
    t0 = _time.time()
    new_trades, new_days = run_scalp_backtest(df, NEW_SETTINGS, "NEW")
    new_time = _time.time() - t0
    new_m = compute_metrics(new_trades, new_days, "NEW (post-fix)")
    print(f"  NEW settings completed in {new_time:.1f}s")
    print()

    # Print comparison
    print("=" * 72)
    print("  COMPARISON TABLE")
    print("=" * 72)
    print(f"  {'Metric':<28} {'OLD':>12}  |  {'NEW':>12}")
    print("-" * 72)

    rows = [
        ("Total Trades",       f'{old_m["total_trades"]:>12}',  f'{new_m["total_trades"]:>12}'),
        ("Win Rate",           f'{old_m["win_rate"]:>11.1%} ', f'{new_m["win_rate"]:>11.1%} '),
        ("Wins / Losses",      f'{old_m["wins"]:>5}/{old_m["losses"]:<5}', f'{new_m["wins"]:>5}/{new_m["losses"]:<5}'),
        ("Total PnL (Rs)",     f'{old_m["total_pnl"]:>+11,.0f}', f'{new_m["total_pnl"]:>+11,.0f}'),
        ("Avg PnL/trade (Rs)", f'{old_m["avg_pnl"]:>+11,.0f}', f'{new_m["avg_pnl"]:>+11,.0f}'),
        ("Avg Win (Rs)",       f'{old_m["avg_win"]:>+11,.0f}', f'{new_m["avg_win"]:>+11,.0f}'),
        ("Avg Loss (Rs)",      f'{old_m["avg_loss"]:>+11,.0f}', f'{new_m["avg_loss"]:>+11,.0f}'),
        ("Profit Factor",      f'{old_m["profit_factor"]:>11.2f}', f'{new_m["profit_factor"]:>11.2f}'),
        ("Sharpe Ratio",       f'{old_m["sharpe"]:>11.2f}', f'{new_m["sharpe"]:>11.2f}'),
        ("Max Drawdown (Rs)",  f'{old_m["max_drawdown"]:>+11,.0f}', f'{new_m["max_drawdown"]:>+11,.0f}'),
        ("Max Consec Losses",  f'{old_m["max_consec_losses"]:>12}', f'{new_m["max_consec_losses"]:>12}'),
        ("Final Equity (Rs)",  f'{old_m["final_equity"]:>11,.0f}', f'{new_m["final_equity"]:>11,.0f}'),
        ("Days Killed",        f'{old_m["days_killed"]:>12}', f'{new_m["days_killed"]:>12}'),
        ("Days Cap Hit",       f'{old_m["days_cap_hit"]:>12}', f'{new_m["days_cap_hit"]:>12}'),
        ("Trading Days",       f'{old_m["trading_days"]:>12}', f'{new_m["trading_days"]:>12}'),
    ]

    for name, old_val, new_val in rows:
        print(f"  {name:<28} {old_val}  |  {new_val}")
    print("=" * 72)

    # Exit reason breakdown
    print()
    print("  EXIT REASON BREAKDOWN")
    print("-" * 72)
    all_reasons = sorted(set(list(old_m["by_exit_reason"].keys()) + list(new_m["by_exit_reason"].keys())))
    for r in all_reasons:
        o = old_m["by_exit_reason"].get(r, {"n": 0, "pnl": 0.0})
        n_ = new_m["by_exit_reason"].get(r, {"n": 0, "pnl": 0.0})
        print(f"  {r:<18} OLD: {o['n']:>3} trades, Rs{o['pnl']:>+8,.0f}  |  NEW: {n_['n']:>3} trades, Rs{n_['pnl']:>+8,.0f}")
    print("-" * 72)

    # SL mode breakdown
    print()
    print("  STOP MODE BREAKDOWN")
    print("-" * 72)
    for mode in ["STRICT", "MED", "WIDE"]:
        o = old_m["by_sl_mode"].get(mode, {"n": 0, "pnl": 0.0, "wins": 0})
        n_ = new_m["by_sl_mode"].get(mode, {"n": 0, "pnl": 0.0, "wins": 0})
        o_wr = o["wins"] / o["n"] * 100 if o["n"] else 0
        n_wr = n_["wins"] / n_["n"] * 100 if n_["n"] else 0
        print(f"  {mode:<10} OLD: {o['n']:>3} trades, {o_wr:.0f}% WR, Rs{o['pnl']:>+8,.0f}  |  NEW: {n_['n']:>3} trades, {n_wr:.0f}% WR, Rs{n_['pnl']:>+8,.0f}")
    print("-" * 72)

    # Per-day comparison (last 10 days)
    print()
    print("  LAST 10 TRADING DAYS -- DAILY PnL")
    print("-" * 72)
    old_by_day = {d.date: d for d in old_days}
    new_by_day = {d.date: d for d in new_days}
    all_dates = sorted(set(list(old_by_day.keys()) + list(new_by_day.keys())))[-10:]
    print(f"  {'Date':<14} {'OLD PnL':>10} {'Trades':>6} | {'NEW PnL':>10} {'Trades':>6} {'Diff':>10}")
    for d in all_dates:
        od = old_by_day.get(d)
        nd = new_by_day.get(d)
        o_pnl = f'Rs{od.pnl:>+7,.0f}' if od else "     ---"
        o_tr = str(od.trades) if od else "   -"
        n_pnl = f'Rs{nd.pnl:>+7,.0f}' if nd else "     ---"
        n_tr = str(nd.trades) if nd else "   -"
        diff = (nd.pnl if nd else 0) - (od.pnl if od else 0)
        d_str = f'Rs{diff:>+7,.0f}'
        print(f"  {d:<14} {o_pnl:>10} {o_tr:>6} | {n_pnl:>10} {n_tr:>6} {d_str:>10}")
    print("-" * 72)

    # Impact analysis
    print()
    print("  IMPACT ANALYSIS")
    print("-" * 72)
    trade_reduction = old_m["total_trades"] - new_m["total_trades"]
    pnl_change = new_m["total_pnl"] - old_m["total_pnl"]
    wr_change = new_m["win_rate"] - old_m["win_rate"]
    pf_change = new_m["profit_factor"] - old_m["profit_factor"]
    dd_change = new_m["max_drawdown"] - old_m["max_drawdown"]
    consec_change = new_m["max_consec_losses"] - old_m["max_consec_losses"]

    print(f"  Trades reduced:          {trade_reduction:>4} ({trade_reduction/max(old_m['total_trades'],1)*100:.0f}% fewer)")
    print(f"  PnL change:              Rs{pnl_change:>+10,.0f}")
    print(f"  Win rate change:         {wr_change:>+.1%}")
    print(f"  Profit factor change:    {pf_change:>+.2f}")
    print(f"  Max drawdown change:     Rs{dd_change:>+10,.0f}")
    print(f"  Max consec losses:       {consec_change:>+d} ({old_m['max_consec_losses']} -> {new_m['max_consec_losses']})")
    print(f"  Days with daily cap hit: {new_m['days_cap_hit']:>4}")
    print(f"  Days killed (loss limit):{new_m['days_killed']:>4}")
    print("-" * 72)

    # Save trade logs
    out_dir = r"D:\All Bots\trading_system\backtest\results"
    os.makedirs(out_dir, exist_ok=True)
    old_df = pd.DataFrame([vars(t) for t in old_trades])
    new_df = pd.DataFrame([vars(t) for t in new_trades])
    old_df.to_csv(os.path.join(out_dir, "scalp_old_trades.csv"), index=False)
    new_df.to_csv(os.path.join(out_dir, "scalp_new_trades.csv"), index=False)
    print(f"\n  Trade logs saved to: {out_dir}")
    print(f"    scalp_old_trades.csv  ({len(old_trades)} trades)")
    print(f"    scalp_new_trades.csv  ({len(new_trades)} trades)")

    print()
    print("=" * 72)
    print("  BACKTEST COMPLETE")
    print("=" * 72)


if __name__ == "__main__":
    main()
