from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
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
from ml_pipeline_v2.models import classifier_candidates, regressor_candidates  # noqa: E402
from ml_pipeline_v2.validation import classification_metrics  # noqa: E402


def add_v2_labels(df: pd.DataFrame, lookahead: int, target_points: float, adverse_mult: float) -> pd.DataFrame:
    if len(df) <= lookahead:
        raise ValueError("not enough rows to build V2 labels")
    close = df["close"].astype(float).to_numpy()
    future = []
    for step in range(1, lookahead + 1):
        shifted = np.empty_like(close, dtype=float)
        shifted[:-step] = close[step:]
        shifted[-step:] = np.nan
        future.append(shifted)
    fwd = np.vstack(future)
    future_max = np.nanmax(fwd, axis=0)
    future_min = np.nanmin(fwd, axis=0)
    max_up = future_max - close
    max_down = close - future_min

    valid = slice(0, len(df) - lookahead)
    df = df.iloc[valid].copy()
    max_up = max_up[valid]
    max_down = max_down[valid]
    df["v2_label_ce_direction"] = (
        (max_up >= target_points) & (max_down < target_points * adverse_mult)
    ).astype(int)
    df["v2_label_pe_direction"] = (
        (max_down >= target_points) & (max_up < target_points * adverse_mult)
    ).astype(int)
    df["v2_ce_mfe_points"] = max_up
    df["v2_pe_mfe_points"] = max_down
    df["v2_ce_mae_points"] = max_down
    df["v2_pe_mae_points"] = max_up
    df["v2_quality_net_ce_rs"] = (max_up * 0.50 - max_down * 0.25) * 30.0 - 132.0
    df["v2_quality_net_pe_rs"] = (max_down * 0.50 - max_up * 0.25) * 30.0 - 132.0
    return df.dropna(subset=["v2_ce_mfe_points", "v2_pe_mfe_points"])


def time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * 0.70)
    cal_end = int(n * 0.85)
    return df.iloc[:train_end], df.iloc[train_end:cal_end], df.iloc[cal_end:]


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

    df = pd.read_csv(args.dataset)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if args.max_rows and len(df) > args.max_rows:
        df = df.iloc[-args.max_rows:].reset_index(drop=True)
    for col in FEATURE_COLUMNS + ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=FEATURE_COLUMNS + ["close"])

    df = add_v2_labels(
        df,
        lookahead=config.labels.directional_lookahead_bars,
        target_points=config.labels.directional_target_points,
        adverse_mult=config.labels.directional_max_adverse_multiple,
    )
    train, cal, test = time_split(df)

    cls_spec = first_available(classifier_candidates(config.validation.random_seed))
    reg_spec = first_available(regressor_candidates(config.validation.random_seed))
    manifest = {
        "dataset": str(args.dataset),
        "rows": int(len(df)),
        "classifier": cls_spec.name,
        "regressor": reg_spec.name,
        "stages": {},
    }

    x_train = train[FEATURE_COLUMNS]
    x_cal = cal[FEATURE_COLUMNS]
    x_test = test[FEATURE_COLUMNS]

    for side in ("ce", "pe"):
        label_col = f"v2_label_{side}_direction"
        model = fit_calibrated_classifier(cls_spec, x_train, train[label_col], x_cal, cal[label_col])
        prob = model.predict_proba(x_test)[:, 1]
        metrics = classification_metrics(test[label_col].to_numpy(), prob)
        manifest["stages"][f"directional_{side}"] = metrics.__dict__
        if not args.dry_run:
            path = config.paths.output_dir / "models" / f"v2_directional_{side}_{cls_spec.name}.joblib"
            joblib.dump(model, path)
            manifest["stages"][f"directional_{side}"]["artifact"] = str(path)

        q_col = f"v2_quality_net_{side}_rs"
        reg = reg_spec.builder()
        reg.fit(x_train, train[q_col])
        pred = reg.predict(x_test)
        q_metrics = {
            "mae": float(np.mean(np.abs(pred - test[q_col].to_numpy()))),
            "prediction_mean": float(np.mean(pred)),
            "target_mean": float(test[q_col].mean()),
        }
        manifest["stages"][f"quality_{side}"] = q_metrics
        if not args.dry_run:
            path = config.paths.output_dir / "models" / f"v2_quality_{side}_{reg_spec.name}.joblib"
            joblib.dump(reg, path)
            manifest["stages"][f"quality_{side}"]["artifact"] = str(path)

    if not args.dry_run:
        manifest_path = config.paths.output_dir / "models" / "v2_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Wrote {manifest_path}")
    else:
        print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
