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
from ml_pipeline_v2.phase5 import run_phase5_profitability_intelligence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Pipeline V2 Phase 5 offline profitability intelligence reports."
    )
    parser.add_argument(
        "--split",
        choices=("train", "calibration", "test", "all"),
        default="test",
        help="Chronological split to analyze. Defaults to the held-out test split.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Optional dataset row cap for fast offline research runs.",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=200,
        help="Minimum trades required before a filter or strategy can be recommended.",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=0,
        help="Optional cap for per-trade reviews. Zero reviews every completed trade in the split.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    summary = run_phase5_profitability_intelligence(
        config=config,
        split=args.split,
        max_rows=args.max_rows,
        min_trades=args.min_trades,
        review_limit=args.review_limit,
    )
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

