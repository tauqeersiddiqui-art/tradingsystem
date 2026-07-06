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
from ml_pipeline_v2.phase7 import run_phase7_adaptive_exit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 7: Adaptive Exit Intelligence — live-feasible exit strategy research."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print config and exit without running. Useful for path verification.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "repo_root": str(ROOT)}, indent=2))
        return 0

    summary = run_phase7_adaptive_exit(config=config)
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
