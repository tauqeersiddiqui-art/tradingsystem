from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.exceptions import ConvergenceWarning
from sklearn.inspection import permutation_importance

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
SCRIPTS = ROOT / "experimental" / "ml_pipeline_v2" / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SCRIPTS))

from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig, ensure_output_dirs  # noqa: E402
from ml_pipeline_v2.models import classifier_candidates  # noqa: E402
from ml_pipeline_v2.validation import (  # noqa: E402
    classification_metrics,
    expected_calibration_error,
    purged_walkforward_splits,
    threshold_recommendation_report,
    trade_metrics,
)
from train_pipeline_v2 import (  # noqa: E402
    add_regime_proxy_labels,
    add_v2_labels,
    fit_calibrated_classifier,
    time_split,
)


warnings.filterwarnings("ignore", category=ConvergenceWarning)


BINARY_TARGETS: dict[str, dict[str, str]] = {
    "directional_ce": {
        "target": "v2_label_ce_direction",
        "value": "v2_quality_net_ce_rs",
        "side": "ce",
    },
    "directional_pe": {
        "target": "v2_label_pe_direction",
        "value": "v2_quality_net_pe_rs",
        "side": "pe",
    },
    "quality_profitable_ce": {
        "target": "v2_quality_ce_profitable",
        "value": "v2_quality_net_ce_rs",
        "side": "ce",
    },
    "quality_profitable_pe": {
        "target": "v2_quality_pe_profitable",
        "value": "v2_quality_net_pe_rs",
        "side": "pe",
    },
    "target_hit_ce": {
        "target": "v2_ce_target_hit",
        "value": "v2_quality_net_ce_rs",
        "side": "ce",
    },
    "target_hit_pe": {
        "target": "v2_pe_target_hit",
        "value": "v2_quality_net_pe_rs",
        "side": "pe",
    },
    "stop_hit_ce": {
        "target": "v2_ce_stop_hit",
        "value": "v2_quality_net_ce_rs",
        "side": "ce",
    },
    "stop_hit_pe": {
        "target": "v2_pe_stop_hit",
        "value": "v2_quality_net_pe_rs",
        "side": "pe",
    },
}

PRIMARY_IMPORTANCE_TARGETS = (
    "directional_ce",
    "directional_pe",
    "quality_profitable_ce",
    "quality_profitable_pe",
)


def json_default(value):
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
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


def report_dirs(config: PipelineConfig) -> dict[str, Path]:
    base = config.paths.output_dir / "reports"
    dirs = {
        "validation": base / "validation",
        "walkforward": base / "walkforward",
        "comparison": base / "comparison",
        "drift": base / "drift",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def resolve_dataset(path: str) -> Path:
    dataset_path = Path(path)
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path
    return dataset_path


def prepare_dataset(dataset_path: Path, config: PipelineConfig, max_rows: int = 0) -> pd.DataFrame:
    df = pd.read_csv(dataset_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if max_rows and len(df) > max_rows:
        df = df.iloc[-max_rows:].reset_index(drop=True)
    for col in FEATURE_COLUMNS + ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=FEATURE_COLUMNS + ["close"])

    df = add_v2_labels(
        df,
        directional_lookahead=config.labels.directional_lookahead_bars,
        quality_lookahead=config.labels.quality_lookahead_bars,
        target_points=config.labels.directional_target_points,
        adverse_mult=config.labels.directional_max_adverse_multiple,
        spread_points_round_trip=config.labels.spread_points_round_trip,
        lot_units=config.labels.lot_units,
        brokerage_round_trip_rs=config.labels.brokerage_round_trip_rs,
    )
    train, cal, test = time_split(df)
    regime_volatility_threshold = float(train["volatility"].quantile(0.90))
    train = add_regime_proxy_labels(train, regime_volatility_threshold)
    cal = add_regime_proxy_labels(cal, regime_volatility_threshold)
    test = add_regime_proxy_labels(test, regime_volatility_threshold)
    df = pd.concat([train, cal, test], axis=0).sort_index().reset_index(drop=True)
    df.attrs["regime_volatility_threshold"] = regime_volatility_threshold
    return df


def first_available_classifier(random_seed: int):
    for spec in classifier_candidates(random_seed):
        if spec.available and spec.builder is not None:
            return spec
    raise RuntimeError("no available classifier candidate")


def merge_rows(
    existing_rows: list[dict[str, object]],
    new_rows: list[dict[str, object]],
    key_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    merged: dict[tuple[object, ...], dict[str, object]] = {}
    for row in existing_rows + new_rows:
        key = tuple(row.get(field) for field in key_fields)
        merged[key] = row
    return list(merged.values())


def split_inner_train_calibration(
    train_idx: np.ndarray,
    calibration_fraction: float,
    min_calibration_rows: int = 1000,
) -> tuple[np.ndarray, np.ndarray]:
    if len(train_idx) <= min_calibration_rows + 100:
        raise ValueError("not enough rows for inner train/calibration split")
    cal_rows = max(min_calibration_rows, int(len(train_idx) * calibration_fraction))
    cal_rows = min(cal_rows, max(1, len(train_idx) // 3))
    return train_idx[:-cal_rows], train_idx[-cal_rows:]


def threshold_metrics(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    threshold: float | None,
    pnl: np.ndarray,
) -> dict[str, float | int | None]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    pnl = np.asarray(pnl).astype(float)
    positives = int(y_true.sum())
    if threshold is None:
        selected = np.zeros(len(y_true), dtype=bool)
    else:
        selected = y_prob >= float(threshold)
    selected_count = int(selected.sum())
    selected_positive = int(y_true[selected].sum()) if selected_count else 0
    precision = float(selected_positive / selected_count) if selected_count else None
    recall = float(selected_positive / positives) if positives else None
    metrics = trade_metrics(pnl[selected])
    return {
        "threshold": float(threshold) if threshold is not None else None,
        "precision": precision,
        "recall": recall,
        "trade_count": selected_count,
        "selected_positives": selected_positive,
        "expectancy": metrics["expectancy"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "net_pnl": metrics["net_pnl"],
    }


def probability_summary(prob: np.ndarray) -> dict[str, float]:
    prob = np.asarray(prob, dtype=float)
    return {
        "prob_mean": float(prob.mean()),
        "prob_std": float(prob.std(ddof=0)),
        "prob_p05": float(np.quantile(prob, 0.05)),
        "prob_p50": float(np.quantile(prob, 0.50)),
        "prob_p95": float(np.quantile(prob, 0.95)),
    }


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    expected = np.asarray(expected, dtype=float)
    actual = np.asarray(actual, dtype=float)
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if len(expected) == 0 or len(actual) == 0:
        return float("nan")
    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.unique(np.quantile(expected, quantiles))
    if len(edges) <= 2:
        lo = min(float(expected.min()), float(actual.min()))
        hi = max(float(expected.max()), float(actual.max()))
        if lo == hi:
            return 0.0
        edges = np.linspace(lo, hi, bins + 1)
    edges[0] = -np.inf
    edges[-1] = np.inf
    expected_counts, _ = np.histogram(expected, bins=edges)
    actual_counts, _ = np.histogram(actual, bins=edges)
    expected_pct = np.maximum(expected_counts / max(1, expected_counts.sum()), 1e-6)
    actual_pct = np.maximum(actual_counts / max(1, actual_counts.sum()), 1e-6)
    return float(np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct)))


def binary_distribution_psi(reference: np.ndarray, actual: np.ndarray) -> float:
    reference = np.asarray(reference).astype(int)
    actual = np.asarray(actual).astype(int)
    ref_pos = np.clip(reference.mean(), 1e-6, 1 - 1e-6)
    act_pos = np.clip(actual.mean(), 1e-6, 1 - 1e-6)
    ref = np.array([1.0 - ref_pos, ref_pos])
    cur = np.array([1.0 - act_pos, act_pos])
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def run_walkforward(
    df: pd.DataFrame,
    config: PipelineConfig,
    dirs: dict[str, Path],
) -> tuple[pd.DataFrame, dict[str, list[np.ndarray]], list[int], pd.DataFrame]:
    spec = first_available_classifier(config.validation.random_seed)
    rows: list[dict[str, object]] = []
    fold_feature_rows: list[dict[str, object]] = []
    probabilities: dict[str, list[np.ndarray]] = defaultdict(list)
    walkforward_test_indices: list[int] = []

    splits = list(
        purged_walkforward_splits(
            len(df),
            folds=config.validation.walkforward_folds,
            min_train_rows=config.validation.min_train_rows,
            purge_bars=config.validation.purge_bars,
            embargo_bars=config.validation.embargo_bars,
        )
    )
    if not splits:
        raise RuntimeError("no walk-forward splits were generated")

    for fold, (train_idx, test_idx) in enumerate(splits, start=1):
        inner_train_idx, inner_cal_idx = split_inner_train_calibration(
            train_idx,
            config.validation.calibration_fraction,
        )
        train_end_nominal = int(test_idx[0] - config.validation.purge_bars)
        actual_gap = int(test_idx[0] - train_idx[-1] - 1)
        leakage_check = {
            "train_max_index": int(train_idx[-1]),
            "inner_train_max_index": int(inner_train_idx[-1]),
            "inner_cal_min_index": int(inner_cal_idx[0]),
            "inner_cal_max_index": int(inner_cal_idx[-1]),
            "test_min_index": int(test_idx[0]),
            "nominal_train_end_index": train_end_nominal,
            "required_purge_bars": config.validation.purge_bars,
            "required_embargo_bars": config.validation.embargo_bars,
            "actual_gap_bars": actual_gap,
            "passed": bool(actual_gap >= config.validation.purge_bars + config.validation.embargo_bars),
        }

        x_train = df.iloc[inner_train_idx][FEATURE_COLUMNS]
        x_cal = df.iloc[inner_cal_idx][FEATURE_COLUMNS]
        x_test = df.iloc[test_idx][FEATURE_COLUMNS]
        walkforward_test_indices.extend(test_idx.tolist())

        feature_reference = df.iloc[inner_train_idx]
        feature_current = df.iloc[test_idx]
        fold_feature_psi = [
            psi(feature_reference[col].to_numpy(), feature_current[col].to_numpy())
            for col in FEATURE_COLUMNS
        ]
        fold_feature_rows.append(
            {
                "fold": fold,
                "feature_psi_mean": float(np.nanmean(fold_feature_psi)),
                "feature_psi_max": float(np.nanmax(fold_feature_psi)),
                "feature_psi_p95": float(np.nanquantile(fold_feature_psi, 0.95)),
            }
        )

        for target_name, target_info in BINARY_TARGETS.items():
            target_col = target_info["target"]
            value_col = target_info["value"]
            y_train = df.iloc[inner_train_idx][target_col]
            y_cal = df.iloc[inner_cal_idx][target_col]
            y_test = df.iloc[test_idx][target_col].to_numpy()
            pnl_test = df.iloc[test_idx][value_col].to_numpy()

            if y_train.nunique() < 2 or y_cal.nunique() < 2 or len(np.unique(y_test)) < 2:
                rows.append(
                    {
                        "fold": fold,
                        "target": target_name,
                        "model": spec.name,
                        "status": "skipped_single_class",
                        **leakage_check,
                    }
                )
                continue

            model = fit_calibrated_classifier(spec, x_train, y_train, x_cal, y_cal)
            cal_prob = model.predict_proba(x_cal)[:, 1]
            test_prob = model.predict_proba(x_test)[:, 1]
            probabilities[target_name].append(test_prob)
            metrics = classification_metrics(y_test, test_prob)
            threshold_report = threshold_recommendation_report(
                y_cal.to_numpy(),
                cal_prob,
                min_samples=max(100, int(len(y_cal) * 0.005)),
                value=df.iloc[inner_cal_idx][value_col].to_numpy(),
            )
            recommended = threshold_report.get("recommended") or {}
            selected_threshold = recommended.get("threshold")
            gate_metrics = threshold_metrics(y_test, test_prob, selected_threshold, pnl_test)
            rows.append(
                {
                    "fold": fold,
                    "target": target_name,
                    "model": spec.name,
                    "status": "ok",
                    "train_rows": int(len(inner_train_idx)),
                    "calibration_rows": int(len(inner_cal_idx)),
                    "test_rows": int(len(test_idx)),
                    "train_date_min": df.iloc[inner_train_idx]["date"].min().isoformat(),
                    "train_date_max": df.iloc[inner_train_idx]["date"].max().isoformat(),
                    "calibration_date_min": df.iloc[inner_cal_idx]["date"].min().isoformat(),
                    "calibration_date_max": df.iloc[inner_cal_idx]["date"].max().isoformat(),
                    "test_date_min": df.iloc[test_idx]["date"].min().isoformat(),
                    "test_date_max": df.iloc[test_idx]["date"].max().isoformat(),
                    "auc": metrics.auc,
                    "average_precision": metrics.average_precision,
                    "pr_auc": metrics.average_precision,
                    "brier": metrics.brier,
                    "ece": metrics.expected_calibration_error,
                    "target_rate": metrics.target_mean,
                    **probability_summary(test_prob),
                    **gate_metrics,
                    **leakage_check,
                }
            )

    fold_metrics = pd.DataFrame(rows)
    feature_stability = pd.DataFrame(fold_feature_rows)
    fold_metrics.to_csv(dirs["walkforward"] / "fold_metrics.csv", index=False)
    write_json(dirs["walkforward"] / "fold_metrics.json", rows)
    feature_stability.to_csv(dirs["walkforward"] / "fold_feature_stability.csv", index=False)
    write_json(dirs["walkforward"] / "fold_feature_stability.json", fold_feature_rows)
    return fold_metrics, probabilities, sorted(set(walkforward_test_indices)), feature_stability


def stability_report(
    fold_metrics: pd.DataFrame,
    probabilities: dict[str, list[np.ndarray]],
    feature_stability: pd.DataFrame,
    dirs: dict[str, Path],
) -> pd.DataFrame:
    ok = fold_metrics[fold_metrics["status"] == "ok"].copy()
    metric_cols = [
        "auc",
        "average_precision",
        "brier",
        "ece",
        "threshold",
        "precision",
        "recall",
        "expectancy",
        "profit_factor",
        "max_drawdown",
        "trade_count",
        "prob_mean",
    ]
    rows: list[dict[str, object]] = []
    for target, group in ok.groupby("target"):
        row: dict[str, object] = {"target": target, "folds": int(len(group))}
        for col in metric_cols:
            values = pd.to_numeric(group[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if values.empty:
                continue
            row[f"{col}_mean"] = float(values.mean())
            row[f"{col}_std"] = float(values.std(ddof=0))
            row[f"{col}_min"] = float(values.min())
            row[f"{col}_max"] = float(values.max())
        prob_drift = []
        target_probs = probabilities.get(target, [])
        for prev, cur in zip(target_probs[:-1], target_probs[1:]):
            prob_drift.append(psi(prev, cur))
        if prob_drift:
            row["probability_psi_mean_fold_to_fold"] = float(np.nanmean(prob_drift))
            row["probability_psi_max_fold_to_fold"] = float(np.nanmax(prob_drift))
        rows.append(row)

    stability = pd.DataFrame(rows)
    stability.to_csv(dirs["walkforward"] / "stability_by_target.csv", index=False)
    write_json(dirs["walkforward"] / "stability_report.json", rows)

    summary = {
        "targets": rows,
        "feature_stability": {
            "folds": int(len(feature_stability)),
            "feature_psi_mean": float(feature_stability["feature_psi_mean"].mean()),
            "feature_psi_max": float(feature_stability["feature_psi_max"].max()),
            "feature_psi_p95_mean": float(feature_stability["feature_psi_p95"].mean()),
        },
    }
    write_json(dirs["validation"] / "phase3_stability_summary.json", summary)
    return stability


def run_model_comparison(
    df: pd.DataFrame,
    config: PipelineConfig,
    dirs: dict[str, Path],
    comparison_max_rows: int,
    model_names: set[str] | None = None,
    merge_existing: bool = False,
) -> pd.DataFrame:
    comparison_df = df.iloc[-comparison_max_rows:].reset_index(drop=True) if comparison_max_rows else df
    train, cal, test = time_split(comparison_df)
    x_train = train[FEATURE_COLUMNS]
    x_cal = cal[FEATURE_COLUMNS]
    x_test = test[FEATURE_COLUMNS]
    availability = []
    rows: list[dict[str, object]] = []

    for spec in classifier_candidates(config.validation.random_seed):
        if model_names is not None and spec.name not in model_names:
            continue
        availability.append(
            {
                "model": spec.name,
                "available": bool(spec.available and spec.builder is not None),
                "reason": spec.reason,
            }
        )
        if not spec.available or spec.builder is None:
            for target_name in BINARY_TARGETS:
                rows.append(
                    {
                        "model": spec.name,
                        "target": target_name,
                        "status": "unavailable",
                        "reason": spec.reason,
                    }
                )
            continue
        for target_name, target_info in BINARY_TARGETS.items():
            target_col = target_info["target"]
            value_col = target_info["value"]
            try:
                if train[target_col].nunique() < 2 or cal[target_col].nunique() < 2:
                    raise ValueError("single-class train or calibration split")
                model = fit_calibrated_classifier(
                    spec,
                    x_train,
                    train[target_col],
                    x_cal,
                    cal[target_col],
                )
                cal_prob = model.predict_proba(x_cal)[:, 1]
                test_prob = model.predict_proba(x_test)[:, 1]
                metrics = classification_metrics(test[target_col].to_numpy(), test_prob)
                threshold_report = threshold_recommendation_report(
                    cal[target_col].to_numpy(),
                    cal_prob,
                    min_samples=max(100, int(len(cal) * 0.005)),
                    value=cal[value_col].to_numpy(),
                )
                recommended = threshold_report.get("recommended") or {}
                gate = threshold_metrics(
                    test[target_col].to_numpy(),
                    test_prob,
                    recommended.get("threshold"),
                    test[value_col].to_numpy(),
                )
                rows.append(
                    {
                        "model": spec.name,
                        "target": target_name,
                        "status": "ok",
                        "rows": int(len(comparison_df)),
                        "train_rows": int(len(train)),
                        "calibration_rows": int(len(cal)),
                        "test_rows": int(len(test)),
                        "auc": metrics.auc,
                        "pr_auc": metrics.average_precision,
                        "average_precision": metrics.average_precision,
                        "calibration": "isotonic_time_split",
                        "ece": metrics.expected_calibration_error,
                        "brier": metrics.brier,
                        **gate,
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "model": spec.name,
                        "target": target_name,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )

    if merge_existing:
        existing_rows = read_json(dirs["comparison"] / "model_comparison.json", [])
        existing_availability = read_json(dirs["comparison"] / "candidate_availability.json", [])
        if not isinstance(existing_rows, list):
            existing_rows = []
        if not isinstance(existing_availability, list):
            existing_availability = []
        rows = merge_rows(existing_rows, rows, ("model", "target"))
        availability = merge_rows(existing_availability, availability, ("model",))

    comparison = pd.DataFrame(rows)
    comparison.to_csv(dirs["comparison"] / "model_comparison.csv", index=False)
    write_json(dirs["comparison"] / "model_comparison.json", rows)
    write_json(dirs["comparison"] / "candidate_availability.json", availability)
    return comparison


def run_feature_importance(
    df: pd.DataFrame,
    config: PipelineConfig,
    dirs: dict[str, Path],
    permutation_sample: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    spec = first_available_classifier(config.validation.random_seed)
    train, cal, test = time_split(df)
    x_train = train[FEATURE_COLUMNS]
    x_test = test[FEATURE_COLUMNS]
    native_rows: list[dict[str, object]] = []
    permutation_rows: list[dict[str, object]] = []
    shap_report: dict[str, object] = {
        "status": "skipped",
        "reason": "shap is not installed",
    }

    for target_name in PRIMARY_IMPORTANCE_TARGETS:
        target_col = BINARY_TARGETS[target_name]["target"]
        if train[target_col].nunique() < 2 or test[target_col].nunique() < 2:
            continue
        model = spec.builder()
        model.fit(x_train, train[target_col])
        importances = getattr(model, "feature_importances_", None)
        if importances is not None:
            total = float(np.sum(importances)) or 1.0
            for feature, importance in zip(FEATURE_COLUMNS, importances):
                native_rows.append(
                    {
                        "target": target_name,
                        "model": spec.name,
                        "feature": feature,
                        "importance": float(importance),
                        "importance_fraction": float(importance / total),
                    }
                )

        sample_rows = min(permutation_sample, len(test))
        if sample_rows > 0:
            sample = test.sample(sample_rows, random_state=config.validation.random_seed)
            perm = permutation_importance(
                model,
                sample[FEATURE_COLUMNS],
                sample[target_col],
                scoring="roc_auc",
                n_repeats=3,
                random_state=config.validation.random_seed,
                n_jobs=-1,
            )
            for feature, mean_value, std_value in zip(
                FEATURE_COLUMNS,
                perm.importances_mean,
                perm.importances_std,
            ):
                permutation_rows.append(
                    {
                        "target": target_name,
                        "model": spec.name,
                        "feature": feature,
                        "importance_mean": float(mean_value),
                        "importance_std": float(std_value),
                    }
                )

    try:
        import shap  # type: ignore

        target_name = PRIMARY_IMPORTANCE_TARGETS[0]
        target_col = BINARY_TARGETS[target_name]["target"]
        model = spec.builder()
        model.fit(x_train, train[target_col])
        sample = test.sample(min(1000, len(test)), random_state=config.validation.random_seed)
        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(sample[FEATURE_COLUMNS])
        values = shap_values[1] if isinstance(shap_values, list) and len(shap_values) > 1 else shap_values
        mean_abs = np.abs(values).mean(axis=0)
        shap_report = {
            "status": "ok",
            "target": target_name,
            "model": spec.name,
            "sample_rows": int(len(sample)),
            "top_features": [
                {"feature": feature, "mean_abs_shap": float(score)}
                for feature, score in sorted(
                    zip(FEATURE_COLUMNS, mean_abs),
                    key=lambda item: item[1],
                    reverse=True,
                )[:25]
            ],
        }
    except Exception as exc:
        shap_report = {
            "status": "skipped",
            "reason": str(exc),
        }

    native = pd.DataFrame(native_rows)
    permutation = pd.DataFrame(permutation_rows)
    native.to_csv(dirs["validation"] / "feature_importance_native.csv", index=False)
    permutation.to_csv(dirs["validation"] / "feature_importance_permutation.csv", index=False)
    write_json(dirs["validation"] / "feature_importance_native.json", native_rows)
    write_json(dirs["validation"] / "feature_importance_permutation.json", permutation_rows)
    write_json(dirs["validation"] / "shap_summary.json", shap_report)
    return native, permutation, shap_report


def unwrap_tree_estimator(model: object) -> object | None:
    stack = [model]
    seen: set[int] = set()
    while stack:
        candidate = stack.pop()
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        module = type(candidate).__module__.lower()
        name = type(candidate).__name__.lower()

        def push_attr(attr: str) -> bool:
            if not hasattr(candidate, attr):
                return False
            value = getattr(candidate, attr)
            if value is None or value is candidate:
                return False
            stack.append(value)
            return True

        if "sklearn.frozen" in module or name == "frozenestimator":
            pushed = False
            for attr in (
                "estimator",
                "estimator_",
                "_estimator",
                "base_estimator",
                "base_estimator_",
                "_base_estimator",
            ):
                pushed = push_attr(attr) or pushed
            for value in getattr(candidate, "__dict__", {}).values():
                if value is not None and value is not candidate:
                    stack.append(value)
                    pushed = True
            if pushed:
                continue

        if (
            "lightgbm" in module
            or "xgboost" in module
            or "catboost" in module
            or (hasattr(candidate, "feature_importances_") and not module.startswith("sklearn."))
        ):
            return candidate

        for attr in ("estimator", "base_estimator", "final_estimator_"):
            push_attr(attr)

        if hasattr(candidate, "calibrated_classifiers_"):
            for calibrated in getattr(candidate, "calibrated_classifiers_"):
                for attr in ("estimator", "base_estimator"):
                    if hasattr(calibrated, attr):
                        stack.append(getattr(calibrated, attr))

        if hasattr(candidate, "steps"):
            for _, step in getattr(candidate, "steps"):
                stack.append(step)

    return None


def normalize_shap_values(values: object, feature_count: int) -> np.ndarray:
    if isinstance(values, list):
        values = values[1] if len(values) > 1 else values[0]
    array = np.asarray(values)
    if array.ndim == 3:
        if array.shape[1] == feature_count:
            array = array[:, :, 1 if array.shape[2] > 1 else 0]
        elif array.shape[2] == feature_count:
            array = array[1 if array.shape[0] > 1 else 0, :, :]
    if array.ndim != 2 or array.shape[1] != feature_count:
        raise ValueError(f"unexpected SHAP value shape: {array.shape}")
    return array


def run_shap_from_artifact(
    df: pd.DataFrame,
    config: PipelineConfig,
    dirs: dict[str, Path],
    target_name: str,
    model_name: str,
    sample_rows: int,
) -> dict[str, object]:
    shap_report: dict[str, object]
    artifact_path = config.paths.output_dir / "models" / f"v2_{target_name}_{model_name}.joblib"
    try:
        import shap  # type: ignore

        if not artifact_path.exists():
            raise FileNotFoundError(str(artifact_path))
        saved_model = joblib.load(artifact_path)
        tree_model = unwrap_tree_estimator(saved_model)
        if tree_model is None:
            raise TypeError(f"could not find a tree estimator inside {type(saved_model).__name__}")

        _, _, test = time_split(df)
        sample = test.sample(min(sample_rows, len(test)), random_state=config.validation.random_seed)
        explainer = shap.TreeExplainer(tree_model)
        shap_values = normalize_shap_values(
            explainer.shap_values(sample[FEATURE_COLUMNS]),
            len(FEATURE_COLUMNS),
        )
        mean_abs = np.abs(shap_values).mean(axis=0)
        shap_report = {
            "status": "ok",
            "target": target_name,
            "model": model_name,
            "source": "existing_artifact",
            "artifact_path": str(artifact_path),
            "explained_estimator": f"{type(tree_model).__module__}.{type(tree_model).__name__}",
            "sample_rows": int(len(sample)),
            "top_features": [
                {"feature": feature, "mean_abs_shap": float(score)}
                for feature, score in sorted(
                    zip(FEATURE_COLUMNS, mean_abs),
                    key=lambda item: item[1],
                    reverse=True,
                )[:25]
            ],
        }
    except Exception as exc:
        shap_report = {
            "status": "skipped",
            "source": "existing_artifact",
            "artifact_path": str(artifact_path),
            "reason": str(exc),
        }

    write_json(dirs["validation"] / "shap_summary.json", shap_report)
    return shap_report


def split_frames_for_drift(
    df: pd.DataFrame,
    walkforward_indices: list[int],
) -> dict[str, pd.DataFrame]:
    train, cal, test = time_split(df)
    latest_rows = len(test)
    latest = df.iloc[-latest_rows:].copy()
    walkforward = df.iloc[walkforward_indices].copy() if walkforward_indices else test.copy()
    return {
        "train": train,
        "calibration": cal,
        "holdout": test,
        "walkforward": walkforward,
        "latest": latest,
    }


def run_drift_detection(
    df: pd.DataFrame,
    walkforward_indices: list[int],
    probabilities: dict[str, list[np.ndarray]],
    dirs: dict[str, Path],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    frames = split_frames_for_drift(df, walkforward_indices)
    reference = frames["train"]
    feature_rows: list[dict[str, object]] = []
    target_rows: list[dict[str, object]] = []
    probability_rows: list[dict[str, object]] = []

    for split_name, frame in frames.items():
        if split_name == "train":
            continue
        for feature in FEATURE_COLUMNS:
            feature_rows.append(
                {
                    "comparison": f"train_vs_{split_name}",
                    "feature": feature,
                    "psi": psi(reference[feature].to_numpy(), frame[feature].to_numpy()),
                    "train_mean": float(reference[feature].mean()),
                    "comparison_mean": float(frame[feature].mean()),
                    "mean_delta": float(frame[feature].mean() - reference[feature].mean()),
                    "train_std": float(reference[feature].std(ddof=0)),
                    "comparison_std": float(frame[feature].std(ddof=0)),
                }
            )
        for target_name, target_info in BINARY_TARGETS.items():
            target_col = target_info["target"]
            target_rows.append(
                {
                    "comparison": f"train_vs_{split_name}",
                    "target": target_name,
                    "psi": binary_distribution_psi(reference[target_col], frame[target_col]),
                    "train_rate": float(reference[target_col].mean()),
                    "comparison_rate": float(frame[target_col].mean()),
                    "rate_delta": float(frame[target_col].mean() - reference[target_col].mean()),
                }
            )

    for target_name, folds in probabilities.items():
        if not folds:
            continue
        reference_prob = folds[0]
        for fold_number, fold_prob in enumerate(folds, start=1):
            probability_rows.append(
                {
                    "target": target_name,
                    "fold": fold_number,
                    "psi_vs_fold_1": psi(reference_prob, fold_prob),
                    **probability_summary(fold_prob),
                }
            )
        for prev_number, (prev, cur) in enumerate(zip(folds[:-1], folds[1:]), start=1):
            probability_rows.append(
                {
                    "target": target_name,
                    "fold": f"{prev_number}_to_{prev_number + 1}",
                    "psi_vs_previous_fold": psi(prev, cur),
                    **probability_summary(cur),
                }
            )

    feature_drift = pd.DataFrame(feature_rows)
    target_drift = pd.DataFrame(target_rows)
    probability_drift = pd.DataFrame(probability_rows)
    feature_drift.to_csv(dirs["drift"] / "feature_drift.csv", index=False)
    target_drift.to_csv(dirs["drift"] / "target_drift.csv", index=False)
    probability_drift.to_csv(dirs["drift"] / "probability_drift.csv", index=False)
    write_json(dirs["drift"] / "feature_drift.json", feature_rows)
    write_json(dirs["drift"] / "target_drift.json", target_rows)
    write_json(dirs["drift"] / "probability_drift.json", probability_rows)
    summary = {
        "feature_drift": {
            "rows": int(len(feature_drift)),
            "max_psi": float(feature_drift["psi"].max()),
            "features_over_0_25": int((feature_drift["psi"] > 0.25).sum()),
        },
        "target_drift": {
            "rows": int(len(target_drift)),
            "max_psi": float(target_drift["psi"].max()),
            "targets_over_0_10": int((target_drift["psi"] > 0.10).sum()),
        },
        "probability_drift": {
            "rows": int(len(probability_drift)),
            "max_psi_vs_fold_1": float(
                pd.to_numeric(probability_drift.get("psi_vs_fold_1"), errors="coerce").max()
            ),
            "max_psi_vs_previous_fold": float(
                pd.to_numeric(probability_drift.get("psi_vs_previous_fold"), errors="coerce").max()
            ),
        },
    }
    write_json(dirs["drift"] / "drift_summary.json", summary)
    return feature_drift, target_drift, probability_drift


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Pipeline V2 Phase 3 validation.")
    parser.add_argument("--dataset", default=str(PipelineConfig().paths.dataset_v3))
    parser.add_argument("--max-rows", type=int, default=0, help="Optional cap for development only.")
    parser.add_argument(
        "--comparison-max-rows",
        type=int,
        default=150_000,
        help="Recent rows used for expensive all-candidate comparison; 0 uses all rows.",
    )
    parser.add_argument(
        "--permutation-sample",
        type=int,
        default=5000,
        help="Rows sampled from holdout for permutation importance.",
    )
    parser.add_argument(
        "--refresh-package-dependent-reports",
        action="store_true",
        help="Regenerate only reports affected by optional packages installed after the full run.",
    )
    parser.add_argument(
        "--package-dependent-model",
        action="append",
        default=None,
        help="Model name to refresh in comparison reports; may be repeated.",
    )
    parser.add_argument(
        "--skip-comparison-refresh",
        action="store_true",
        help="Refresh SHAP only and leave existing comparison reports unchanged.",
    )
    parser.add_argument(
        "--shap-target",
        default=PRIMARY_IMPORTANCE_TARGETS[0],
        choices=PRIMARY_IMPORTANCE_TARGETS,
        help="Target used for package-dependent SHAP refresh.",
    )
    parser.add_argument(
        "--shap-model",
        default="lgbm",
        help="Existing saved model artifact suffix used for SHAP refresh.",
    )
    parser.add_argument(
        "--shap-sample",
        type=int,
        default=1000,
        help="Rows sampled from holdout for SHAP refresh.",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)
    dirs = report_dirs(config)
    dataset_path = resolve_dataset(args.dataset)
    df = prepare_dataset(dataset_path, config, max_rows=args.max_rows)

    if args.refresh_package_dependent_reports:
        model_names = set(args.package_dependent_model or ["xgboost"])
        if args.skip_comparison_refresh:
            comparison_rows = read_json(dirs["comparison"] / "model_comparison.json", [])
            if not isinstance(comparison_rows, list):
                comparison_rows = []
            comparison = pd.DataFrame(comparison_rows)
            model_names = set()
        else:
            comparison = run_model_comparison(
                df,
                config,
                dirs,
                args.comparison_max_rows,
                model_names=model_names,
                merge_existing=True,
            )
        shap_report = run_shap_from_artifact(
            df,
            config,
            dirs,
            args.shap_target,
            args.shap_model,
            args.shap_sample,
        )
        summary_path = dirs["validation"] / "phase3_validation_summary.json"
        phase3_summary = read_json(summary_path, {})
        if not isinstance(phase3_summary, dict):
            phase3_summary = {}
        phase3_summary.setdefault("status", "ok")
        phase3_summary["dataset"] = str(dataset_path)
        phase3_summary["rows"] = int(len(df))
        phase3_summary["comparison"] = {
            "rows": int(len(comparison)),
            "comparison_max_rows": int(args.comparison_max_rows),
            "models": sorted(comparison["model"].dropna().unique().tolist()) if not comparison.empty else [],
        }
        feature_importance = phase3_summary.setdefault("feature_importance", {})
        if isinstance(feature_importance, dict):
            feature_importance["shap_status"] = shap_report.get("status")
            feature_importance["shap_source"] = shap_report.get("source")
        write_json(summary_path, phase3_summary)

        refresh_summary = {
            "status": "ok",
            "mode": "package_dependent_report_refresh",
            "dataset": str(dataset_path),
            "rows": int(len(df)),
            "comparison": phase3_summary["comparison"],
            "refreshed_models": sorted(model_names),
            "shap_status": shap_report.get("status"),
            "reports": {
                "validation": str(dirs["validation"]),
                "comparison": str(dirs["comparison"]),
            },
        }
        write_json(dirs["validation"] / "phase3_package_dependent_refresh_summary.json", refresh_summary)
        print(json.dumps(refresh_summary, indent=2, default=json_default))
        return 0

    fold_metrics, probabilities, walkforward_indices, feature_stability = run_walkforward(df, config, dirs)
    stability = stability_report(fold_metrics, probabilities, feature_stability, dirs)
    comparison = run_model_comparison(df, config, dirs, args.comparison_max_rows)
    native, permutation, shap_report = run_feature_importance(df, config, dirs, args.permutation_sample)
    feature_drift, target_drift, probability_drift = run_drift_detection(
        df,
        walkforward_indices,
        probabilities,
        dirs,
    )

    summary = {
        "status": "ok",
        "dataset": str(dataset_path),
        "rows": int(len(df)),
        "regime_volatility_threshold": float(df.attrs.get("regime_volatility_threshold")),
        "walkforward": {
            "fold_metric_rows": int(len(fold_metrics)),
            "targets": sorted(fold_metrics["target"].dropna().unique().tolist()),
            "all_leakage_checks_passed": bool(
                fold_metrics["passed"].dropna().astype(bool).all()
            ),
        },
        "stability": {
            "rows": int(len(stability)),
        },
        "comparison": {
            "rows": int(len(comparison)),
            "comparison_max_rows": int(args.comparison_max_rows),
            "models": sorted(comparison["model"].dropna().unique().tolist()) if not comparison.empty else [],
        },
        "feature_importance": {
            "native_rows": int(len(native)),
            "permutation_rows": int(len(permutation)),
            "shap_status": shap_report.get("status"),
        },
        "drift": {
            "feature_rows": int(len(feature_drift)),
            "target_rows": int(len(target_drift)),
            "probability_rows": int(len(probability_drift)),
        },
        "reports": {
            "validation": str(dirs["validation"]),
            "walkforward": str(dirs["walkforward"]),
            "comparison": str(dirs["comparison"]),
            "drift": str(dirs["drift"]),
        },
    }
    write_json(dirs["validation"] / "phase3_validation_summary.json", summary)
    print(json.dumps(summary, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
