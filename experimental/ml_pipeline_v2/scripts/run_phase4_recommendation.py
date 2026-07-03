from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from ml_pipeline_v2.config import PipelineConfig, ensure_output_dirs  # noqa: E402
from ml_pipeline_v2.phase4 import json_default, run_phase4_recommendation  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Pipeline V2 Phase 4 production recommendation engine."
    )
    parser.add_argument(
        "--start-capital",
        type=float,
        default=1_000_000.0,
        help="Capital base used for Monte Carlo risk simulation.",
    )
    parser.add_argument(
        "--ruin-fraction",
        type=float,
        default=0.30,
        help="Fractional drawdown from start capital treated as ruin.",
    )
    parser.add_argument(
        "--monte-carlo-runs",
        type=int,
        default=2000,
        help="Monte Carlo bootstrap runs per model-target row.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    summary = run_phase4_recommendation(
        config=config,
        start_capital=args.start_capital,
        ruin_fraction=args.ruin_fraction,
        monte_carlo_runs=args.monte_carlo_runs,
    )
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
