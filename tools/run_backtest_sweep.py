#!/usr/bin/env python3
"""
tools/run_backtest_sweep.py

Run backtest across multiple slippage scenarios (1x, 2x, 3x cost).
Produces artifacts/backtest_summary_<scenario>.json and logs/trade_signals_sample_<scenario>.csv

Usage:
  python tools/run_backtest_sweep.py --cost-multipliers 1,2,3 --tail 50000
"""

import argparse
import os
import sys
import json
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def run_backtest(cost_multiplier, tail=None, out_dir="artifacts"):
    """Run a single backtest with given cost multiplier."""
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    csv_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "historical", "banknifty_1m_full.csv"
    )

    cmd = [
        sys.executable, "-m", "backtest.backtest_engine",
        "--csv", csv_path,
    ]
    if tail:
        cmd.extend(["--tail", str(tail)])

    env = os.environ.copy()
    env["COST_PER_LOT"] = str(66 * cost_multiplier)

    print(f"Running backtest: cost_multiplier={cost_multiplier}x (COST_PER_LOT={env['COST_PER_LOT']})")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)

    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-1000:]}")
        return None

    # Parse results from stdout
    output = result.stdout
    results = {}
    for line in output.splitlines():
        if ": " in line and not line.startswith("["):
            key, val = line.split(": ", 1)
            if key in ["net_pnl", "expectancy", "avg_pnl", "win_rate", "profit_factor", "max_drawdown", "trades"]:
                try:
                    results[key] = float(val) if "." in val else int(val)
                except:
                    results[key] = val

    # Save summary
    summary_path = os.path.join(out_dir, f"backtest_summary_{cost_multiplier}x.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Summary saved to {summary_path}")

    # Copy trade log
    import glob
    trade_logs = glob.glob("backtest/results/trade_log_*.csv")
    if trade_logs:
        latest = max(trade_logs, key=os.path.getmtime)
        import shutil
        dest = os.path.join("logs", f"trade_signals_sample_{cost_multiplier}x.csv")
        shutil.copy2(latest, dest)
        print(f"  Trade log saved to {dest}")

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cost-multipliers", default="1,2,3", help="Comma-separated multipliers")
    parser.add_argument("--tail", type=int, default=0, help="Use only last N rows (0=all)")
    parser.add_argument("--out-dir", default="artifacts")
    args = parser.parse_args()

    multipliers = [float(x) for x in args.cost_multipliers.split(",")]

    print(f"Running backtest sweep: {multipliers}")
    all_results = {}

    for mult in multipliers:
        res = run_backtest(mult, tail=args.tail if args.tail > 0 else None, out_dir=args.out_dir)
        if res:
            all_results[f"{mult}x"] = res

    # Save combined summary
    combined = {
        "timestamp": datetime.now().isoformat(),
        "scenarios": all_results
    }
    combined_path = os.path.join(args.out_dir, "backtest_summary_all.json")
    with open(combined_path, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"\nCombined summary saved to {combined_path}")

    # Print comparison
    print("\n=== COMPARISON ===")
    for scenario, res in all_results.items():
        print(f"\n{scenario}:")
        for k, v in res.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()