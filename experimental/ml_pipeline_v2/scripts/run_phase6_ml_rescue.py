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
from ml_pipeline_v2.phase6 import run_phase6_ml_rescue  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6: Normal ML Rescue Engine — 7-module deep analysis."
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=200,
        help="Minimum Phase 5 trades required before Phase 6 can run.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    summary = run_phase6_ml_rescue(config=config, min_trades=args.min_trades)
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
