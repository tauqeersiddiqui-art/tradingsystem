# run_backtest_diagnostic.py
# Diagnostic backtest: entry timing, loss patterns, strategy breakdown.
# Runs on Jun 2025 – Jun 2026 (203 days) with current strategies.

import os, sys, warnings, logging
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

import pandas as pd
import numpy as np
from collections import defaultdict
from backtest.backtest_engine import BacktestEngine

CSV = "data/historical/nifty_1m_full.csv"

# ── helpers ───────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'='*58}")
    print(f"  {title}")
    print('='*58)

def fmt(label, val, width=36):
    print(f"  {label:<{width}} {val}")

def run_config(label, entry_on, lunch_end_hr, lunch_end_min, df):
    from backtest.backtest_engine import BacktestEngine
    import backtest.backtest_engine as be
    # patch lunch end
    import datetime
    be._LUNCH_END = datetime.time(lunch_end_hr, lunch_end_min)

    cfg = {
        "INITIAL_CAPITAL":    100_000,
        "DAILY_LOSS_LIMIT":   -2_000,
        "MAX_TRADES_PER_DAY": 10,
        "MAX_HOLD_SECONDS":   300,
        "COOLDOWN_SECONDS":   180,
        "LOT_SIZE":           65,
        "LOTS_PER_TRADE":     1,
        "CHAMPION_THRESHOLD": 0.42,
        "ENTRY_ON":           entry_on,
    }
    eng = BacktestEngine(cfg)
    results = eng.run(df)
    results["_engine"] = eng
    results["_label"]  = label
    return results

def analyse(results, eng):
    trades = eng.trades
    if not trades:
        print("  No trades.")
        return

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]

    # ── Entry time buckets ──────────────────────────────────────────
    buckets = defaultdict(lambda: {"n":0,"pnl":0,"wins":0})
    for t in trades:
        h = t.entry_time.hour
        m = t.entry_time.minute
        mins = h*60 + m - (9*60+30)   # minutes after 9:30
        if mins < 0:
            b = "09:15-09:30 (ORB)"
        elif mins < 30:
            b = "09:30-10:00"
        elif mins < 90:
            b = "10:00-11:00"
        elif mins < 180:
            b = "11:00-12:30 (lunch)"
        elif mins < 270:
            b = "12:30-14:00"
        elif mins < 345:
            b = "14:00-15:15"
        else:
            b = "15:15+"
        buckets[b]["n"]    += 1
        buckets[b]["pnl"]  += t.pnl
        buckets[b]["wins"] += 1 if t.pnl > 0 else 0

    print("\n  Entry Time Breakdown:")
    print(f"  {'Slot':<25} {'#':>4}  {'PnL':>8}  {'WR':>6}  {'Avg':>7}")
    print(f"  {'-'*56}")
    for b in ["09:15-09:30 (ORB)","09:30-10:00","10:00-11:00",
              "11:00-12:30 (lunch)","12:30-14:00","14:00-15:15","15:15+"]:
        v = buckets[b]
        if v["n"] == 0:
            continue
        wr  = v["wins"]/v["n"]*100
        avg = v["pnl"]/v["n"]
        print(f"  {b:<25} {v['n']:>4}  {v['pnl']:>8.0f}  {wr:>5.0f}%  {avg:>7.0f}")

    # ── Strategy breakdown ──────────────────────────────────────────
    strat = defaultdict(lambda:{"n":0,"pnl":0,"wins":0})
    for t in trades:
        k = t.entry_reason
        strat[k]["n"]    += 1
        strat[k]["pnl"]  += t.pnl
        strat[k]["wins"] += 1 if t.pnl>0 else 0

    print("\n  Strategy Breakdown:")
    print(f"  {'Reason':<18} {'#':>4}  {'PnL':>8}  {'WR':>6}  {'Avg':>7}")
    print(f"  {'-'*46}")
    for k,v in sorted(strat.items(), key=lambda x:-abs(x[1]["pnl"])):
        wr  = v["wins"]/v["n"]*100
        avg = v["pnl"]/v["n"]
        print(f"  {k:<18} {v['n']:>4}  {v['pnl']:>8.0f}  {wr:>5.0f}%  {avg:>7.0f}")

    # ── Exit reason breakdown ───────────────────────────────────────
    ex = defaultdict(lambda:{"n":0,"pnl":0})
    for t in trades:
        ex[t.exit_reason]["n"]   += 1
        ex[t.exit_reason]["pnl"] += t.pnl
    print("\n  Exit Reason:")
    print(f"  {'Reason':<22} {'#':>4}  {'PnL':>8}")
    print(f"  {'-'*38}")
    for k,v in sorted(ex.items(), key=lambda x:-x[1]["n"]):
        print(f"  {k:<22} {v['n']:>4}  {v['pnl']:>8.0f}")

    # ── Hold time analysis ──────────────────────────────────────────
    held = [t.held_seconds for t in trades]
    w_h  = [t.held_seconds for t in trades if t.pnl>0]
    l_h  = [t.held_seconds for t in trades if t.pnl<=0]
    print(f"\n  Hold time -- avg: {np.mean(held):.0f}s  "
          f"wins: {np.mean(w_h) if w_h else 0:.0f}s  "
          f"losses: {np.mean(l_h) if l_h else 0:.0f}s")

    # ── CE vs PE ────────────────────────────────────────────────────
    for side in ("CE","PE"):
        st = [t for t in trades if t.side==side]
        if not st:
            continue
        sp = [t.pnl for t in st]
        sw = sum(1 for p in sp if p>0)
        print(f"  {side}: {len(st)} trades  PnL={sum(sp):,.0f}  "
              f"WR={sw/len(st)*100:.0f}%  avg={np.mean(sp):.0f}")


# ── MAIN ──────────────────────────────────────────────────────────────

print("\n" + "#"*58)
print("  BACKTEST DIAGNOSTIC  --  Jun 2025 to Jun 2026")
print("#"*58)

# Load and filter to Jun 2025 onwards
df_all = pd.read_csv(CSV)
df_all["date"] = pd.to_datetime(df_all["date"])
df_all["day"]  = df_all["date"].dt.date

# Filter to market hours only, Jun 2025 onward
df = df_all[
    (df_all["date"] >= "2025-06-01") &
    (df_all["date"].dt.hour >= 9) &
    (df_all["date"].dt.hour < 16)
].copy().reset_index(drop=True)
df["day"] = df["date"].dt.date

print(f"\n  Data: {len(df):,} rows | {df['day'].nunique()} trading days")
print(f"  Range: {df['date'].min().date()} to {df['date'].max().date()}")

# ── RUN 1: Current setup (current_close entry, lunch 11-12:30) ────
section("RUN 1 -- Current Strategy (entry=close, lunch=11-12:30)")
r1 = run_config("current_close + lunch12:30", "current_close", 12, 30, df)
eng1 = r1.pop("_engine"); lbl1 = r1.pop("_label")

fmt("Total trades",   r1["total_trades"])
fmt("Win rate",       f"{r1['win_rate']*100:.1f}%")
fmt("Total PnL",      f"Rs{r1['total_pnl']:,.0f}")
fmt("Avg PnL/trade",  f"Rs{r1['avg_pnl']:,.0f}")
fmt("Avg win",        f"Rs{r1['avg_win']:,.0f}")
fmt("Avg loss",       f"Rs{r1['avg_loss']:,.0f}")
fmt("Profit factor",  f"{r1['profit_factor']:.2f}")
fmt("Max drawdown",   f"Rs{r1['max_drawdown']:,.0f}")
fmt("Sharpe",         f"{r1['sharpe']:.2f}")
fmt("Days killed",    f"{r1['days_killed']} / {r1['trading_days']}")
analyse(r1, eng1)

# ── RUN 2: Late entry simulation (next_open, same filters) ────────
section("RUN 2 -- Late Entry Simulation (entry=next_open, lunch=11-12:30)")
r2 = run_config("next_open + lunch12:30", "next_open", 12, 30, df)
eng2 = r2.pop("_engine"); lbl2 = r2.pop("_label")

fmt("Total trades",   r2["total_trades"])
fmt("Win rate",       f"{r2['win_rate']*100:.1f}%")
fmt("Total PnL",      f"Rs{r2['total_pnl']:,.0f}")
fmt("Avg PnL/trade",  f"Rs{r2['avg_pnl']:,.0f}")
fmt("Avg win",        f"Rs{r2['avg_win']:,.0f}")
fmt("Avg loss",       f"Rs{r2['avg_loss']:,.0f}")
fmt("Profit factor",  f"{r2['profit_factor']:.2f}")

# ── RUN 3: Old lunch filter (14:00) for comparison ────────────────
section("RUN 3 -- Old Lunch Filter (entry=close, lunch=11-14:00)")
r3 = run_config("current_close + lunch14:00", "current_close", 14, 0, df)
eng3 = r3.pop("_engine"); lbl3 = r3.pop("_label")

fmt("Total trades",   r3["total_trades"])
fmt("Win rate",       f"{r3['win_rate']*100:.1f}%")
fmt("Total PnL",      f"Rs{r3['total_pnl']:,.0f}")
fmt("Avg PnL/trade",  f"Rs{r3['avg_pnl']:,.0f}")
fmt("Profit factor",  f"{r3['profit_factor']:.2f}")

# ── RUN 4: Aggressive -- tighter ML threshold ──────────────────────
section("RUN 4 -- Tighter Filter (entry=close, ML floor=0.65, lunch=11-12:30)")
import backtest.backtest_engine as _be
import datetime as _dt
_be._LUNCH_END   = _dt.time(12, 30)
_be._MIN_ML_FLOOR = 0.65

cfg4 = {
    "INITIAL_CAPITAL":    100_000,
    "DAILY_LOSS_LIMIT":   -2_000,
    "MAX_TRADES_PER_DAY": 10,
    "MAX_HOLD_SECONDS":   300,
    "COOLDOWN_SECONDS":   180,
    "LOT_SIZE":           65,
    "LOTS_PER_TRADE":     1,
    "CHAMPION_THRESHOLD": 0.42,
    "ENTRY_ON":           "current_close",
}
eng4 = BacktestEngine(cfg4)
r4   = eng4.run(df)

fmt("Total trades",   r4["total_trades"])
fmt("Win rate",       f"{r4['win_rate']*100:.1f}%")
fmt("Total PnL",      f"Rs{r4['total_pnl']:,.0f}")
fmt("Avg PnL/trade",  f"Rs{r4['avg_pnl']:,.0f}")
fmt("Profit factor",  f"{r4['profit_factor']:.2f}")
analyse(r4, eng4)

# Reset floor
_be._MIN_ML_FLOOR = 0.62

# ── SUMMARY COMPARISON ────────────────────────────────────────────
section("SUMMARY COMPARISON")
print(f"\n  {'Config':<35} {'Trades':>6}  {'WR%':>5}  {'PnL':>9}  {'PF':>5}  {'Sharpe':>6}")
print(f"  {'-'*68}")
for label, r in [
    ("1. Close entry + lunch12:30",  r1),
    ("2. NextOpen entry (late)",      r2),
    ("3. Close entry + lunch14:00",   r3),
    ("4. ML>=0.65 + lunch12:30",      r4),
]:
    print(
        f"  {label:<35} "
        f"{r['total_trades']:>6}  "
        f"{r['win_rate']*100:>5.1f}  "
        f"Rs{r['total_pnl']:>8,.0f}  "
        f"{r['profit_factor']:>5.2f}  "
        f"{r['sharpe']:>6.2f}"
    )

# ── SAVE BEST RESULT ──────────────────────────────────────────────
print("\n  Saving Run 1 trade log to backtest/results/...")
eng1.save_results("backtest/results")
print("  Done -- check backtest/results/trade_log.csv")
print()
