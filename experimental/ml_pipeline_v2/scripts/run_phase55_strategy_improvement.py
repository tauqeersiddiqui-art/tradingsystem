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
from ml_pipeline_v2.phase4 import json_default  # noqa: E402
from ml_pipeline_v2.phase55 import run_phase55_autonomous_strategy_improvement  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Pipeline V2 Phase 5.5 autonomous strategy improvement reports."
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=200,
        help="Minimum trades required before a recommendation can pass validation.",
    )
    parser.add_argument(
        "--min-trade-coverage",
        type=float,
        default=0.20,
        help="Minimum share of baseline trades retained to reduce overfit risk.",
    )
    parser.add_argument(
        "--max-combination-size",
        type=int,
        default=3,
        help="Maximum number of recommendation filters tested together.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    summary = run_phase55_autonomous_strategy_improvement(
        config=config,
        min_trades=args.min_trades,
        min_trade_coverage=args.min_trade_coverage,
        max_combination_size=args.max_combination_size,
    )
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
