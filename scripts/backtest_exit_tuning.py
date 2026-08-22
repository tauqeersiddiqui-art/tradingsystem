# scripts/backtest_exit_tuning.py - Phase-10 exit-tuning backtest (Task #16)
#
# Reuses VERBATIM the entry-generation + rejection logic of
# scripts/backtest_entry_quality.py (imported by file path below) to rebuild
# the identical accepted-entry population (58 trades, Aug 13–21 sealed-bar
# replay of data/historical/nifty_1m_full.csv through the real
# engine.execution.filters rejection stack), then sweeps a grid of
# premium-space EXIT variants on that FIXED entry population:
#
#   NO_LIFE   : time 60/90/120/180 s  x  profit floor 1.0/2.0/3.0 premium pts
#               (plus an OFF variant so the trailing stop can run alone)
#   SL        : 3/5/8 premium pts
#   TARGET    : +30/+50/+80 premium pts
#   TRAILING  : always ON - after +10 pts stop moves to breakeven
#               (entry + round-trip cost in premium space); after +20 pts
#               trail 8 pts below the high-water mark.
#
# MAX_HOLD fixed at 300 s for every variant. Cost Rs66/trade, delta 0.5,
# qty 30 - same as the entry-quality baseline.
#
# No new dependencies: pandas + stdlib only. Production files untouched.

import argparse
import importlib.util
import os
import sys
from datetime import time as dtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

# -- Import the entry-quality script by file path (its module name would
#    otherwise collide with the top-level `backtest/` package). ----------
_BEQ_PATH = os.path.join(ROOT, "scripts", "backtest_entry_quality.py")
_spec = importlib.util.spec_from_file_location("beq_entry_quality", _BEQ_PATH)
beq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(beq)

from engine.execution.filters import (  # noqa: E402
    get_rejection_stats, reset_rejection_stats,
)

# -- Constants carried over from the entry-quality baseline --------------
DELTA, QTY = beq.DELTA, beq.QTY
COST_RS = beq.COST_RS                 # round_trip_cost(30) == Rs66
MAX_HOLD_S = beq.MAX_HOLD_S           # 300 s - held constant across variants
COST_PTS = COST_RS / QTY              # round-trip cost in premium pts (2.2)

# -- Exit grid -----------------------------------------------------------
SL_GRID = [3.0, 5.0, 8.0]
TARGET_GRID = [30.0, 50.0, 80.0]
NL_TIME_GRID = [60, 90, 120, 180]     # seconds
NL_PTS_GRID = [1.0, 2.0, 3.0]         # premium pts profit floor
TRAIL_BE_PTS = 10.0                   # after +10 pts -> stop at breakeven
TRAIL_T2_PTS = 20.0                   # after +20 pts -> trailing mode
TRAIL_GAP_PTS = 8.0                   # stop = HWM - 8

BASELINE = dict(sl=5.0, target=50.0, nl_s=120, nl_pts=2.0, tag="BASELINE")


# -- Entry population (identical to backtest_entry_quality FILTERED set) -
def build_entry_population(df: pd.DataFrame, days: list) -> list:
    """Replay candidates through the real rejection stack; return accepted
    entries as dicts {day, i, side, ts}. Same loop as beq.run_day filtered
    branch - entry bars are accepted BEFORE any exit simulation, so the
    population is exit-config-independent."""
    entries = []
    for d in days:
        day = df[df["d"] == d].reset_index(drop=True)
        for cand in beq.gen_candidates(day):
            b_i, side = cand["breakout_i"], cand["side"]
            breakout_ts = day["ts"].iloc[b_i] if cand["kind"] == "ORB" else None
            attempts = beq.ORB_ATTEMPT_BARS if cand["kind"] == "ORB" else 0
            for k in range(attempts + 1):
                j = b_i + k
                if j >= len(day):
                    break
                if beq.eq_or_none(day, j, side, breakout_ts):
                    entries.append({"day": day, "i": j, "side": side,
                                    "ts": day["ts"].iloc[j]})
                    break
    return entries


# -- Parametrized exit simulator (same bar-walk skeleton as beq) ---------
def simulate_exit_variant(day: pd.DataFrame, entry_i: int, side: str,
                          sl: float, target: float, nl_s, nl_pts: float,
                          trail: bool = True) -> tuple:
    """Walk sealed bars after entry under an exit variant.
    nl_s=None disables the NO_LIFE check. Returns (exit_premium_pts, reason).
    Priority per bar: SL/stop -> TARGET -> trailing stop -> NO_LIFE -> MAX_HOLD,
    matching the original check order (SL first, TARGET second)."""
    entry_px = float(day["close"].iloc[entry_i])
    entry_ts = day["ts"].iloc[entry_i]
    sign = 1.0 if side == "CE" else -1.0
    be_stop = -(COST_PTS)             # breakeven stop: exit px = entry + cost
    hwm, be_armed, trail_armed = 0.0, False, False
    last_prem = 0.0
    for j in range(entry_i + 1, len(day)):
        px = float(day["close"].iloc[j])
        prem = DELTA * sign * (px - entry_px)
        held = (day["ts"].iloc[j] - entry_ts).total_seconds()
        last_prem = prem
        if prem > hwm:
            hwm = prem
        # arm trailing tiers on first touch (peak-based)
        if trail and not be_armed and hwm >= TRAIL_BE_PTS:
            be_armed = True
        if trail and not trail_armed and hwm >= TRAIL_T2_PTS:
            trail_armed = True
        # -- hard stop: fixed SL, then (once armed) trail/breakeven stop --
        if prem <= -sl:
            return -sl, "SL"
        if trail_armed and prem <= hwm - TRAIL_GAP_PTS:
            return prem, "TRAIL"
        if be_armed and prem <= be_stop:
            return prem, "BREAKEVEN"
        # -- profit target ------------------------------------------------
        if prem >= target:
            return target, "TARGET"
        # -- no-life cut -------------------------------------------------
        if nl_s is not None and held >= nl_s and prem < nl_pts:
            return prem, "NO_LIFE"
        # -- max hold ----------------------------------------------------
        if held >= MAX_HOLD_S:
            return prem, "MAX_HOLD"
    return last_prem, "EOD"


def trade_pnl(prem_pts: float) -> float:
    return prem_pts * QTY - COST_RS


def run_variant(entries: list, sl, target, nl_s, nl_pts, trail=True) -> dict:
    rows = []
    for e in entries:
        prem, reason = simulate_exit_variant(
            e["day"], e["i"], e["side"], sl, target, nl_pts=nl_pts,
            nl_s=nl_s, trail=trail)
        rows.append({"pnl": trade_pnl(prem), "exit": reason})
    n = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    mix = {}
    for r in rows:
        mix[r["exit"]] = mix.get(r["exit"], 0) + 1
    return {
        "trades": n,
        "win_rate": 100.0 * wins / n if n else 0.0,
        "net_pnl": sum(r["pnl"] for r in rows),
        "mix": dict(sorted(mix.items())),
    }


def variant_tag(sl, target, nl_s, nl_pts) -> str:
    nl = "NL=off" if nl_s is None else f"NL<{nl_pts:g}@{nl_s}s"
    return f"SL={sl:g} TGT=+{target:g} {nl}"


def print_result(tag: str, res: dict):
    mix = ", ".join(f"{k}:{v}" for k, v in res["mix"].items())
    print(f"{tag:<38} trades={res['trades']:<3} win={res['win_rate']:5.1f}% "
          f"net=Rs{res['net_pnl']:+8.0f}  exits[{mix}]")


def main():
    ap = argparse.ArgumentParser(description="Phase-10 exit-tuning grid")
    ap.add_argument("--csv", default="data/historical/nifty_1m_full.csv")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (optional)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (optional)")
    ap.add_argument("--days", type=int, default=7,
                    help="last N complete trading days when no range given")
    args = ap.parse_args()

    df = beq.load_data(args.csv)
    if args.start or args.end:
        if args.start:
            df = df[df["ts"] >= args.start]
        if args.end:
            df = df[df["ts"] <= args.end + " 23:59"]
        days = sorted(df["d"].unique())
    else:
        days = beq.complete_days(df, args.days)
    if not days:
        print("No trading days in range - nothing to replay.")
        return

    print("=" * 100)
    print("PHASE-10 EXIT-TUNING BACKTEST - fixed entry population, exit grid sweep")
    print(f"Days {len(days)}: {days[0]} .. {days[-1]} | delta={DELTA} qty={QTY} "
          f"cost=Rs{COST_RS:.0f}/trade (={COST_PTS:.1f} prem pts) "
          f"MAX_HOLD={MAX_HOLD_S}s (constant)")
    print(f"Trailing: +{TRAIL_BE_PTS:g}pts -> breakeven(entry+cost); "
          f"+{TRAIL_T2_PTS:g}pts -> trail {TRAIL_GAP_PTS:g} pts below HWM")
    print("=" * 100)

    # -- Build the (exit-independent) accepted-entry population ----------
    reset_rejection_stats()
    entries = build_entry_population(df, days)
    stats = get_rejection_stats()
    reset_rejection_stats()
    print(f"\nEntry population: {len(entries)} accepted entries "
          f"({stats['total_rejections']} rejections across {stats['evals']} EQ evals)")

    # -- Baseline first (current live config - no trailing) --------------
    b = BASELINE
    base_res = run_variant(entries, b["sl"], b["target"], b["nl_s"],
                           b["nl_pts"], trail=False)
    # cross-check against the ORIGINAL simulator to prove population parity
    orig_mix = {}
    orig_net = 0.0
    for e in entries:
        prem, reason = beq.simulate_exit(e["day"], e["i"], e["side"])
        orig_mix[reason] = orig_mix.get(reason, 0) + 1
        orig_net += trade_pnl(prem)
    parity = (orig_mix == base_res["mix"]
              and abs(orig_net - base_res["net_pnl"]) < 1e-6)

    print("\n-- BASELINE (current config: SL=-5, TGT=+50, NO_LIFE<2.0@120s, no trail) --")
    print_result("BASELINE", base_res)
    parity_msg = ("OK" if parity
                  else f"MISMATCH {orig_mix} net=Rs{orig_net:+.0f}")
    print(f"  parity vs original simulator: {parity_msg}")
    if not parity:
        print("  !! baseline mismatch - aborting grid (entry/exit parity broken)")
        return

    # -- Grid sweep -------------------------------------------------------
    results = []
    for sl in SL_GRID:
        for tgt in TARGET_GRID:
            nl_combos = [(t, p) for t in NL_TIME_GRID for p in NL_PTS_GRID]
            nl_combos += [(None, None)]          # NO_LIFE disabled variant
            for nl_s, nl_pts in nl_combos:
                res = run_variant(entries, sl, tgt, nl_s, nl_pts,
                                  trail=True)
                res["tag"] = variant_tag(sl, tgt, nl_s, nl_pts)
                res["cfg"] = (sl, tgt, nl_s, nl_pts)
                results.append(res)

    print(f"\n-- GRID ({len(results)} variants) --")
    for res in results:
        print_result(res["tag"], res)

    # -- Leaderboard: sort by net PnL desc, tie-break win rate desc ------
    board = sorted(results, key=lambda r: (-r["net_pnl"], -r["win_rate"]))
    print("\n" + "=" * 100)
    print("LEADERBOARD (sorted by net PnL; ties broken by win rate)")
    print("=" * 100)
    print(f"{'#':<4}{'variant':<38}{'trades':<8}{'win%':<7}{'net PnL':<12}exit mix")
    for rank, res in enumerate(board[:20], 1):
        mix = ", ".join(f"{k}:{v}" for k, v in res["mix"].items())
        print(f"{rank:<4}{res['tag']:<38}{res['trades']:<8}"
              f"{res['win_rate']:<7.1f}Rs{res['net_pnl']:+8.0f}  [{mix}]")
    base_rank = next(i for i, r in enumerate(board, 1)
                     if r["cfg"] == (b["sl"], b["target"], b["nl_s"], b["nl_pts"]))
    print(f"... baseline (SL=5 TGT=+50 NL<2@120s) ranks #{base_rank} of {len(board)}")

    # -- Recommendation ---------------------------------------------------
    best = board[0]
    print("\n" + "=" * 100)
    print("RECOMMENDED CONFIG (best net PnL; tie-break win rate)")
    print("=" * 100)
    print_result("RECOMMENDED", best)
    sl, tgt, nl_s, nl_pts = best["cfg"]
    print(f"  SL_PTS        = {sl:g}")
    print(f"  TARGET_PTS    = {tgt:g}")
    if nl_s is None:
        print("  NO_LIFE       = disabled")
    else:
        print(f"  NO_LIFE_PTS   = {nl_pts:g}   NO_LIFE_S = {nl_s}")
    print(f"  TRAILING      = ON (BE@+{TRAIL_BE_PTS:g}, trail {TRAIL_GAP_PTS:g} "
          f"below HWM after +{TRAIL_T2_PTS:g})   MAX_HOLD = {MAX_HOLD_S}s")
    d = best["net_pnl"] - base_res["net_pnl"]
    print(f"  vs baseline   : Rs{d:+.0f} net PnL, "
          f"{best['win_rate'] - base_res['win_rate']:+.1f} pp win rate")


if __name__ == "__main__":
    main()
