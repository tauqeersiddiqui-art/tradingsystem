from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from ml_pipeline_v2.audit import (  # noqa: E402
    bucket_rates,
    feature_ranges,
    label_audit,
    read_audit_dataset,
    to_jsonable,
    write_markdown_report,
)
from ml_pipeline_v2.config import PipelineConfig, ensure_output_dirs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Pipeline V2 Phase 1 audit.")
    parser.add_argument(
        "--dataset",
        default=str(PipelineConfig().paths.dataset_v3),
        help="Dataset CSV to audit. Defaults to training_dataset_v3.csv.",
    )
    parser.add_argument(
        "--out",
        default=str(PipelineConfig().paths.output_dir / "reports"),
        help="Output report directory.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = read_audit_dataset(Path(args.dataset))
    audit = label_audit(df)
    buckets = bucket_rates(df)
    ranges = feature_ranges(df)

    json_path = out_dir / "phase1_audit.json"
    md_path = out_dir / "phase1_audit.md"
    json_path.write_text(
        json.dumps(to_jsonable(audit, buckets, ranges), indent=2),
        encoding="utf-8",
    )
    write_markdown_report(md_path, audit, buckets, ranges)

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        "Summary: rows={:,} CE_pos={:.4%} PE_pos={:.4%}".format(
            audit.rows,
            audit.ce_positive_rate,
            audit.pe_positive_rate,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

