#!/usr/bin/env python3
"""Backtest old vs new scalp entry timing on Aug 19 data."""
import sys, collections
sys.path.insert(0, r"D:\All Bots\trading_system")
from datetime import datetime, timedelta, time as dtime
from engine.config.config import Config
from engine.scalping.scalp_engine import ScalpEngine
import pandas as pd
import numpy as np

df = pd.read_csv(r"D:\All Bots\trading_system\data\historical\nifty_1m_full.csv")
df["date"] = pd.to_datetime(df["date"], format="mixed")
aug19 = df[df["date"].dt.date == pd.Timestamp("2026-08-19").date()].copy()

# Build tick-level data from 1m candles
ticks = []
for _, row in aug19.iterrows():
    ts = row["date"]
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    ticks.append((ts, o))
    for i in range(1, 12):
        frac = i / 12
        base = o + (c - o) * frac
        excursion = (h - l) * 0.3 * np.sin(np.pi * frac)
        tp = base + excursion * (1 if i % 2 == 0 else -1)
        tp = max(l, min(h, tp))
        ticks.append((ts + timedelta(seconds=i * 5), tp))
    ticks.append((ts + timedelta(seconds=55), c))

print(f"Aug 19: {len(aug19)} candles -> {len(ticks)} ticks")


# OLD logic: original check_entry without timing fixes
class OldScalpEngine(ScalpEngine):
    def check_entry(self, ltp_now, ltp_history, ts, htf5=0, safe_mode=False):
        if ltp_now <= 0:
            return None
        now_time = ts.time()
        if not (dtime(9, 30) <= now_time < dtime(15, 10)):
            return None
        if len(ltp_history) < 2:
            return None
        cutoff = ts - timedelta(seconds=self._mom_window)
        past = [(t, p) for t, p in ltp_history if t >= cutoff]
        if not past:
            return None
        earliest_ltp = past[0][1]
        move = ltp_now - earliest_ltp
        _bar = self._mom_thresh
        if move < _bar and move > -_bar:
            return None
        side = "CE" if move >= _bar else "PE"
        prices = [p for _, p in past]
        if len(prices) < self._min_samples:
            return None
        half = len(prices) // 2
        first, second = prices[:half], prices[half:]
        h1, l1 = max(first), min(first)
        h2, l2 = max(second), min(second)
        rng = max(h2 - l2, 1e-9)
        if side == "CE" and h2 <= h1:
            return None
        if side == "PE" and l2 >= l1:
            return None
        pullback = (h2 - ltp_now) if side == "CE" else (ltp_now - l2)
        if not (0.10 * rng <= pullback <= 0.50 * rng):
            return None
        if side == "CE" and ltp_now < l2:
            return None
        if side == "PE" and ltp_now > h2:
            return None
        if len(prices) >= 4:
            q = max(1, len(prices) // 4)
            tail_move = abs(ltp_now - prices[-q])
            total_move = abs(ltp_now - prices[0])
            if total_move > 1e-9 and tail_move > self._tail_frac * total_move:
                return None
        return {"side": side, "reason": "SCALP_MOM", "move_pts": round(move, 2)}


config = Config()
old_engine = OldScalpEngine(config)
new_engine = ScalpEngine(config)


def backtest(engine, ticks, sl_pts=8.0, target_pts=50.0, cooldown_s=240, max_hold_s=180):
    trades = []
    position = None
    last_exit_time = ticks[0][0] - timedelta(days=1)

    for ts, ltp in ticks:
        if position is not None:
            entry = position["entry"]
            held = (ts - position["entry_ts"]).total_seconds()
            if position["side"] == "CE":
                hit_sl = ltp <= position["sl"]
                hit_tp = ltp >= position["target"]
                pnl = (ltp - entry) * position["qty"]
            else:
                hit_sl = ltp >= position["sl"]
                hit_tp = ltp <= position["target"]
                pnl = (entry - ltp) * position["qty"]

            reason = None
            if hit_sl:
                reason = "STOP"
            elif hit_tp:
                reason = "TARGET"
            elif held > max_hold_s:
                reason = "TIME"

            if reason:
                trades.append({**position, "exit_price": ltp, "exit_time": ts, "pnl": pnl, "reason": reason, "held_s": held})
                position = None
                last_exit_time = ts
        else:
            if (ts - last_exit_time).total_seconds() < cooldown_s:
                continue
            history = collections.deque(
                [(t, p) for t, p in ticks if t <= ts][-60:],
                maxlen=200,
            )
            result = engine.check_entry(ltp, history, ts, htf5=0, safe_mode=False)
            if result:
                side = result["side"]
                if side == "CE":
                    sl = ltp - sl_pts
                    target = ltp + target_pts
                else:
                    sl = ltp + sl_pts
                    target = ltp - target_pts
                position = {
                    "side": side,
                    "entry": ltp,
                    "entry_ts": ts,
                    "sl": sl,
                    "target": target,
                    "qty": 60,
                    "move_pts": result["move_pts"],
                }

    if position:
        if position["side"] == "CE":
            pnl = (ticks[-1][1] - position["entry"]) * position["qty"]
        else:
            pnl = (position["entry"] - ticks[-1][1]) * position["qty"]
        trades.append({**position, "exit_price": ticks[-1][1], "exit_time": ticks[-1][0], "pnl": pnl, "reason": "EOD", "held_s": 0})

    return trades


def stats(trades, label):
    if not trades:
        print(f"\n{label}: No trades")
        return {}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    wr = len(wins) / len(trades) * 100

    reasons = {}
    for t in trades:
        r = t["reason"]
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl": 0, "wins": 0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            reasons[r]["wins"] += 1

    avg_hold = np.mean([t["held_s"] for t in trades])

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Trades:    {len(trades)}")
    print(f"  Wins:      {len(wins)} ({wr:.1f}%)")
    print(f"  Losses:    {len(losses)} ({100 - wr:.1f}%)")
    print(f"  Total PnL: {total_pnl:+,.0f} pts")
    print(f"  Avg win:   {avg_win:+.1f} pts")
    print(f"  Avg loss:  {avg_loss:+.1f} pts")
    print(f"  Avg hold:  {avg_hold:.0f}s")
    print(f"\n  Exit reasons:")
    for r, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
        wr_r = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
        print(f"    {r:>8}: {d['count']:>4} trades | PnL={d['pnl']:+8.0f} | WR={wr_r:.0f}%")

    print(f"\n  Sample trades (first 5):")
    for t in trades[:5]:
        ts_str = t["entry_ts"].strftime("%H:%M:%S")
        print(f"    {ts_str} {t['side']} move={t['move_pts']:+.1f} entry={t['entry']:.1f} exit={t['exit_price']:.1f} pnl={t['pnl']:+.0f} ({t['reason']})")

    return {
        "trades": len(trades),
        "wr": wr,
        "pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


print("\nRunning backtests on Aug 19 data...")
old_trades = backtest(old_engine, ticks)
new_trades = backtest(new_engine, ticks)

old_stats = stats(old_trades, "OLD (pre-fix) - original entry timing")
new_stats = stats(new_trades, "NEW (with timing fix) - spike age + pullback + candle alignment")

if old_stats and new_stats:
    print(f"\n{'=' * 60}")
    print(f"  COMPARISON: OLD vs NEW")
    print(f"{'=' * 60}")
    print(f"  Trades:    {old_stats['trades']} -> {new_stats['trades']} ({new_stats['trades'] - old_stats['trades']:+d})")
    print(f"  Win rate:  {old_stats['wr']:.1f}% -> {new_stats['wr']:.1f}% ({new_stats['wr'] - old_stats['wr']:+.1f}%)")
    print(f"  Total PnL: {old_stats['pnl']:+,.0f} -> {new_stats['pnl']:+,.0f} ({new_stats['pnl'] - old_stats['pnl']:+,.0f})")
    print(f"  Avg win:   {old_stats['avg_win']:+.1f} -> {new_stats['avg_win']:+.1f}")
    print(f"  Avg loss:  {old_stats['avg_loss']:+.1f} -> {new_stats['avg_loss']:+.1f}")
    print()
    if new_stats["trades"] > 0:
        new_profit_factor = abs(sum(t["pnl"] for t in new_trades if t["pnl"] > 0) / sum(t["pnl"] for t in new_trades if t["pnl"] < 0)) if sum(t["pnl"] for t in new_trades if t["pnl"] < 0) != 0 else float("inf")
        print(f"  NEW profit factor: {new_profit_factor:.2f}")
    if old_stats["trades"] > 0:
        old_profit_factor = abs(sum(t["pnl"] for t in old_trades if t["pnl"] > 0) / sum(t["pnl"] for t in old_trades if t["pnl"] < 0)) if sum(t["pnl"] for t in old_trades if t["pnl"] < 0) != 0 else float("inf")
        print(f"  OLD profit factor: {old_profit_factor:.2f}")
