from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from ml_pipeline_v2.config import PipelineConfig, ensure_output_dirs  # noqa: E402
from ml_pipeline_v2.validation import monte_carlo_risk, trade_metrics  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate isolated Pipeline V2 artifacts.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--pnl-csv",
        default="",
        help="Optional CSV with a net_pnl column for Monte Carlo validation.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)

    report = {
        "status": "dry_run" if args.dry_run else "candidate_validation",
        "notes": [
            "This script validates V2 candidate artifacts only.",
            "It never reads or writes production champion model paths.",
        ],
    }

    if args.pnl_csv:
        import pandas as pd

        df = pd.read_csv(args.pnl_csv)
        pnl = df["net_pnl"].to_numpy(dtype=float)
        report["trade_metrics"] = trade_metrics(pnl)
        report["monte_carlo"] = monte_carlo_risk(pnl, start_capital=100_000.0)
    else:
        sample = np.array([300, -180, 420, -250, 150, -120], dtype=float)
        report["example_trade_metrics"] = trade_metrics(sample)
        report["example_monte_carlo"] = monte_carlo_risk(sample, start_capital=100_000.0, runs=200)

    if args.dry_run:
        print(json.dumps(report, indent=2))
    else:
        path = config.paths.output_dir / "reports" / "validation_report.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

