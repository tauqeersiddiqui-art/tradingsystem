from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_v2.config import PipelineConfig


@dataclass(frozen=True)
class ProductionRegistryPaths:
    registry_dir: Path
    model_dir: Path
    report_dir: Path
    deployment_manifest: Path
    production_registry: Path
    registry_report: Path


def registry_paths(config: PipelineConfig) -> ProductionRegistryPaths:
    registry_dir = config.paths.output_dir / "registry"
    model_dir = config.paths.output_dir / "models" / "production_candidates"
    report_dir = config.paths.output_dir / "reports" / "phase4" / "registry"
    paths = ProductionRegistryPaths(
        registry_dir=registry_dir,
        model_dir=model_dir,
        report_dir=report_dir,
        deployment_manifest=registry_dir / "deployment_manifest.json",
        production_registry=registry_dir / "production_artifact_registry.json",
        registry_report=report_dir / "production_artifact_registry.json",
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
    legacy = legacy_model_path(config, target, model)
    if legacy.exists():
        return legacy
    registered = latest_registry_model_path(config, target, model)
    return registered if registered is not None else legacy


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
