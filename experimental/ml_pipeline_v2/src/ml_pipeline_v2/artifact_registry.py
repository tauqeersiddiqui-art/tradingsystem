from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig


@dataclass(frozen=True)
class ProductionRegistryPaths:
    registry_dir: Path
    model_dir: Path
    report_dir: Path
    deployment_manifest: Path
    deployment_manifest_report: Path
    production_registry: Path
    registry_report: Path
    research_only_registry: Path
    research_only_report: Path


def registry_paths(config: PipelineConfig) -> ProductionRegistryPaths:
    registry_dir = config.paths.output_dir / "registry"
    model_dir = config.paths.output_dir / "models" / "production_candidates"
    report_dir = config.paths.output_dir / "reports" / "phase4" / "registry"
    paths = ProductionRegistryPaths(
        registry_dir=registry_dir,
        model_dir=model_dir,
        report_dir=report_dir,
        deployment_manifest=registry_dir / "deployment_manifest.json",
        deployment_manifest_report=report_dir / "deployment_manifest.json",
        production_registry=registry_dir / "production_artifact_registry.json",
        registry_report=report_dir / "production_artifact_registry.json",
        research_only_registry=registry_dir / "research_only_models.json",
        research_only_report=report_dir / "research_only_models.json",
    )
    for path in (paths.registry_dir, paths.model_dir, paths.report_dir):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def json_default(value: object) -> object:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if pd.isna(value):
        return None
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_model_path(config: PipelineConfig, target: str, model: str) -> Path:
    return config.paths.output_dir / "models" / f"v2_{target}_{model}.joblib"


def registry_model_pattern(target: str, model: str) -> str:
    return f"v2_{target}_{model}_*.joblib"


def latest_registry_model_path(config: PipelineConfig, target: str, model: str) -> Path | None:
    paths = registry_paths(config)
    candidates = sorted(
        paths.model_dir.glob(registry_model_pattern(target, model)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def deployable_model_path(config: PipelineConfig, target: str, model: str) -> Path:
    registered = latest_registry_model_path(config, target, model)
    if registered is not None:
        return registered
    return legacy_model_path(config, target, model)


def load_check(path: Path, feature_count: int) -> dict[str, object]:
    model = joblib.load(path)
    can_predict_proba = hasattr(model, "predict_proba")
    classes = getattr(model, "classes_", None)
    return {
        "loadable": True,
        "object_type": f"{type(model).__module__}.{type(model).__name__}",
        "predict_proba": bool(can_predict_proba),
        "classes": [str(cls) for cls in classes] if classes is not None else None,
        "expected_feature_count": int(feature_count),
    }


def load_production_registry(config: PipelineConfig) -> dict[str, object]:
    registry = read_json(registry_paths(config).production_registry, {})
    if not isinstance(registry, dict):
        raise ValueError("production artifact registry is not a JSON object")
    return registry


def production_registry_records(
    config: PipelineConfig,
    *,
    target: str | None = None,
    champion_only: bool = False,
) -> list[dict[str, object]]:
    registry = load_production_registry(config)
    records = registry.get("artifacts", [])
    if not isinstance(records, list):
        return []
    filtered: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if target is not None and record.get("target") != target:
            continue
        eligibility = record.get("production_eligibility") or {}
        if champion_only and not bool(eligibility.get("champion")):
            continue
        filtered.append(record)
    return filtered


def load_registered_model(
    config: PipelineConfig,
    target: str,
    model: str | None = None,
    *,
    champion_only: bool = True,
) -> object:
    records = production_registry_records(config, target=target, champion_only=champion_only)
    if model is not None:
        records = [record for record in records if record.get("model") == model]
    if not records:
        raise KeyError(f"no registered artifact for target={target!r}, model={model!r}")
    records = sorted(
        records,
        key=lambda record: int(
            ((record.get("production_eligibility") or {}).get("production_rank_for_target")) or 999
        ),
    )
    path = Path(str(records[0]["artifact_path"]))
    return joblib.load(path)


def _timestamp_utc() -> str:
    return pd.Timestamp.utcnow().isoformat()


def _row_value(row: pd.Series, key: str, default: object = None) -> object:
    value = row.get(key, default)
    if isinstance(value, float) and pd.isna(value):
        return default
    return value


def _bool_value(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    if pd.isna(value):
        return False
    return bool(value)


def _float_value(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _int_value(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def artifact_origin(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if "production_candidates" in parts:
        return "phase4_registry_training"
    return "phase2_primary_training"


def artifact_version(path: Path, target: str, model: str) -> str:
    prefix = f"v2_{target}_{model}_"
    stem = path.stem
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    return "phase2_primary"


def _lookup_record(frame: pd.DataFrame, target: str, model: str) -> dict[str, object]:
    if frame.empty or not {"target", "model"}.issubset(frame.columns):
        return {}
    matches = frame[(frame["target"] == target) & (frame["model"] == model)]
    if matches.empty:
        return {}
    return matches.iloc[0].to_dict()


def _metric_snapshot(row: pd.Series) -> dict[str, object]:
    keys = (
        "auc",
        "pr_auc",
        "average_precision",
        "brier",
        "ece",
        "precision",
        "recall",
        "trade_count",
        "selected_positives",
        "expectancy",
        "profit_factor",
        "max_drawdown",
        "net_pnl",
        "selection_score",
        "rank_for_target",
        "production_rank_for_target",
        "global_rank",
    )
    return {key: _row_value(row, key) for key in keys if key in row.index}


def _research_only_reason(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if not _bool_value(_row_value(row, "meets_expectancy_floor")):
        reasons.append("expectancy floor failed")
    if not _bool_value(_row_value(row, "meets_profit_factor_floor")):
        reasons.append("profit-factor floor failed")
    if not _bool_value(_row_value(row, "meets_calibration_floor")):
        reasons.append("calibration floor failed")
    if not _bool_value(_row_value(row, "meets_trade_count_floor")):
        reasons.append("trade-count floor failed")
    if not _bool_value(_row_value(row, "artifact_available")):
        reasons.append("deployable artifact is unavailable")
    return reasons or ["not production eligible"]


def production_artifact_record(
    config: PipelineConfig,
    row: pd.Series,
    champion_keys: set[tuple[str, str]],
    thresholds: pd.DataFrame,
    risk: pd.DataFrame,
) -> dict[str, object] | None:
    target = str(row["target"])
    model = str(row["model"])
    path = Path(str(_row_value(row, "model_artifact_path", deployable_model_path(config, target, model))))
    if not path.exists():
        return None

    threshold_record = _lookup_record(thresholds, target, model)
    risk_record = _lookup_record(risk, target, model)
    try:
        load_metadata = load_check(path, len(FEATURE_COLUMNS))
    except Exception as exc:
        load_metadata = {
            "loadable": False,
            "error": str(exc),
            "expected_feature_count": len(FEATURE_COLUMNS),
        }

    loadable = _bool_value(load_metadata.get("loadable"))
    metric_viable = _bool_value(_row_value(row, "metric_viable"))
    artifact_available = _bool_value(_row_value(row, "artifact_available"))
    production_viable = _bool_value(_row_value(row, "production_viable")) and loadable
    champion = (target, model) in champion_keys
    checksum = file_sha256(path)

    return {
        "model": model,
        "target": target,
        "side": _row_value(row, "side"),
        "artifact_path": str(path),
        "artifact_origin": artifact_origin(path),
        "version": artifact_version(path, target, model),
        "checksum": {
            "algorithm": "sha256",
            "value": checksum,
        },
        "training_metadata": {
            "source": "phase3_comparison_window",
            "rows": _int_value(_row_value(row, "rows")),
            "train_rows": _int_value(_row_value(row, "train_rows")),
            "calibration_rows": _int_value(_row_value(row, "calibration_rows")),
            "test_rows": _int_value(_row_value(row, "test_rows")),
            "feature_columns": list(FEATURE_COLUMNS),
            "feature_count": len(FEATURE_COLUMNS),
            "metrics": _metric_snapshot(row),
        },
        "calibration_metadata": {
            "method": _row_value(row, "calibration"),
            "ece": _float_value(_row_value(row, "ece")),
            "brier": _float_value(_row_value(row, "brier")),
            "phase3_threshold": _float_value(_row_value(row, "threshold")),
            "recommended_threshold": _float_value(threshold_record.get("recommended_threshold")),
            "threshold_policy": threshold_record.get("threshold_policy"),
            "base_threshold": _float_value(threshold_record.get("base_threshold")),
            "aggressive_threshold": _float_value(threshold_record.get("aggressive_threshold")),
            "defensive_threshold": _float_value(threshold_record.get("defensive_threshold")),
        },
        "risk_metadata": {
            "risk_gate_passed": _bool_value(risk_record.get("risk_gate_passed")),
            "drawdown_gate_passed": _bool_value(risk_record.get("drawdown_gate_passed")),
            "risk_of_ruin_pct": _float_value(risk_record.get("risk_of_ruin_pct")),
            "p05_max_drawdown": _float_value(risk_record.get("p05_max_drawdown")),
            "median_max_drawdown": _float_value(risk_record.get("median_max_drawdown")),
        },
        "load_metadata": load_metadata,
        "production_eligibility": {
            "metric_viable": metric_viable,
            "artifact_available": artifact_available,
            "loadable_without_retraining": loadable,
            "production_viable": production_viable,
            "champion": champion,
            "rank_for_target": _int_value(_row_value(row, "rank_for_target")),
            "production_rank_for_target": _int_value(_row_value(row, "production_rank_for_target")),
            "manual_review_required_before_promotion": True,
            "auto_promoted": False,
            "promotion_status": "candidate_not_promoted",
        },
    }


def write_production_registry(
    config: PipelineConfig,
    scored: pd.DataFrame,
    champions: pd.DataFrame,
    thresholds: pd.DataFrame,
    risk: pd.DataFrame,
    extra_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    paths = registry_paths(config)
    generated_at = _timestamp_utc()
    champion_keys = set()
    if not champions.empty and {"target", "model"}.issubset(champions.columns):
        champion_keys = set(zip(champions["target"].astype(str), champions["model"].astype(str)))

    production_rows = scored[
        scored.get("metric_viable", False).astype(bool)
        & scored.get("artifact_available", False).astype(bool)
    ].copy()
    records = []
    for _, row in production_rows.iterrows():
        record = production_artifact_record(config, row, champion_keys, thresholds, risk)
        if record is not None:
            records.append(record)

    records = sorted(
        records,
        key=lambda record: (
            str(record["target"]),
            int((record["production_eligibility"] or {}).get("production_rank_for_target") or 999),
            -float(((record["training_metadata"] or {}).get("metrics") or {}).get("selection_score") or 0.0),
        ),
    )
    by_target: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_target.setdefault(str(record["target"]), []).append(record)
    champion_records = [
        record
        for record in records
        if _bool_value((record.get("production_eligibility") or {}).get("champion"))
    ]

    research_rows = []
    for _, row in scored.iterrows():
        if _bool_value(_row_value(row, "metric_viable")) and _bool_value(_row_value(row, "artifact_available")):
            continue
        research_rows.append(
            {
                "model": row.get("model"),
                "target": row.get("target"),
                "side": row.get("side"),
                "selection_score": _float_value(row.get("selection_score")),
                "metric_viable": _bool_value(row.get("metric_viable")),
                "artifact_available": _bool_value(row.get("artifact_available")),
                "production_viable": False,
                "research_only_reasons": _research_only_reason(row),
            }
        )

    controls = {
        "uses_experimental_artifacts_only": True,
        "production_files_modified": False,
        "manual_review_required_before_promotion": True,
        "auto_promotion_enabled": False,
    }
    registry = {
        "schema_version": "ml_pipeline_v2.production_artifact_registry.v1",
        "generated_at_utc": generated_at,
        "artifact_count": len(records),
        "champion_count": len(champion_records),
        "research_only_count": len(research_rows),
        "controls": controls,
        "metadata": extra_metadata or {},
        "artifacts": records,
        "by_target": by_target,
        "champions": champion_records,
    }
    deployment_manifest = {
        "schema_version": "ml_pipeline_v2.deployment_manifest.v1",
        "generated_at_utc": generated_at,
        "deployment_status": "candidate_not_promoted",
        "controls": controls,
        "artifacts": records,
        "champions": champion_records,
    }
    research_only = {
        "schema_version": "ml_pipeline_v2.research_only_models.v1",
        "generated_at_utc": generated_at,
        "models": research_rows,
    }

    write_json(paths.production_registry, registry)
    write_json(paths.registry_report, registry)
    write_json(paths.deployment_manifest, deployment_manifest)
    write_json(paths.deployment_manifest_report, deployment_manifest)
    write_json(paths.research_only_registry, research_only)
    write_json(paths.research_only_report, research_only)
    return {
        "registry": str(paths.production_registry),
        "deployment_manifest": str(paths.deployment_manifest),
        "research_only": str(paths.research_only_registry),
        "artifact_count": len(records),
        "champion_count": len(champion_records),
        "research_only_count": len(research_rows),
    }
