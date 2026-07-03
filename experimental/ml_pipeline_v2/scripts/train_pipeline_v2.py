from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV
try:
    from sklearn.frozen import FrozenEstimator
except Exception:  # pragma: no cover - older sklearn fallback
    FrozenEstimator = None

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "experimental" / "ml_pipeline_v2" / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig, ensure_output_dirs  # noqa: E402
from ml_pipeline_v2.models import (  # noqa: E402
    classifier_candidates,
    regime_classifier_candidates,
    regressor_candidates,
)
from ml_pipeline_v2.validation import (  # noqa: E402
    classification_metrics,
    multiclass_classification_metrics,
    regression_metrics,
    threshold_recommendation_report,
)


def first_hit_bars(delta: np.ndarray, threshold: float, fallback_bars: int) -> np.ndarray:
    hit = delta >= threshold
    first = np.argmax(hit, axis=0) + 1
    return np.where(hit.any(axis=0), first, fallback_bars).astype(float)


def add_v2_labels(
    df: pd.DataFrame,
    directional_lookahead: int,
    quality_lookahead: int,
    target_points: float,
    adverse_mult: float,
    spread_points_round_trip: float,
    lot_units: int,
    brokerage_round_trip_rs: float,
) -> pd.DataFrame:
    lookahead = max(directional_lookahead, quality_lookahead)
    if len(df) <= lookahead:
        raise ValueError("not enough rows to build V2 labels")
    close = df["close"].astype(float).to_numpy()
    valid_len = len(df) - lookahead
    future = []
    for step in range(1, lookahead + 1):
        future.append(close[step : step + valid_len])
    fwd = np.vstack(future)
    directional_fwd = fwd[:directional_lookahead]
    quality_fwd = fwd[:quality_lookahead]
    directional_max = np.nanmax(directional_fwd, axis=0)
    directional_min = np.nanmin(directional_fwd, axis=0)
    quality_max = np.nanmax(quality_fwd, axis=0)
    quality_min = np.nanmin(quality_fwd, axis=0)
    quality_argmax = np.nanargmax(quality_fwd, axis=0) + 1
    quality_argmin = np.nanargmin(quality_fwd, axis=0) + 1
    current_close = close[:valid_len]
    directional_up = np.maximum(directional_max - current_close, 0.0)
    directional_down = np.maximum(current_close - directional_min, 0.0)
    max_up = np.maximum(quality_max - current_close, 0.0)
    max_down = np.maximum(current_close - quality_min, 0.0)
    ce_favorable_path = np.maximum(quality_fwd - current_close, 0.0)
    pe_favorable_path = np.maximum(current_close - quality_fwd, 0.0)
    ce_holding_bars = first_hit_bars(ce_favorable_path, target_points, quality_lookahead + 1)
    pe_holding_bars = first_hit_bars(pe_favorable_path, target_points, quality_lookahead + 1)
    ce_net_points = max_up * 0.50 - max_down * 0.25 - spread_points_round_trip
    pe_net_points = max_down * 0.50 - max_up * 0.25 - spread_points_round_trip

    df = df.iloc[:valid_len].copy()
    df["v2_label_ce_direction"] = (
        (directional_up >= target_points) & (directional_down < target_points * adverse_mult)
    ).astype(int)
    df["v2_label_pe_direction"] = (
        (directional_down >= target_points) & (directional_up < target_points * adverse_mult)
    ).astype(int)
    df["v2_ce_mfe_points"] = max_up
    df["v2_pe_mfe_points"] = max_down
    df["v2_ce_mae_points"] = max_down
    df["v2_pe_mae_points"] = max_up
    df["v2_ce_net_points"] = ce_net_points
    df["v2_pe_net_points"] = pe_net_points
    df["v2_quality_net_ce_rs"] = ce_net_points * lot_units - brokerage_round_trip_rs
    df["v2_quality_net_pe_rs"] = pe_net_points * lot_units - brokerage_round_trip_rs
    df["v2_quality_ce_profitable"] = (df["v2_quality_net_ce_rs"] > 0.0).astype(int)
    df["v2_quality_pe_profitable"] = (df["v2_quality_net_pe_rs"] > 0.0).astype(int)
    df["v2_ce_target_hit"] = (max_up >= target_points).astype(int)
    df["v2_pe_target_hit"] = (max_down >= target_points).astype(int)
    df["v2_ce_stop_hit"] = (max_down >= target_points * adverse_mult).astype(int)
    df["v2_pe_stop_hit"] = (max_up >= target_points * adverse_mult).astype(int)
    df["v2_ce_reward_risk"] = np.clip(
        max_up / np.maximum(max_down + spread_points_round_trip, 1e-6), 0.0, 20.0
    )
    df["v2_pe_reward_risk"] = np.clip(
        max_down / np.maximum(max_up + spread_points_round_trip, 1e-6), 0.0, 20.0
    )
    df["v2_ce_drawdown_rs"] = max_down * lot_units
    df["v2_pe_drawdown_rs"] = max_up * lot_units
    df["v2_ce_bars_to_target"] = ce_holding_bars
    df["v2_pe_bars_to_target"] = pe_holding_bars
    df["v2_ce_bars_to_mfe"] = quality_argmax.astype(float)
    df["v2_pe_bars_to_mfe"] = quality_argmin.astype(float)
    return df.dropna(subset=["v2_ce_mfe_points", "v2_pe_mfe_points"])


def add_regime_proxy_labels(df: pd.DataFrame, volatility_threshold: float) -> pd.DataFrame:
    df = df.copy()
    conditions = [
        (df["volatility"] >= volatility_threshold) | (df["adx"] >= 35),
        (df["adx"] >= 25) & (df["di_spread"].abs() >= 10),
        df["adx"] < 18,
    ]
    choices = ["volatile_trend", "trend", "range"]
    df["v2_regime_proxy"] = np.select(conditions, choices, default="mixed")
    return df


def time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * 0.70)
    cal_end = int(n * 0.85)
    return df.iloc[:train_end].copy(), df.iloc[train_end:cal_end].copy(), df.iloc[cal_end:].copy()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        return f"unavailable: {exc}"
    if result.returncode != 0:
        return f"unavailable: {result.stderr.strip()}"
    return result.stdout.strip()


def source_file_hashes() -> dict[str, str]:
    files = [
        Path("experimental/ml_pipeline_v2/scripts/train_pipeline_v2.py"),
        Path("experimental/ml_pipeline_v2/src/ml_pipeline_v2/config.py"),
        Path("experimental/ml_pipeline_v2/src/ml_pipeline_v2/models.py"),
        Path("experimental/ml_pipeline_v2/src/ml_pipeline_v2/validation.py"),
    ]
    return {str(path): file_sha256(ROOT / path) for path in files}


def value_counts_fraction(series: pd.Series) -> dict[str, float]:
    counts = series.value_counts(normalize=True, dropna=False).sort_index()
    return {str(key): float(value) for key, value in counts.items()}


def numeric_summary(series: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {"count": 0}
    return {
        "count": int(len(values)),
        "mean": float(values.mean()),
        "std": float(values.std(ddof=0)),
        "p05": float(values.quantile(0.05)),
        "p50": float(values.quantile(0.50)),
        "p95": float(values.quantile(0.95)),
    }


def config_manifest(config: PipelineConfig) -> dict[str, object]:
    return {
        "labels": {
            "directional_lookahead_bars": config.labels.directional_lookahead_bars,
            "directional_target_points": config.labels.directional_target_points,
            "directional_max_adverse_multiple": config.labels.directional_max_adverse_multiple,
            "quality_lookahead_bars": config.labels.quality_lookahead_bars,
            "spread_points_round_trip": config.labels.spread_points_round_trip,
            "lot_units": config.labels.lot_units,
            "brokerage_round_trip_rs": config.labels.brokerage_round_trip_rs,
        },
        "validation": {
            "min_train_rows": config.validation.min_train_rows,
            "calibration_fraction": config.validation.calibration_fraction,
            "test_fraction": config.validation.test_fraction,
            "walkforward_folds": config.validation.walkforward_folds,
            "purge_bars": config.validation.purge_bars,
            "embargo_bars": config.validation.embargo_bars,
            "random_seed": config.validation.random_seed,
        },
        "risk": {
            "max_risk_per_trade_pct": config.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct": config.risk.max_daily_loss_pct,
            "max_open_exposure_pct": config.risk.max_open_exposure_pct,
            "min_expected_net_pnl_rs": config.risk.min_expected_net_pnl_rs,
            "max_risk_of_ruin_pct": config.risk.max_risk_of_ruin_pct,
        },
    }


def build_metadata(
    dataset_path: Path,
    df: pd.DataFrame,
    train: pd.DataFrame,
    cal: pd.DataFrame,
    test: pd.DataFrame,
    config: PipelineConfig,
) -> dict[str, object]:
    return {
        "created_by": "experimental.ml_pipeline_v2.train_pipeline_v2",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pandas_version": pd.__version__,
        "numpy_version": np.__version__,
        "sklearn_version": sklearn.__version__,
        "joblib_version": joblib.__version__,
        "created_at_utc": pd.Timestamp.utcnow().isoformat(),
        "git": {
            "commit": git_output("rev-parse", "HEAD"),
            "branch": git_output("branch", "--show-current"),
            "status_experimental": git_output("status", "--short", "--", "experimental/ml_pipeline_v2"),
            "status_session": git_output("status", "--short", "--", "SESSION.md"),
        },
        "source_file_hashes": source_file_hashes(),
        "dataset": {
            "path": str(dataset_path),
            "sha256": file_sha256(dataset_path),
            "rows_after_labeling": int(len(df)),
            "date_min": df["date"].min().isoformat(),
            "date_max": df["date"].max().isoformat(),
        },
        "split": {
            "method": "chronological_70_15_15_train_calibration_test",
            "train_rows": int(len(train)),
            "calibration_rows": int(len(cal)),
            "test_rows": int(len(test)),
            "train_date_min": train["date"].min().isoformat(),
            "train_date_max": train["date"].max().isoformat(),
            "calibration_date_min": cal["date"].min().isoformat(),
            "calibration_date_max": cal["date"].max().isoformat(),
            "test_date_min": test["date"].min().isoformat(),
            "test_date_max": test["date"].max().isoformat(),
        },
        "feature_columns": list(FEATURE_COLUMNS),
        "config": config_manifest(config),
    }


def target_manifest(df: pd.DataFrame) -> dict[str, object]:
    targets: dict[str, object] = {
        "regime_proxy": value_counts_fraction(df["v2_regime_proxy"]),
    }
    for side in ("ce", "pe"):
        targets[f"directional_{side}"] = value_counts_fraction(df[f"v2_label_{side}_direction"])
        targets[f"quality_{side}_profitable"] = value_counts_fraction(
            df[f"v2_quality_{side}_profitable"]
        )
        targets[f"{side}_target_hit"] = value_counts_fraction(df[f"v2_{side}_target_hit"])
        targets[f"{side}_stop_hit"] = value_counts_fraction(df[f"v2_{side}_stop_hit"])
        targets[f"quality_net_{side}_rs"] = numeric_summary(df[f"v2_quality_net_{side}_rs"])
        targets[f"{side}_reward_risk"] = numeric_summary(df[f"v2_{side}_reward_risk"])
        targets[f"{side}_drawdown_rs"] = numeric_summary(df[f"v2_{side}_drawdown_rs"])
        targets[f"{side}_bars_to_target"] = numeric_summary(df[f"v2_{side}_bars_to_target"])
        targets[f"{side}_bars_to_mfe"] = numeric_summary(df[f"v2_{side}_bars_to_mfe"])
    return targets


def first_available(specs):
    for spec in specs:
        if spec.available and spec.builder is not None:
            return spec
    raise RuntimeError("no available model candidate")


def fit_calibrated_classifier(spec, x_train, y_train, x_cal, y_cal):
    base = spec.builder()
    base.fit(x_train, y_train)
    if FrozenEstimator is not None:
        calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    else:
        calibrated = CalibratedClassifierCV(base, cv="prefit", method="isotonic")
    calibrated.fit(x_cal, y_cal)
    return calibrated


def main() -> int:
    parser = argparse.ArgumentParser(description="Train isolated Pipeline V2 candidate models.")
    parser.add_argument("--dataset", default=str(PipelineConfig().paths.dataset_v3))
    parser.add_argument("--dry-run", action="store_true", help="Run labels and metrics without writing models.")
    parser.add_argument("--max-rows", type=int, default=0, help="Optional cap for quick experiments.")
    args = parser.parse_args()

    config = PipelineConfig()
    ensure_output_dirs(config)

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path

    df = pd.read_csv(dataset_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if args.max_rows and len(df) > args.max_rows:
        df = df.iloc[-args.max_rows:].reset_index(drop=True)
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
    df = pd.concat([train, cal, test], axis=0).sort_index()

    cls_spec = first_available(classifier_candidates(config.validation.random_seed))
    regime_spec = first_available(regime_classifier_candidates(config.validation.random_seed))
    reg_spec = first_available(regressor_candidates(config.validation.random_seed))
    manifest = {
        "dataset": str(dataset_path),
        "rows": int(len(df)),
        "classifier": cls_spec.name,
        "regime_classifier": regime_spec.name,
        "regressor": reg_spec.name,
        "metadata": build_metadata(dataset_path, df, train, cal, test, config),
        "targets": target_manifest(df),
        "threshold_reports": {},
        "stages": {},
    }

    x_train = train[FEATURE_COLUMNS]
    x_cal = cal[FEATURE_COLUMNS]
    x_test = test[FEATURE_COLUMNS]

    regime_model = fit_calibrated_classifier(
        regime_spec,
        x_train,
        train["v2_regime_proxy"],
        x_cal,
        cal["v2_regime_proxy"],
    )
    regime_prob = regime_model.predict_proba(x_test)
    regime_metrics = multiclass_classification_metrics(
        test["v2_regime_proxy"].to_numpy(),
        regime_prob,
        regime_model.classes_,
    )
    manifest["stages"]["regime_proxy"] = {
        **regime_metrics.__dict__,
        "target": "current_bar_heuristic_proxy",
        "volatility_threshold_source": "train_split_p90",
        "volatility_threshold": regime_volatility_threshold,
        "classes": [str(cls) for cls in regime_model.classes_],
    }
    if not args.dry_run:
        path = config.paths.output_dir / "models" / f"v2_regime_proxy_{regime_spec.name}.joblib"
        joblib.dump(regime_model, path)
        manifest["stages"]["regime_proxy"]["artifact"] = str(path)

    for side in ("ce", "pe"):
        label_col = f"v2_label_{side}_direction"
        model = fit_calibrated_classifier(cls_spec, x_train, train[label_col], x_cal, cal[label_col])
        cal_prob = model.predict_proba(x_cal)[:, 1]
        prob = model.predict_proba(x_test)[:, 1]
        metrics = classification_metrics(test[label_col].to_numpy(), prob)
        threshold_report = threshold_recommendation_report(
            cal[label_col].to_numpy(),
            cal_prob,
            min_samples=max(100, int(len(cal) * 0.005)),
            value=cal[f"v2_quality_net_{side}_rs"].to_numpy(),
        )
        manifest["stages"][f"directional_{side}"] = {
            **metrics.__dict__,
            "target": label_col,
            "calibration": "isotonic_time_split",
            "threshold_source": "calibration_split",
        }
        manifest["threshold_reports"][f"directional_{side}"] = threshold_report
        if not args.dry_run:
            path = config.paths.output_dir / "models" / f"v2_directional_{side}_{cls_spec.name}.joblib"
            joblib.dump(model, path)
            manifest["stages"][f"directional_{side}"]["artifact"] = str(path)

        binary_targets = {
            "quality_profitable": f"v2_quality_{side}_profitable",
            "target_hit": f"v2_{side}_target_hit",
            "stop_hit": f"v2_{side}_stop_hit",
        }
        for stage_suffix, target_col in binary_targets.items():
            q_model = fit_calibrated_classifier(
                cls_spec,
                x_train,
                train[target_col],
                x_cal,
                cal[target_col],
            )
            q_cal_prob = q_model.predict_proba(x_cal)[:, 1]
            q_prob = q_model.predict_proba(x_test)[:, 1]
            q_metrics = classification_metrics(test[target_col].to_numpy(), q_prob)
            stage_name = f"{stage_suffix}_{side}"
            manifest["stages"][stage_name] = {
                **q_metrics.__dict__,
                "target": target_col,
                "calibration": "isotonic_time_split",
                "threshold_source": "calibration_split",
            }
            manifest["threshold_reports"][stage_name] = threshold_recommendation_report(
                cal[target_col].to_numpy(),
                q_cal_prob,
                min_samples=max(100, int(len(cal) * 0.005)),
                value=cal[f"v2_quality_net_{side}_rs"].to_numpy(),
            )
            if not args.dry_run:
                path = config.paths.output_dir / "models" / f"v2_{stage_name}_{cls_spec.name}.joblib"
                joblib.dump(q_model, path)
                manifest["stages"][stage_name]["artifact"] = str(path)

        regression_targets = {
            "quality_net_rs": f"v2_quality_net_{side}_rs",
            "reward_risk": f"v2_{side}_reward_risk",
            "drawdown_rs": f"v2_{side}_drawdown_rs",
            "bars_to_target": f"v2_{side}_bars_to_target",
        }
        for stage_suffix, target_col in regression_targets.items():
            reg = reg_spec.builder()
            reg.fit(x_train, train[target_col])
            pred = reg.predict(x_test)
            r_metrics = regression_metrics(test[target_col].to_numpy(), pred)
            stage_name = f"{stage_suffix}_{side}"
            manifest["stages"][stage_name] = {
                **r_metrics.__dict__,
                "target": target_col,
            }
            if not args.dry_run:
                path = config.paths.output_dir / "models" / f"v2_{stage_name}_{reg_spec.name}.joblib"
                joblib.dump(reg, path)
                manifest["stages"][stage_name]["artifact"] = str(path)

    if not args.dry_run:
        manifest_path = config.paths.output_dir / "models" / "v2_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        report_path = config.paths.output_dir / "reports" / "phase2_threshold_recommendations.json"
        report_path.write_text(json.dumps(manifest["threshold_reports"], indent=2), encoding="utf-8")
        print(f"Wrote {manifest_path}")
        print(f"Wrote {report_path}")
    else:
        print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
