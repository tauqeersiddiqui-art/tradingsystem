# scripts/backtest_entry_quality.py — Phase-8 reproducibility replay (Task #14)
#
# Replays ORB-breakout + momentum candidates on SEALED 1m bars from the
# historical NIFTY CSV through the REAL engine.execution.filters
# .compute_entry_quality, then simulates premium-space exits, printing
# baseline (no filter) vs filtered results plus the rejection breakdown.
#
# Exit model (premium points): SL -5, TARGET +50, NO_LIFE (<2 pts after
# 120 s), MAX_HOLD 300 s, delta 0.5, qty 30, cost via
# engine.execution.cost_model.round_trip_cost.
#
# No new dependencies: pandas + argparse + stdlib only.

import os
import sys
import argparse
from datetime import time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from engine.execution.filters import (  # noqa: E402
    compute_entry_quality, get_rejection_stats, reset_rejection_stats,
)
from engine.execution.cost_model import round_trip_cost  # noqa: E402

# ── Exit / sizing model ─────────────────────────────────────────────────
SL_PTS, TARGET_PTS = 5.0, 50.0        # premium points
NO_LIFE_PTS, NO_LIFE_S = 2.0, 120     # <2 pts premium after 120 s → cut
MAX_HOLD_S = 300
DELTA, QTY = 0.5, 30                  # ATM delta proxy, one BANKNIFTY lot
COST_RS = round_trip_cost(QTY)        # authoritative round-trip cost

# ── Candidate generation params ─────────────────────────────────────────
MOM_BARS, MOM_PTS = 2, 20.0           # 2-bar spot move >= 20 pts
ORB_ATTEMPT_BARS = 8                  # live-loop style retries after breakout
MOM_MIN_GAP = 5                       # bars between momentum candidates

_MKT_OPEN, _ORB_END, _MKT_CLOSE = dtime(9, 15), dtime(9, 30), dtime(15, 30)


def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["date"], format="mixed")
    df = df.sort_values("ts").reset_index(drop=True)
    t = df["ts"].dt.time
    df = df[(t >= _MKT_OPEN) & (t <= _MKT_CLOSE)].copy()
    df["d"] = df["ts"].dt.date
    return df


def complete_days(df: pd.DataFrame, n: int) -> list:
    """Trading days whose data reaches >= 15:15 (i.e. not truncated)."""
    full = [d for d, g in df.groupby("d")
            if g["ts"].dt.time.max() >= dtime(15, 15)]
    return sorted(full)[-n:]


def simulate_exit(day: pd.DataFrame, entry_i: int, side: str) -> tuple:
    """Walk sealed bars after entry; return (exit_premium_pts, reason)."""
    entry_px = float(day["close"].iloc[entry_i])
    entry_ts = day["ts"].iloc[entry_i]
    sign = 1.0 if side == "CE" else -1.0
    last_prem = 0.0
    for j in range(entry_i + 1, len(day)):
        px = float(day["close"].iloc[j])
        prem = DELTA * sign * (px - entry_px)
        held = (day["ts"].iloc[j] - entry_ts).total_seconds()
        last_prem = prem
        if prem <= -SL_PTS:
            return -SL_PTS, "SL"
        if prem >= TARGET_PTS:
            return TARGET_PTS, "TARGET"
        if held >= NO_LIFE_S and prem < NO_LIFE_PTS:
            return prem, "NO_LIFE"
        if held >= MAX_HOLD_S:
            return prem, "MAX_HOLD"
    return last_prem, "EOD"


def trade_pnl(prem_pts: float) -> float:
    return prem_pts * QTY - COST_RS


def gen_candidates(day: pd.DataFrame) -> list:
    """Sealed-bar candidates: ORB breakout episodes + 2-bar momentum bursts.
    Each: dict(kind, side, breakout_i, entry_i, attempt_from)."""
    orb_bars = day[day["ts"].dt.time < _ORB_END]
    if len(orb_bars) < 5:
        return []
    orb_high, orb_low = float(orb_bars["high"].max()), float(orb_bars["low"].min())

    cands, closes = [], day["close"].values
    orb_fired = {"CE": False, "PE": False}
    last_mom_i = -MOM_MIN_GAP
    for i in range(len(orb_bars), len(day)):
        c = float(closes[i])
        # ORB breakout — first sealed close beyond the range (one per side/day)
        if not orb_fired["CE"] and c > orb_high:
            orb_fired["CE"] = True
            cands.append({"kind": "ORB", "side": "CE", "breakout_i": i})
        if not orb_fired["PE"] and c < orb_low:
            orb_fired["PE"] = True
            cands.append({"kind": "ORB", "side": "PE", "breakout_i": i})
        # Momentum — 2-bar move >= MOM_PTS (throttled to avoid burst spam)
        if i >= MOM_BARS and i - last_mom_i >= MOM_MIN_GAP:
            mv = c - float(closes[i - MOM_BARS])
            if mv >= MOM_PTS:
                cands.append({"kind": "MOM", "side": "CE", "breakout_i": i})
                last_mom_i = i
            elif mv <= -MOM_PTS:
                cands.append({"kind": "MOM", "side": "PE", "breakout_i": i})
                last_mom_i = i
    return cands


def eq_or_none(day, i, side, breakout_ts) -> bool:
    """Run the REAL filter on sealed bars 0..i; True if entry accepted."""
    eq = compute_entry_quality(
        day.iloc[:i + 1], side, float(day["close"].iloc[i]), day["ts"].iloc[i],
        {"breakout_ts": breakout_ts, "orb_done": breakout_ts is not None},
        cost_rs=COST_RS,
    )
    return eq["accepted"]


def summarize(tag: str, rows: list) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    net = sum(r["pnl"] for r in rows)
    wr = 100.0 * wins / n if n else 0.0
    print(f"{tag:<10} trades={n:<4} win_rate={wr:5.1f}%  net_pnl=Rs{net:+9.0f}")
    return {"trades": n, "win_rate": wr, "net_pnl": net}


def run_day(day: pd.DataFrame, baseline_rows: list, filtered_rows: list):
    for cand in gen_candidates(day):
        b_i, side = cand["breakout_i"], cand["side"]
        breakout_ts = day["ts"].iloc[b_i] if cand["kind"] == "ORB" else None
        # ── Baseline: enter at the candidate bar, no quality filter ──
        prem, reason = simulate_exit(day, b_i, side)
        baseline_rows.append({"pnl": trade_pnl(prem), "exit": reason})
        # ── Filtered: rejection-first. ORB episodes retry on each sealed
        #    bar (like the live loop) until accepted or attempt window ends;
        #    momentum candidates get a single evaluation. ──
        attempts = ORB_ATTEMPT_BARS if cand["kind"] == "ORB" else 0
        for k in range(attempts + 1):
            j = b_i + k
            if j >= len(day):
                break
            if eq_or_none(day, j, side, breakout_ts):
                prem, reason = simulate_exit(day, j, side)
                filtered_rows.append({"pnl": trade_pnl(prem), "exit": reason})
                break


def main():
    ap = argparse.ArgumentParser(description="Phase-8 entry-quality replay")
    ap.add_argument("--csv", default="data/historical/nifty_1m_full.csv")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (optional)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (optional)")
    ap.add_argument("--days", type=int, default=7,
                    help="last N complete trading days when no range given")
    args = ap.parse_args()

    df = load_data(args.csv)
    if args.start or args.end:
        if args.start:
            df = df[df["ts"] >= args.start]
        if args.end:
            df = df[df["ts"] <= args.end + " 23:59"]
        days = sorted(df["d"].unique())
    else:
        days = complete_days(df, args.days)
    if not days:
        print("No trading days in range — nothing to replay.")
        return

    print(f"Replaying {len(days)} day(s): {days[0]} .. {days[-1]}")
    print(f"Cost/trade=Rs{COST_RS:.0f} | SL=-{SL_PTS} TARGET=+{TARGET_PTS} "
          f"NO_LIFE<{NO_LIFE_PTS}@{NO_LIFE_S}s MAX_HOLD={MAX_HOLD_S}s "
          f"delta={DELTA} qty={QTY}\n")

    reset_rejection_stats()
    baseline_rows, filtered_rows = [], []
    for d in days:
        run_day(df[df["d"] == d].reset_index(drop=True), baseline_rows, filtered_rows)

    summarize("BASELINE", baseline_rows)
    f = summarize("FILTERED", filtered_rows)

    exits = {}
    for r in filtered_rows:
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
    print(f"\nFiltered exit mix: {dict(sorted(exits.items()))}")

    stats = get_rejection_stats()
    print(f"\nEQ evals={stats['evals']}  total_rejections={stats['total_rejections']}")
    for reason, cnt in sorted(stats["rejections"].items(), key=lambda x: -x[1]):
        print(f"  {reason:<18} {cnt}")
    reset_rejection_stats()


if __name__ == "__main__":
    main()
