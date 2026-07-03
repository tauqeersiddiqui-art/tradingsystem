from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import joblib
import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
SCRIPTS = ROOT / "experimental" / "ml_pipeline_v2" / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SCRIPTS))

from ml_pipeline_v2.artifact_registry import (  # noqa: E402
    file_sha256,
    json_default,
    latest_registry_model_path,
    load_check,
    registry_paths,
    write_json,
)
from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig, ensure_output_dirs  # noqa: E402
from ml_pipeline_v2.models import classifier_candidates  # noqa: E402
from run_phase3_validation import (  # noqa: E402
    BINARY_TARGETS,
    prepare_dataset,
    resolve_dataset,
    threshold_metrics,
)
from train_pipeline_v2 import fit_calibrated_classifier, time_split  # noqa: E402


PRODUCTION_CAPABLE_MODELS = {"lgbm", "catboost", "xgboost", "random_forest"}
RESEARCH_ONLY_MODELS = {"mlp"}


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def available_classifier_specs(config: PipelineConfig) -> dict[str, object]:
    return {
        spec.name: spec
        for spec in classifier_candidates(config.validation.random_seed)
        if spec.available and spec.builder is not None
    }


def research_champions(scored: pd.DataFrame) -> pd.DataFrame:
    eligible = scored[scored["metric_viable"].astype(bool)].copy()
    eligible = eligible[eligible["model"].isin(PRODUCTION_CAPABLE_MODELS)]
    eligible = eligible.sort_values(["target", "selection_score"], ascending=[True, False])
    return eligible.groupby("target", as_index=False).head(1).reset_index(drop=True)


def version_for(target: str, model: str, dataset_path: Path) -> str:
    dataset_hash = file_sha256(dataset_path)[:12]
    return f"{pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{dataset_hash}"


def artifact_path(target: str, model: str, version: str, config: PipelineConfig) -> Path:
    return registry_paths(config).model_dir / f"v2_{target}_{model}_{version}.joblib"


def build_artifact_for_row(
    row: pd.Series,
    comparison_df: pd.DataFrame,
    full_df: pd.DataFrame,
    dataset_path: Path,
    config: PipelineConfig,
    force: bool,
) -> dict[str, object]:
    target = str(row["target"])
    model_name = str(row["model"])
    specs = available_classifier_specs(config)
    if model_name not in specs:
        return {
            "target": target,
            "model": model_name,
            "status": "skipped_unavailable",
            "reason": "model builder is not available in this environment",
        }
    if model_name in RESEARCH_ONLY_MODELS:
        return {
            "target": target,
            "model": model_name,
            "status": "skipped_research_only",
            "reason": "model family is explicitly research-only",
        }
    if model_name not in PRODUCTION_CAPABLE_MODELS:
        return {
            "target": target,
            "model": model_name,
            "status": "skipped_not_production_capable",
            "reason": "model family is not in the production-capable allowlist",
        }
    existing = latest_registry_model_path(config, target, model_name)
    if existing is not None and existing.exists() and not force:
        return {
            "target": target,
            "model": model_name,
            "status": "already_registered",
            "artifact_path": str(existing),
            "checksum": file_sha256(existing),
            "load_check": load_check(existing, len(FEATURE_COLUMNS)),
        }

    comparison_rows = int(row.get("rows") or 0)
    training_df = full_df.iloc[-comparison_rows:].reset_index(drop=True) if comparison_rows else full_df
    train, cal, test = time_split(training_df)
    target_info = BINARY_TARGETS[target]
    target_col = target_info["target"]
    value_col = target_info["value"]
    if train[target_col].nunique() < 2 or cal[target_col].nunique() < 2:
        return {
            "target": target,
            "model": model_name,
            "status": "skipped_single_class",
            "reason": "single-class train or calibration split",
        }

    spec = specs[model_name]
    start = time.perf_counter()
    fitted = fit_calibrated_classifier(
        spec,
        train[FEATURE_COLUMNS],
        train[target_col],
        cal[FEATURE_COLUMNS],
        cal[target_col],
    )
    version = version_for(target, model_name, dataset_path)
    path = artifact_path(target, model_name, version, config)
    joblib.dump(fitted, path)
    elapsed = time.perf_counter() - start

    cal_prob = fitted.predict_proba(cal[FEATURE_COLUMNS])[:, 1]
    test_prob = fitted.predict_proba(test[FEATURE_COLUMNS])[:, 1]
    gate = threshold_metrics(
        test[target_col].to_numpy(),
        test_prob,
        row.get("threshold"),
        test[value_col].to_numpy(),
    )
    comparison_match = comparison_df[
        (comparison_df["target"] == target) & (comparison_df["model"] == model_name)
    ]
    comparison_record = comparison_match.iloc[0].to_dict() if not comparison_match.empty else {}
    checksum = file_sha256(path)
    metadata = {
        "schema_version": "ml_pipeline_v2.production_artifact_metadata.v1",
        "target": target,
        "model": model_name,
        "version": version,
        "artifact_path": str(path),
        "checksum": {
            "algorithm": "sha256",
            "value": checksum,
        },
        "training_metadata": {
            "source": "phase3_comparison_window",
            "dataset": str(dataset_path),
            "dataset_sha256": file_sha256(dataset_path),
            "rows": int(len(training_df)),
            "train_rows": int(len(train)),
            "calibration_rows": int(len(cal)),
            "test_rows": int(len(test)),
            "feature_columns": list(FEATURE_COLUMNS),
            "feature_count": len(FEATURE_COLUMNS),
            "target_column": target_col,
            "value_column": value_col,
            "train_date_min": train["date"].min().isoformat(),
            "train_date_max": train["date"].max().isoformat(),
            "calibration_date_min": cal["date"].min().isoformat(),
            "calibration_date_max": cal["date"].max().isoformat(),
            "test_date_min": test["date"].min().isoformat(),
            "test_date_max": test["date"].max().isoformat(),
            "fit_seconds": elapsed,
        },
        "calibration_metadata": {
            "method": "isotonic_time_split",
            "calibration_rows": int(len(cal)),
            "phase3_threshold": float(row["threshold"]) if pd.notna(row.get("threshold")) else None,
            "calibration_probability_min": float(cal_prob.min()),
            "calibration_probability_max": float(cal_prob.max()),
            "calibration_probability_mean": float(cal_prob.mean()),
        },
        "production_eligibility": {
            "metric_viable": bool(row.get("metric_viable")),
            "production_capable_model": True,
            "artifact_available": True,
            "auto_promoted": False,
            "promotion_status": "candidate_not_promoted",
        },
        "phase3_comparison": comparison_record,
        "artifact_gate_check": gate,
        "load_check": load_check(path, len(FEATURE_COLUMNS)),
    }
    metadata_path = path.with_suffix(".metadata.json")
    write_json(metadata_path, metadata)
    return {
        "target": target,
        "model": model_name,
        "status": "created",
        "artifact_path": str(path),
        "metadata_path": str(metadata_path),
        "checksum": checksum,
        "version": version,
        "fit_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persist deployable Pipeline V2 artifacts for Phase 4 production-capable champions."
    )
    parser.add_argument("--dataset", default=str(PipelineConfig().paths.dataset_v3))
    parser.add_argument(
        "--comparison-max-rows",
        type=int,
        default=150_000,
        help="Must match Phase 3 model comparison window.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new registry artifact version even if one already exists.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Include champions that already have legacy or registry artifacts in the build report.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    paths = registry_paths(config)
    dataset_path = resolve_dataset(args.dataset)

    rankings_path = config.paths.output_dir / "reports" / "phase4" / "recommendations" / "trading_model_rankings.csv"
    comparison_path = config.paths.output_dir / "reports" / "comparison" / "model_comparison.csv"
    if not rankings_path.exists():
        raise FileNotFoundError(str(rankings_path))
    if not comparison_path.exists():
        raise FileNotFoundError(str(comparison_path))

    scored = pd.read_csv(rankings_path)
    comparison = pd.read_csv(comparison_path)
    champions = research_champions(scored)
    if not args.include_existing:
        champions = champions[~champions["artifact_available"].astype(bool)].copy()

    df = prepare_dataset(dataset_path, config)
    if args.comparison_max_rows:
        df_for_check = df.iloc[-args.comparison_max_rows:].reset_index(drop=True)
        if not champions.empty:
            expected_rows = set(champions["rows"].dropna().astype(int).tolist())
            expected_rows.discard(len(df_for_check))
            if expected_rows:
                raise ValueError(
                    f"Phase 4 rankings expect comparison row counts {sorted(expected_rows)} "
                    f"but --comparison-max-rows produced {len(df_for_check)} rows"
                )

    results = [
        build_artifact_for_row(
            row,
            comparison_df=comparison,
            full_df=df,
            dataset_path=dataset_path,
            config=config,
            force=args.force,
        )
        for _, row in champions.iterrows()
    ]
    research_only = [
        {
            "model": model,
            "reason": "explicit research-only model family; not persisted as production artifact",
        }
        for model in sorted(RESEARCH_ONLY_MODELS)
    ]
    summary = {
        "schema_version": "ml_pipeline_v2.production_artifact_build.v1",
        "status": "ok",
        "dataset": str(dataset_path),
        "comparison_max_rows": int(args.comparison_max_rows),
        "production_capable_models": sorted(PRODUCTION_CAPABLE_MODELS),
        "research_only_models": research_only,
        "candidate_rows": int(len(champions)),
        "created": int(sum(1 for item in results if item.get("status") == "created")),
        "already_registered": int(sum(1 for item in results if item.get("status") == "already_registered")),
        "skipped": int(sum(1 for item in results if str(item.get("status", "")).startswith("skipped"))),
        "results": results,
    }
    summary_path = paths.report_dir / "production_artifact_build_summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
