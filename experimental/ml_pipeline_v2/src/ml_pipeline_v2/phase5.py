from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig
from ml_pipeline_v2.phase4 import json_default, read_json, write_json
from ml_pipeline_v2.validation import trade_metrics


SIDES = ("ce", "pe")
PHASE5_SCHEMA_VERSION = "phase5.profitability_intelligence.v1"


@dataclass(frozen=True)
class Phase5Paths:
    base: Path
    trades: Path
    loss_analysis: Path
    optimizer: Path
    reviews: Path
    strategy_optimizer: Path
    regime_matrix: Path
    recommendations: Path


@dataclass(frozen=True)
class FilterCandidate:
    key: str
    category: str
    description: str
    context: str
    mask: pd.Series


def phase5_paths(config: PipelineConfig) -> Phase5Paths:
    base = config.paths.output_dir / "reports" / "phase5"
    paths = Phase5Paths(
        base=base,
        trades=base / "trades",
        loss_analysis=base / "loss_analysis",
        optimizer=base / "optimizer",
        reviews=base / "trade_reviews",
        strategy_optimizer=base / "strategy_optimizer",
        regime_matrix=base / "regime_matrix",
        recommendations=base / "recommendations",
    )
    for path in (
        paths.base,
        paths.trades,
        paths.loss_analysis,
        paths.optimizer,
        paths.reviews,
        paths.strategy_optimizer,
        paths.regime_matrix,
        paths.recommendations,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def json_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    clean = frame.replace([np.inf, -np.inf], np.nan)
    return clean.where(pd.notna(clean), None).to_dict("records")


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
    required = {
        "v2_quality_net_ce_rs",
        "v2_quality_net_pe_rs",
        "v2_ce_target_hit",
        "v2_pe_target_hit",
        "v2_ce_stop_hit",
        "v2_pe_stop_hit",
        "v2_ce_mfe_points",
        "v2_pe_mfe_points",
        "v2_ce_mae_points",
        "v2_pe_mae_points",
    }
    if required.issubset(df.columns):
        return df.copy()

    lookahead = max(directional_lookahead, quality_lookahead)
    if len(df) <= lookahead:
        raise ValueError("not enough rows to build V2 labels")

    close = df["close"].astype(float).to_numpy()
    valid_len = len(df) - lookahead
    future = [close[step : step + valid_len] for step in range(1, lookahead + 1)]
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

    labeled = df.iloc[:valid_len].copy()
    labeled["v2_label_ce_direction"] = (
        (directional_up >= target_points) & (directional_down < target_points * adverse_mult)
    ).astype(int)
    labeled["v2_label_pe_direction"] = (
        (directional_down >= target_points) & (directional_up < target_points * adverse_mult)
    ).astype(int)
    labeled["v2_ce_mfe_points"] = max_up
    labeled["v2_pe_mfe_points"] = max_down
    labeled["v2_ce_mae_points"] = max_down
    labeled["v2_pe_mae_points"] = max_up
    labeled["v2_ce_net_points"] = ce_net_points
    labeled["v2_pe_net_points"] = pe_net_points
    labeled["v2_quality_net_ce_rs"] = ce_net_points * lot_units - brokerage_round_trip_rs
    labeled["v2_quality_net_pe_rs"] = pe_net_points * lot_units - brokerage_round_trip_rs
    labeled["v2_quality_ce_profitable"] = (labeled["v2_quality_net_ce_rs"] > 0.0).astype(int)
    labeled["v2_quality_pe_profitable"] = (labeled["v2_quality_net_pe_rs"] > 0.0).astype(int)
    labeled["v2_ce_target_hit"] = (max_up >= target_points).astype(int)
    labeled["v2_pe_target_hit"] = (max_down >= target_points).astype(int)
    labeled["v2_ce_stop_hit"] = (max_down >= target_points * adverse_mult).astype(int)
    labeled["v2_pe_stop_hit"] = (max_up >= target_points * adverse_mult).astype(int)
    labeled["v2_ce_reward_risk"] = np.clip(
        max_up / np.maximum(max_down + spread_points_round_trip, 1e-6), 0.0, 20.0
    )
    labeled["v2_pe_reward_risk"] = np.clip(
        max_down / np.maximum(max_up + spread_points_round_trip, 1e-6), 0.0, 20.0
    )
    labeled["v2_ce_drawdown_rs"] = max_down * lot_units
    labeled["v2_pe_drawdown_rs"] = max_up * lot_units
    labeled["v2_ce_bars_to_target"] = ce_holding_bars
    labeled["v2_pe_bars_to_target"] = pe_holding_bars
    labeled["v2_ce_bars_to_mfe"] = quality_argmax.astype(float)
    labeled["v2_pe_bars_to_mfe"] = quality_argmin.astype(float)
    return labeled.dropna(subset=["v2_ce_mfe_points", "v2_pe_mfe_points"])


def add_regime_proxy_labels(df: pd.DataFrame, volatility_threshold: float) -> pd.DataFrame:
    if "v2_regime_proxy" in df.columns:
        return df.copy()
    labeled = df.copy()
    conditions = [
        (labeled["volatility"] >= volatility_threshold) | (labeled["adx"] >= 35),
        (labeled["adx"] >= 25) & (labeled["di_spread"].abs() >= 10),
        labeled["adx"] < 18,
    ]
    choices = ["volatile_trend", "trend", "range"]
    labeled["v2_regime_proxy"] = np.select(conditions, choices, default="mixed")
    return labeled


def time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    train_end = int(len(df) * 0.70)
    cal_end = int(len(df) * 0.85)
    return df.iloc[:train_end].copy(), df.iloc[train_end:cal_end].copy(), df.iloc[cal_end:].copy()


def resolve_dataset_path(config: PipelineConfig) -> Path:
    manifest = read_json(config.paths.output_dir / "models" / "v2_manifest.json", {})
    if isinstance(manifest, dict):
        path_text = manifest.get("dataset") or (
            (manifest.get("metadata") or {}).get("dataset", {}).get("path")
            if isinstance(manifest.get("metadata"), dict)
            else None
        )
        if path_text:
            path = Path(str(path_text))
            if path.exists():
                return path
    if config.paths.dataset_v3.exists():
        return config.paths.dataset_v3
    return config.paths.dataset_v2


def load_labeled_dataset(
    config: PipelineConfig,
    split: str = "test",
    max_rows: int = 0,
) -> tuple[pd.DataFrame, dict[str, object]]:
    dataset_path = resolve_dataset_path(config)
    df = pd.read_csv(dataset_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if max_rows and len(df) > max_rows:
        df = df.iloc[-max_rows:].reset_index(drop=True)

    numeric_columns = sorted(set(FEATURE_COLUMNS + ["close", "open", "high", "low"]))
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    required = [column for column in FEATURE_COLUMNS + ["close"] if column in df.columns]
    df = df.dropna(subset=required)

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
    volatility_threshold = float(train["volatility"].quantile(0.90))
    train = add_regime_proxy_labels(train, volatility_threshold)
    cal = add_regime_proxy_labels(cal, volatility_threshold)
    test = add_regime_proxy_labels(test, volatility_threshold)
    selected = {"train": train, "calibration": cal, "test": test, "all": pd.concat([train, cal, test])}
    if split not in selected:
        raise ValueError(f"unsupported split: {split}")

    metadata = {
        "dataset": str(dataset_path),
        "split": split,
        "max_rows": int(max_rows),
        "rows_after_labeling": int(len(df)),
        "selected_rows": int(len(selected[split])),
        "date_min": selected[split]["date"].min().isoformat(),
        "date_max": selected[split]["date"].max().isoformat(),
        "regime_volatility_threshold_source": "train_split_p90",
        "regime_volatility_threshold": volatility_threshold,
    }
    return selected[split].sort_values("date").reset_index(drop=True), metadata


def load_phase4_champions(config: PipelineConfig) -> dict[str, dict[str, object]]:
    path = config.paths.output_dir / "reports" / "phase4" / "recommendations" / "champions.csv"
    if not path.exists():
        return {}
    champions = pd.read_csv(path)
    by_target: dict[str, dict[str, object]] = {}
    for row in champions.to_dict("records"):
        by_target[str(row.get("target"))] = row
    return by_target


def manifest_stage_artifacts(config: PipelineConfig) -> dict[str, str]:
    manifest = read_json(config.paths.output_dir / "models" / "v2_manifest.json", {})
    if not isinstance(manifest, dict):
        return {}
    stages = manifest.get("stages") or {}
    if not isinstance(stages, dict):
        return {}
    artifacts = {}
    for stage, payload in stages.items():
        if isinstance(payload, dict) and payload.get("artifact"):
            artifacts[str(stage)] = str(payload["artifact"])
    return artifacts


def artifact_candidates_for_target(config: PipelineConfig, target: str) -> list[Path]:
    candidates: list[Path] = []
    champion = load_phase4_champions(config).get(target, {})
    if champion.get("model_artifact_path"):
        candidates.append(Path(str(champion["model_artifact_path"])))
    stage_artifacts = manifest_stage_artifacts(config)
    if stage_artifacts.get(target):
        candidates.append(Path(stage_artifacts[target]))
    unique: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen and path.exists():
            unique.append(path)
            seen.add(key)
    return unique


def load_model(path: Path) -> object:
    import joblib

    return joblib.load(path)


def binary_probability(model: object, features: pd.DataFrame) -> np.ndarray:
    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(features)
        if probabilities.ndim == 1:
            return np.asarray(probabilities, dtype=float)
        classes = getattr(model, "classes_", None)
        if classes is not None and 1 in set(classes):
            class_index = list(classes).index(1)
        else:
            class_index = probabilities.shape[1] - 1
        return np.asarray(probabilities[:, class_index], dtype=float)
    if hasattr(model, "predict"):
        return np.asarray(model.predict(features), dtype=float)
    raise TypeError(f"model at runtime does not expose predict or predict_proba: {type(model)!r}")


def add_model_scores(
    config: PipelineConfig,
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    scored = df.copy()
    warnings: list[dict[str, object]] = []
    features = scored[FEATURE_COLUMNS]
    targets = [
        "directional_ce",
        "directional_pe",
        "quality_profitable_ce",
        "quality_profitable_pe",
        "target_hit_ce",
        "target_hit_pe",
        "stop_hit_ce",
        "stop_hit_pe",
    ]

    for target in targets:
        score_col = f"score_{target}"
        scored[score_col] = np.nan
        for path in artifact_candidates_for_target(config, target):
            try:
                model = load_model(path)
                scored[score_col] = binary_probability(model, features)
                warnings.append(
                    {
                        "target": target,
                        "artifact": str(path),
                        "status": "loaded",
                    }
                )
                break
            except Exception as exc:
                warnings.append(
                    {
                        "target": target,
                        "artifact": str(path),
                        "status": "load_failed",
                        "error": str(exc),
                    }
                )

    regime_candidates = artifact_candidates_for_target(config, "regime_proxy")
    scored["score_regime_predicted"] = scored.get("v2_regime_proxy", "unknown")
    for path in regime_candidates:
        try:
            model = load_model(path)
            scored["score_regime_predicted"] = model.predict(features)
            warnings.append({"target": "regime_proxy", "artifact": str(path), "status": "loaded"})
            break
        except Exception as exc:
            warnings.append(
                {
                    "target": "regime_proxy",
                    "artifact": str(path),
                    "status": "load_failed",
                    "error": str(exc),
                }
            )
    return scored, warnings


def bucket_by_quantiles(
    series: pd.Series,
    low_label: str,
    mid_label: str,
    high_label: str,
) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if values.notna().sum() < 3:
        return pd.Series(mid_label, index=series.index)
    low = values.quantile(0.25)
    high = values.quantile(0.75)
    return pd.Series(
        np.select([values <= low, values >= high], [low_label, high_label], default=mid_label),
        index=series.index,
    )


def time_of_day_bucket(frame: pd.DataFrame) -> pd.Series:
    mins_since_open = pd.to_numeric(frame.get("mins_since_open"), errors="coerce")
    mins_to_close = pd.to_numeric(frame.get("mins_to_close"), errors="coerce")
    hour = pd.to_numeric(frame["date"].dt.hour, errors="coerce")
    conditions = [
        mins_since_open <= 30,
        (mins_since_open > 30) & (mins_since_open <= 120),
        (mins_since_open > 120) & (mins_since_open <= 240),
        (mins_since_open > 240) & (mins_to_close > 45),
        mins_to_close <= 45,
        hour >= 14,
    ]
    choices = ["open_30m", "morning", "midday", "afternoon", "closing_45m", "after_2pm"]
    return pd.Series(np.select(conditions, choices, default="unknown"), index=frame.index)


def trend_bucket(frame: pd.DataFrame) -> pd.Series:
    adx = pd.to_numeric(frame["adx"], errors="coerce")
    return pd.Series(
        np.select([adx < 18, adx >= 25], ["low_adx", "strong_trend"], default="moderate_trend"),
        index=frame.index,
    )


def rsi_bucket(frame: pd.DataFrame) -> pd.Series:
    rsi = pd.to_numeric(frame["rsi"], errors="coerce")
    return pd.Series(
        np.select([rsi < 35, rsi > 65], ["rsi_below_35", "rsi_above_65"], default="rsi_35_65"),
        index=frame.index,
    )


def add_context_columns(frame: pd.DataFrame) -> pd.DataFrame:
    enriched = frame.copy()
    enriched["day_of_week"] = enriched["date"].dt.day_name()
    enriched["time_of_day"] = time_of_day_bucket(enriched)
    enriched["volatility_bucket"] = bucket_by_quantiles(
        enriched["volatility"], "low_volatility", "normal_volatility", "high_volatility"
    )
    enriched["atr_bucket"] = bucket_by_quantiles(enriched["atr"], "low_atr", "normal_atr", "high_atr")
    enriched["trend_strength_bucket"] = trend_bucket(enriched)
    enriched["rsi_bucket"] = rsi_bucket(enriched)
    enriched["after_2pm"] = enriched["date"].dt.hour >= 14
    enriched["opening_gap_proxy"] = (
        (pd.to_numeric(enriched.get("session_open"), errors="coerce").fillna(0) == 1)
        & (
            pd.to_numeric(enriched.get("return_1"), errors="coerce").abs()
            >= pd.to_numeric(enriched.get("return_1"), errors="coerce").abs().quantile(0.90)
        )
    )
    enriched["expiry_proxy"] = enriched["date"].dt.weekday == 3
    return enriched


def confidence_bucket(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.Series(
        np.select(
            [numeric < 0.45, numeric >= 0.60],
            ["low_confidence", "high_confidence"],
            default="medium_confidence",
        ),
        index=values.index,
    )


def build_trade_table(config: PipelineConfig, scored: pd.DataFrame) -> pd.DataFrame:
    base = add_context_columns(scored)
    trade_frames: list[pd.DataFrame] = []
    for side in SIDES:
        eligible_col = f"{side}_eligible"
        if eligible_col in base.columns and base[eligible_col].notna().any():
            eligible = base[eligible_col].astype(str).str.lower().isin({"true", "1", "yes"})
            if not eligible.any():
                eligible = pd.Series(True, index=base.index)
        else:
            eligible = pd.Series(True, index=base.index)

        side_frame = base.loc[eligible].copy()
        side_frame["side"] = side
        side_frame["trade_id"] = side + "_" + side_frame.index.astype(str)
        side_frame["pnl"] = pd.to_numeric(side_frame[f"v2_quality_net_{side}_rs"], errors="coerce")
        side_frame["mfe_points"] = pd.to_numeric(side_frame[f"v2_{side}_mfe_points"], errors="coerce")
        side_frame["mae_points"] = pd.to_numeric(side_frame[f"v2_{side}_mae_points"], errors="coerce")
        side_frame["target_hit"] = pd.to_numeric(side_frame[f"v2_{side}_target_hit"], errors="coerce").fillna(0).astype(int)
        side_frame["stop_hit"] = pd.to_numeric(side_frame[f"v2_{side}_stop_hit"], errors="coerce").fillna(0).astype(int)
        side_frame["target_missed"] = side_frame["target_hit"] == 0
        side_frame["holding_bars"] = pd.to_numeric(side_frame[f"v2_{side}_bars_to_target"], errors="coerce")
        side_frame["bars_to_mfe"] = pd.to_numeric(side_frame[f"v2_{side}_bars_to_mfe"], errors="coerce")
        side_frame["reward_risk"] = pd.to_numeric(side_frame[f"v2_{side}_reward_risk"], errors="coerce")
        side_frame["drawdown_rs"] = pd.to_numeric(side_frame[f"v2_{side}_drawdown_rs"], errors="coerce")
        side_frame["directional_confidence"] = pd.to_numeric(
            side_frame.get(f"score_directional_{side}"), errors="coerce"
        )
        side_frame["quality_confidence"] = pd.to_numeric(
            side_frame.get(f"score_quality_profitable_{side}"), errors="coerce"
        )
        side_frame["target_probability"] = pd.to_numeric(
            side_frame.get(f"score_target_hit_{side}"), errors="coerce"
        )
        side_frame["stop_probability"] = pd.to_numeric(
            side_frame.get(f"score_stop_hit_{side}"), errors="coerce"
        )
        side_frame["confidence"] = side_frame["quality_confidence"]
        fallback_confidence = side_frame["target_probability"] * (1.0 - side_frame["stop_probability"])
        side_frame["confidence"] = side_frame["confidence"].fillna(fallback_confidence)
        side_frame["confidence_bucket"] = confidence_bucket(side_frame["confidence"])
        side_frame["holding_time_bucket"] = pd.Series(
            np.select(
                [
                    side_frame["holding_bars"] <= 3,
                    side_frame["holding_bars"] >= config.labels.quality_lookahead_bars + 1,
                ],
                ["quick_resolution", "target_not_reached_in_window"],
                default="normal_hold",
            ),
            index=side_frame.index,
        )
        side_frame["outcome"] = np.where(side_frame["pnl"] > 0, "win", "loss")
        side_frame["market_regime"] = side_frame.get("score_regime_predicted", side_frame["v2_regime_proxy"])
        trade_frames.append(side_frame)

    trades = pd.concat(trade_frames, axis=0).sort_values(["date", "side"]).reset_index(drop=True)
    return trades.dropna(subset=["pnl"])


def metrics_from_pnl(pnl: pd.Series) -> dict[str, float]:
    return trade_metrics(pd.to_numeric(pnl, errors="coerce").dropna().to_numpy(dtype=float))


def expectancy_lift_stats(selected: pd.Series, baseline: pd.Series) -> dict[str, float]:
    selected_values = pd.to_numeric(selected, errors="coerce").dropna().to_numpy(dtype=float)
    baseline_values = pd.to_numeric(baseline, errors="coerce").dropna().to_numpy(dtype=float)
    if len(selected_values) < 2 or len(baseline_values) < 2:
        return {
            "expectancy_lift": 0.0,
            "expectancy_lift_ci_low": 0.0,
            "expectancy_lift_ci_high": 0.0,
            "p_value_approx": 1.0,
        }
    diff = float(selected_values.mean() - baseline_values.mean())
    se = math.sqrt(
        float(selected_values.var(ddof=1)) / len(selected_values)
        + float(baseline_values.var(ddof=1)) / len(baseline_values)
    )
    if se <= 0 or not math.isfinite(se):
        p_value = 0.0 if diff > 0 else 1.0
        return {
            "expectancy_lift": diff,
            "expectancy_lift_ci_low": diff,
            "expectancy_lift_ci_high": diff,
            "p_value_approx": p_value,
        }
    z_score = diff / se
    p_value = math.erfc(abs(z_score) / math.sqrt(2.0))
    return {
        "expectancy_lift": diff,
        "expectancy_lift_ci_low": float(diff - 1.96 * se),
        "expectancy_lift_ci_high": float(diff + 1.96 * se),
        "p_value_approx": float(p_value),
    }


def baseline_contexts(trades: pd.DataFrame) -> dict[str, pd.Series]:
    contexts = {"all": pd.Series(True, index=trades.index)}
    for side in SIDES:
        contexts[side] = trades["side"] == side
    return contexts


def quantile_thresholds(series: pd.Series, quantiles: tuple[float, ...]) -> list[float]:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return []
    values = sorted({float(numeric.quantile(q)) for q in quantiles})
    return [value for value in values if math.isfinite(value)]


def build_filter_candidates(trades: pd.DataFrame) -> list[FilterCandidate]:
    candidates: list[FilterCandidate] = []
    contexts = baseline_contexts(trades)
    for context, context_mask in contexts.items():
        prefix = f"{context}:"
        context_index = trades.index
        for threshold in quantile_thresholds(trades.loc[context_mask, "confidence"], (0.50, 0.60, 0.70, 0.80)):
            mask = context_mask & (trades["confidence"] >= threshold)
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}min_confidence_{threshold:.4f}",
                    category="minimum_confidence",
                    description=f"{context} require quality confidence >= {threshold:.4f}",
                    context=context,
                    mask=mask.reindex(context_index, fill_value=False),
                )
            )
        for threshold in quantile_thresholds(
            trades.loc[context_mask, "directional_confidence"], (0.50, 0.65, 0.80)
        ):
            mask = context_mask & (trades["directional_confidence"] >= threshold)
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}min_directional_confidence_{threshold:.4f}",
                    category="minimum_confidence",
                    description=f"{context} require directional confidence >= {threshold:.4f}",
                    context=context,
                    mask=mask.reindex(context_index, fill_value=False),
                )
            )
        for threshold in quantile_thresholds(trades.loc[context_mask, "stop_probability"], (0.50, 0.65, 0.80)):
            mask = context_mask & (trades["stop_probability"] <= threshold)
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}max_stop_probability_{threshold:.4f}",
                    category="stop_risk_filter",
                    description=f"{context} require stop-hit probability <= {threshold:.4f}",
                    context=context,
                    mask=mask.reindex(context_index, fill_value=False),
                )
            )

        for threshold in (18.0, 20.0, 25.0, 30.0):
            mask = context_mask & (pd.to_numeric(trades["adx"], errors="coerce") >= threshold)
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}min_adx_{threshold:.0f}",
                    category="minimum_trend_strength",
                    description=f"{context} require ADX >= {threshold:.0f}",
                    context=context,
                    mask=mask,
                )
            )

        rsi_ranges = [(35.0, 65.0), (40.0, 60.0), (45.0, 70.0), (30.0, 55.0)]
        rsi = pd.to_numeric(trades["rsi"], errors="coerce")
        for low, high in rsi_ranges:
            mask = context_mask & rsi.between(low, high, inclusive="both")
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}rsi_{low:.0f}_{high:.0f}",
                    category="rsi_range",
                    description=f"{context} require RSI between {low:.0f} and {high:.0f}",
                    context=context,
                    mask=mask,
                )
            )

        atr = pd.to_numeric(trades["atr"], errors="coerce")
        atr_values = atr[context_mask].dropna()
        if not atr_values.empty:
            q25, q75 = float(atr_values.quantile(0.25)), float(atr_values.quantile(0.75))
            atr_filters = [
                (f"atr_mid_{q25:.4f}_{q75:.4f}", atr.between(q25, q75, inclusive="both"), f"ATR between {q25:.4f} and {q75:.4f}"),
                (f"atr_above_{q25:.4f}", atr >= q25, f"ATR >= {q25:.4f}"),
                (f"atr_below_{q75:.4f}", atr <= q75, f"ATR <= {q75:.4f}"),
            ]
            for key, atr_mask, description in atr_filters:
                candidates.append(
                    FilterCandidate(
                        key=f"{prefix}{key}",
                        category="atr_range",
                        description=f"{context} require {description}",
                        context=context,
                        mask=context_mask & atr_mask,
                    )
                )

        volatility = pd.to_numeric(trades["volatility"], errors="coerce")
        vol_values = volatility[context_mask].dropna()
        if not vol_values.empty:
            q75 = float(vol_values.quantile(0.75))
            q25 = float(vol_values.quantile(0.25))
            candidates.extend(
                [
                    FilterCandidate(
                        key=f"{prefix}exclude_high_volatility",
                        category="volatility_filter",
                        description=f"{context} exclude volatility above p75 ({q75:.6f})",
                        context=context,
                        mask=context_mask & (volatility <= q75),
                    ),
                    FilterCandidate(
                        key=f"{prefix}exclude_low_volatility",
                        category="volatility_filter",
                        description=f"{context} exclude volatility below p25 ({q25:.6f})",
                        context=context,
                        mask=context_mask & (volatility >= q25),
                    ),
                ]
            )

        time_filters = [
            ("avoid_after_2pm", ~(trades["after_2pm"].astype(bool)), "avoid trades after 2 PM"),
            ("avoid_open_30m", trades["time_of_day"] != "open_30m", "avoid first 30 minutes"),
            ("avoid_closing_45m", trades["time_of_day"] != "closing_45m", "avoid last 45 minutes"),
            ("morning_only", trades["time_of_day"].isin(["open_30m", "morning"]), "trade morning only"),
            ("avoid_expiry_proxy", ~trades["expiry_proxy"].astype(bool), "avoid weekday-3 expiry proxy"),
        ]
        for key, time_mask, description in time_filters:
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}{key}",
                    category="time_filter",
                    description=f"{context} {description}",
                    context=context,
                    mask=context_mask & time_mask,
                )
            )

        for regime in sorted(trades.loc[context_mask, "market_regime"].dropna().astype(str).unique()):
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}only_regime_{regime}",
                    category="regime_filter",
                    description=f"{context} trade only regime={regime}",
                    context=context,
                    mask=context_mask & (trades["market_regime"].astype(str) == regime),
                )
            )
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}exclude_regime_{regime}",
                    category="regime_filter",
                    description=f"{context} exclude regime={regime}",
                    context=context,
                    mask=context_mask & (trades["market_regime"].astype(str) != regime),
                )
            )
    return candidates


def evaluate_filter_candidate(
    trades: pd.DataFrame,
    candidate: FilterCandidate,
    min_trades: int,
) -> dict[str, object]:
    context_mask = baseline_contexts(trades)[candidate.context]
    baseline_pnl = trades.loc[context_mask, "pnl"]
    selected = trades.loc[candidate.mask, "pnl"]
    baseline_metrics = metrics_from_pnl(baseline_pnl)
    selected_metrics = metrics_from_pnl(selected)
    lift_stats = expectancy_lift_stats(selected, baseline_pnl)
    recommended = bool(
        selected_metrics["trades"] >= min_trades
        and selected_metrics["net_pnl"] > 0
        and selected_metrics["expectancy"] > 0
        and selected_metrics["profit_factor"] >= 1.10
        and selected_metrics["profit_factor"] > baseline_metrics["profit_factor"]
        and lift_stats["expectancy_lift_ci_low"] > 0
    )
    return {
        "filter_key": candidate.key,
        "context": candidate.context,
        "category": candidate.category,
        "description": candidate.description,
        "baseline_trade_count": baseline_metrics["trades"],
        "baseline_net_pnl": baseline_metrics["net_pnl"],
        "baseline_profit_factor": baseline_metrics["profit_factor"],
        "baseline_win_rate": baseline_metrics["win_rate"],
        "baseline_expectancy": baseline_metrics["expectancy"],
        "trade_count": selected_metrics["trades"],
        "net_pnl": selected_metrics["net_pnl"],
        "profit_factor": selected_metrics["profit_factor"],
        "win_rate": selected_metrics["win_rate"],
        "expectancy": selected_metrics["expectancy"],
        "max_drawdown": selected_metrics["max_drawdown"],
        "avg_winner": selected_metrics["avg_winner"],
        "avg_loser": selected_metrics["avg_loser"],
        "coverage": float(selected_metrics["trades"] / max(1, baseline_metrics["trades"])),
        "statistical_test": "approx_welch_expectancy_lift_95ci",
        "statistically_meaningful": recommended,
        **lift_stats,
    }


def profitability_optimizer(
    trades: pd.DataFrame,
    min_trades: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.Series]]:
    candidates = build_filter_candidates(trades)
    rows = [evaluate_filter_candidate(trades, candidate, min_trades) for candidate in candidates]
    results = pd.DataFrame(rows)
    if results.empty:
        return results, results.copy(), {}
    results = results.sort_values(
        ["statistically_meaningful", "expectancy_lift_ci_low", "net_pnl", "trade_count"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    recommendations = results[results["statistically_meaningful"].astype(bool)].copy()
    recommendations = recommendations.sort_values(
        ["context", "expectancy_lift_ci_low", "net_pnl"],
        ascending=[True, False, False],
    )
    recommended_keys = set(recommendations["filter_key"].head(25))
    masks = {candidate.key: candidate.mask for candidate in candidates if candidate.key in recommended_keys}
    return results, recommendations.reset_index(drop=True), masks


def primary_failure_reason(row: pd.Series) -> str:
    if bool(row.get("stop_hit")):
        return "stop_hit"
    if bool(row.get("target_missed")) and row.get("holding_time_bucket") == "target_not_reached_in_window":
        return "target_missed_long_hold"
    if row.get("confidence_bucket") == "low_confidence":
        return "low_confidence"
    if row.get("volatility_bucket") == "high_volatility":
        return "high_volatility"
    if row.get("trend_strength_bucket") == "low_adx":
        return "low_trend_strength"
    if bool(row.get("after_2pm")):
        return "after_2pm"
    if str(row.get("market_regime")) == "range":
        return "range_regime"
    return "adverse_outcome"


def grouped_loss_rows(trades: pd.DataFrame, dimension: str) -> list[dict[str, object]]:
    losers = trades[trades["pnl"] < 0].copy()
    total_loss = abs(float(losers["pnl"].sum())) if not losers.empty else 0.0
    rows: list[dict[str, object]] = []
    for value, loss_group in losers.groupby(dimension, dropna=False):
        all_group = trades[trades[dimension].astype(str) == str(value)]
        metrics = metrics_from_pnl(all_group["pnl"])
        loss_amount = abs(float(loss_group["pnl"].sum()))
        rows.append(
            {
                "dimension": dimension,
                "bucket": str(value),
                "loss_count": int(len(loss_group)),
                "total_loss_abs": loss_amount,
                "avg_loss": float(loss_group["pnl"].mean()),
                "loss_share": float(loss_amount / total_loss) if total_loss > 0 else 0.0,
                "all_trade_count": metrics["trades"],
                "bucket_net_pnl": metrics["net_pnl"],
                "bucket_profit_factor": metrics["profit_factor"],
                "bucket_win_rate": metrics["win_rate"],
                "bucket_expectancy": metrics["expectancy"],
            }
        )
    return rows


def loss_failure_analysis(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    losers = trades[trades["pnl"] < 0].copy()
    if losers.empty:
        losers["primary_failure"] = []
    else:
        losers["primary_failure"] = losers.apply(primary_failure_reason, axis=1)

    dimensions = [
        "market_regime",
        "time_of_day",
        "day_of_week",
        "volatility_bucket",
        "trend_strength_bucket",
        "confidence_bucket",
        "stop_hit",
        "target_missed",
        "holding_time_bucket",
        "primary_failure",
        "side",
    ]
    grouped_rows: list[dict[str, object]] = []
    for dimension in dimensions:
        grouped_rows.extend(grouped_loss_rows(losers if dimension == "primary_failure" else trades, dimension))
    grouped = pd.DataFrame(grouped_rows).sort_values(
        ["total_loss_abs", "loss_count"], ascending=[False, False]
    )

    all_metrics = metrics_from_pnl(trades["pnl"])
    loss_metrics = {
        "losing_trades": int(len(losers)),
        "loss_rate": float(len(losers) / max(1, len(trades))),
        "gross_loss_abs": float(abs(losers["pnl"].sum())) if not losers.empty else 0.0,
        "avg_loss": float(losers["pnl"].mean()) if not losers.empty else 0.0,
        "median_loss": float(losers["pnl"].median()) if not losers.empty else 0.0,
    }
    summary = {
        "schema_version": PHASE5_SCHEMA_VERSION,
        "baseline": all_metrics,
        "losses": loss_metrics,
        "top_loss_buckets": json_records(grouped.head(20)),
    }
    return losers, grouped, summary


def load_feature_importance(config: PipelineConfig) -> dict[str, list[str]]:
    reports = config.paths.output_dir / "reports" / "validation"
    importance: dict[str, list[str]] = {}

    shap = read_json(reports / "shap_summary.json", {})
    if isinstance(shap, dict) and isinstance(shap.get("top_features"), list):
        target = str(shap.get("target", "directional_ce"))
        importance[target] = [
            str(item["feature"])
            for item in shap["top_features"]
            if isinstance(item, dict) and item.get("feature") in FEATURE_COLUMNS
        ]

    native_path = reports / "feature_importance_native.csv"
    if native_path.exists():
        native = pd.read_csv(native_path)
        if {"target", "feature", "importance"}.issubset(native.columns):
            for target, group in native.groupby("target"):
                features = (
                    group.sort_values("importance", ascending=False)["feature"]
                    .astype(str)
                    .loc[lambda values: values.isin(FEATURE_COLUMNS)]
                    .head(10)
                    .tolist()
                )
                importance.setdefault(str(target), features)
    return importance


def feature_quantile_labels(trades: pd.DataFrame, features: list[str]) -> dict[str, tuple[float, float]]:
    labels: dict[str, tuple[float, float]] = {}
    for feature in features:
        if feature not in trades:
            continue
        values = pd.to_numeric(trades[feature], errors="coerce").dropna()
        if values.empty:
            continue
        labels[feature] = (float(values.quantile(0.25)), float(values.quantile(0.75)))
    return labels


def describe_feature_values(row: pd.Series, features: list[str], quantiles: dict[str, tuple[float, float]]) -> str:
    parts: list[str] = []
    for feature in features[:5]:
        if feature not in row or pd.isna(row[feature]):
            continue
        value = float(row[feature])
        low, high = quantiles.get(feature, (float("-inf"), float("inf")))
        bucket = "low" if value <= low else "high" if value >= high else "normal"
        parts.append(f"{feature}={value:.4g}({bucket})")
    return "; ".join(parts) if parts else "feature attribution unavailable"


def why_trade_taken(row: pd.Series) -> str:
    pieces = [
        f"{str(row['side']).upper()} eligible candidate",
        f"regime={row.get('market_regime', 'unknown')}",
        f"time={row.get('time_of_day', 'unknown')}",
    ]
    if pd.notna(row.get("confidence")):
        pieces.append(f"quality_confidence={float(row['confidence']):.3f}")
    if pd.notna(row.get("target_probability")):
        pieces.append(f"target_probability={float(row['target_probability']):.3f}")
    if pd.notna(row.get("stop_probability")):
        pieces.append(f"stop_probability={float(row['stop_probability']):.3f}")
    return "; ".join(pieces)


def why_trade_won_or_lost(row: pd.Series) -> str:
    if row["pnl"] > 0:
        if bool(row.get("target_hit")):
            return "Won because favorable movement reached the target window before costs overwhelmed the trade."
        return "Won despite target miss because favorable excursion exceeded adverse movement after cost model."
    if bool(row.get("stop_hit")):
        return "Lost because adverse excursion reached the stop-risk threshold."
    if bool(row.get("target_missed")):
        return "Lost because the target was missed and favorable excursion did not cover cost and drawdown."
    return "Lost because realized quality-net outcome was negative after modeled spread and brokerage."


def first_preventing_filter(
    row_index: int,
    recommendations: pd.DataFrame,
    filter_masks: dict[str, pd.Series],
    side: str,
) -> str:
    if recommendations.empty:
        return "none_evidence_backed"
    context_order = [side, "all"]
    for context in context_order:
        context_recs = recommendations[recommendations["context"] == context]
        for row in context_recs.head(10).itertuples(index=False):
            mask = filter_masks.get(row.filter_key)
            if mask is not None and row_index in mask.index and not bool(mask.loc[row_index]):
                return str(row.description)
    return "none_evidence_backed"


def build_trade_reviews(
    trades: pd.DataFrame,
    recommendations: pd.DataFrame,
    filter_masks: dict[str, pd.Series],
    config: PipelineConfig,
    review_limit: int,
) -> pd.DataFrame:
    importance = load_feature_importance(config)
    review_frame = trades.copy()
    if review_limit > 0:
        review_frame = review_frame.head(review_limit).copy()

    rows: list[dict[str, object]] = []
    feature_cache: dict[str, tuple[list[str], dict[str, tuple[float, float]]]] = {}
    for index, row in review_frame.iterrows():
        side = str(row["side"])
        target = f"quality_profitable_{side}"
        features = importance.get(target) or importance.get(f"directional_{side}") or importance.get("directional_ce") or FEATURE_COLUMNS[:5]
        if target not in feature_cache:
            feature_cache[target] = (features, feature_quantile_labels(trades, features))
        feature_names, quantiles = feature_cache[target]
        prevented_by = (
            first_preventing_filter(index, recommendations, filter_masks, side)
            if float(row["pnl"]) < 0
            else "not_applicable_win"
        )
        should_skip = bool(
            float(row["pnl"]) < 0
            and (
                prevented_by != "none_evidence_backed"
                or row.get("confidence_bucket") == "low_confidence"
                or bool(row.get("stop_hit"))
                or row.get("trend_strength_bucket") == "low_adx"
            )
        )
        rows.append(
            {
                "trade_id": row["trade_id"],
                "date": row["date"],
                "side": side,
                "pnl": float(row["pnl"]),
                "outcome": row["outcome"],
                "market_regime": row.get("market_regime"),
                "time_of_day": row.get("time_of_day"),
                "confidence": row.get("confidence"),
                "target_hit": bool(row.get("target_hit")),
                "stop_hit": bool(row.get("stop_hit")),
                "target_missed": bool(row.get("target_missed")),
                "holding_bars": row.get("holding_bars"),
                "why_taken": why_trade_taken(row),
                "why_won_or_lost": why_trade_won_or_lost(row),
                "top_feature_values": describe_feature_values(row, feature_names, quantiles),
                "should_have_been_skipped": should_skip,
                "filter_that_would_have_prevented_loss": prevented_by,
            }
        )
    return pd.DataFrame(rows)


def performance_row(
    trades: pd.DataFrame,
    mask: pd.Series,
    group: str,
    bucket: str,
    side: str,
    baseline_pnl: pd.Series,
) -> dict[str, object]:
    selected = trades.loc[mask, "pnl"]
    metrics = metrics_from_pnl(selected)
    lift = expectancy_lift_stats(selected, baseline_pnl)
    return {
        "group": group,
        "bucket": bucket,
        "side": side,
        "trade_count": metrics["trades"],
        "net_pnl": metrics["net_pnl"],
        "profit_factor": metrics["profit_factor"],
        "win_rate": metrics["win_rate"],
        "expectancy": metrics["expectancy"],
        "max_drawdown": metrics["max_drawdown"],
        **lift,
    }


def regime_performance_matrix(trades: pd.DataFrame, min_trades: int) -> tuple[pd.DataFrame, dict[str, object]]:
    rows: list[dict[str, object]] = []
    contexts = baseline_contexts(trades)
    for side, context_mask in contexts.items():
        baseline_pnl = trades.loc[context_mask, "pnl"]
        regime_conditions: list[tuple[str, str, pd.Series]] = []
        for regime in sorted(trades.loc[context_mask, "market_regime"].dropna().astype(str).unique()):
            regime_conditions.append(("market_regime", regime, context_mask & (trades["market_regime"].astype(str) == regime)))
        regime_conditions.extend(
            [
                ("regime_example", "Trending", context_mask & (pd.to_numeric(trades["adx"], errors="coerce") >= 25)),
                ("regime_example", "Range", context_mask & (pd.to_numeric(trades["adx"], errors="coerce") < 18)),
                ("regime_example", "High Volatility", context_mask & (trades["volatility_bucket"] == "high_volatility")),
                ("regime_example", "Low Volatility", context_mask & (trades["volatility_bucket"] == "low_volatility")),
                ("regime_example", "Gap Opening", context_mask & trades["opening_gap_proxy"].astype(bool)),
                ("regime_example", "Expiry Proxy", context_mask & trades["expiry_proxy"].astype(bool)),
            ]
        )
        for group, bucket, mask in regime_conditions:
            rows.append(performance_row(trades, mask, group, bucket, side, baseline_pnl))
    matrix = pd.DataFrame(rows)
    if matrix.empty:
        return matrix, {}
    eligible = matrix[matrix["trade_count"] >= min_trades].copy()
    best = eligible.sort_values(["expectancy", "profit_factor", "net_pnl"], ascending=False).head(10)
    worst = eligible.sort_values(["expectancy", "net_pnl"], ascending=True).head(10)
    summary = {
        "best_regimes": json_records(best),
        "worst_regimes": json_records(worst),
        "min_trades": int(min_trades),
    }
    return matrix.sort_values(["side", "group", "bucket"]).reset_index(drop=True), summary


def counterfactual_strategy_pnl(
    trades: pd.DataFrame,
    reward_points: float,
    stop_points: float,
    risk_multiplier: float,
    trailing_stop: bool,
    partial_exit: float,
    config: PipelineConfig,
) -> pd.Series:
    mfe = pd.to_numeric(trades["mfe_points"], errors="coerce").fillna(0.0)
    mae = pd.to_numeric(trades["mae_points"], errors="coerce").fillna(0.0)
    reward_hit = mfe >= reward_points
    stop_hit = mae >= stop_points
    unresolved_points = mfe * 0.35 - mae * 0.25 - config.labels.spread_points_round_trip
    points = pd.Series(unresolved_points, index=trades.index, dtype=float)
    points = points.mask(stop_hit & ~reward_hit, -stop_points)
    points = points.mask(reward_hit, reward_points)
    points = points.mask(stop_hit & reward_hit, reward_points * 0.65 - stop_points * 0.20)
    if trailing_stop:
        locked = np.maximum(points, mfe * 0.40 - config.labels.spread_points_round_trip)
        points = pd.Series(locked, index=trades.index)
    if partial_exit > 0:
        target_component = reward_points * partial_exit
        runner_component = points * (1.0 - partial_exit)
        points = pd.Series(np.where(reward_hit, target_component + runner_component, points * (1.0 - partial_exit * 0.15)), index=trades.index)
    return points * config.labels.lot_units * risk_multiplier - config.labels.brokerage_round_trip_rs


def entry_filter_mask(trades: pd.DataFrame, name: str, threshold: float) -> pd.Series:
    if name == "none":
        return pd.Series(True, index=trades.index)
    if name == "confidence_threshold":
        return trades["confidence"] >= threshold
    if name == "confidence_and_adx":
        return (trades["confidence"] >= threshold) & (pd.to_numeric(trades["adx"], errors="coerce") >= 20)
    if name == "confidence_no_after_2pm":
        return (trades["confidence"] >= threshold) & (~trades["after_2pm"].astype(bool))
    if name == "confidence_no_high_vol":
        return (trades["confidence"] >= threshold) & (trades["volatility_bucket"] != "high_volatility")
    raise ValueError(f"unknown entry filter: {name}")


def strategy_optimizer(trades: pd.DataFrame, config: PipelineConfig, min_trades: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    confidence_values = quantile_thresholds(trades["confidence"], (0.55, 0.65, 0.75))
    if not confidence_values:
        confidence_values = [0.0]
    reward_values = [
        config.labels.directional_target_points * multiplier for multiplier in (0.75, 1.0, 1.25, 1.50)
    ]
    stop_values = [
        config.labels.directional_target_points * multiplier for multiplier in (1.0, 1.5, 2.0)
    ]
    risk_values = [0.50, 1.00, 1.25]
    trailing_values = [False, True]
    partial_values = [0.0, 0.50]
    entry_filters = [
        "none",
        "confidence_threshold",
        "confidence_and_adx",
        "confidence_no_after_2pm",
        "confidence_no_high_vol",
    ]
    rows: list[dict[str, object]] = []
    for entry_filter in entry_filters:
        thresholds = [0.0] if entry_filter == "none" else confidence_values
        for threshold, reward, stop, risk, trailing, partial in itertools.product(
            thresholds, reward_values, stop_values, risk_values, trailing_values, partial_values
        ):
            mask = entry_filter_mask(trades, entry_filter, threshold)
            if int(mask.sum()) == 0:
                continue
            pnl = counterfactual_strategy_pnl(
                trades.loc[mask],
                reward_points=reward,
                stop_points=stop,
                risk_multiplier=risk,
                trailing_stop=trailing,
                partial_exit=partial,
                config=config,
            )
            metrics = metrics_from_pnl(pnl)
            rows.append(
                {
                    "entry_filter": entry_filter,
                    "confidence_threshold": threshold,
                    "risk_multiplier": risk,
                    "reward_points": reward,
                    "stop_points": stop,
                    "trailing_stop": trailing,
                    "partial_exit_fraction": partial,
                    "trade_count": metrics["trades"],
                    "net_pnl": metrics["net_pnl"],
                    "profit_factor": metrics["profit_factor"],
                    "win_rate": metrics["win_rate"],
                    "expectancy": metrics["expectancy"],
                    "max_drawdown": metrics["max_drawdown"],
                    "avg_winner": metrics["avg_winner"],
                    "avg_loser": metrics["avg_loser"],
                    "meets_min_trades": metrics["trades"] >= min_trades,
                }
            )
    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid, grid.copy()
    grid = grid.sort_values(
        ["meets_min_trades", "net_pnl", "profit_factor", "expectancy"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    top = grid[grid["meets_min_trades"].astype(bool)].head(25).copy()
    if top.empty:
        top = grid.head(25).copy()
    top["rank"] = np.arange(1, len(top) + 1)
    return grid, top


def bad_segment_recommendations(
    matrix: pd.DataFrame,
    min_trades: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if matrix.empty:
        return rows
    candidates = matrix[
        (matrix["trade_count"] >= min_trades)
        & (matrix["net_pnl"] < 0)
        & (matrix["expectancy"] < 0)
        & (matrix["expectancy_lift_ci_high"] < 0)
    ].copy()
    for row in candidates.sort_values(["net_pnl", "expectancy"]).head(15).itertuples(index=False):
        rows.append(
            {
                "source": "regime_performance_matrix",
                "recommendation_type": "avoid_or_reduce",
                "action": f"Reduce or disable {row.side} trades in {row.bucket}",
                "evidence": (
                    f"{row.trade_count} trades, net PnL {row.net_pnl:.2f}, "
                    f"expectancy {row.expectancy:.2f}, 95pct lift CI high {row.expectancy_lift_ci_high:.2f}"
                ),
                "expected_effect": "Avoids a statistically weak segment in offline validation.",
                "confidence": "high" if row.trade_count >= min_trades * 2 else "medium",
            }
        )
    return rows


def filter_recommendations(filters: pd.DataFrame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in filters.head(20).itertuples(index=False):
        rows.append(
            {
                "source": "profitability_optimizer",
                "recommendation_type": "entry_filter",
                "action": row.description,
                "evidence": (
                    f"{row.trade_count} trades, net PnL {row.net_pnl:.2f}, "
                    f"PF {row.profit_factor:.2f}, win rate {row.win_rate:.2%}, "
                    f"expectancy lift CI low {row.expectancy_lift_ci_low:.2f}"
                ),
                "expected_effect": "Improves offline expectancy with positive 95pct lower-bound lift.",
                "confidence": "high" if row.p_value_approx <= 0.01 else "medium",
            }
        )
    return rows


def strategy_recommendations(top_strategies: pd.DataFrame) -> list[dict[str, object]]:
    if top_strategies.empty:
        return []
    rows: list[dict[str, object]] = []
    for row in top_strategies.head(5).itertuples(index=False):
        threshold_text = (
            "without confidence threshold"
            if row.entry_filter == "none"
            else f"with threshold {row.confidence_threshold:.4f}"
        )
        rows.append(
            {
                "source": "strategy_optimizer",
                "recommendation_type": "research_strategy",
                "action": (
                    f"Research {row.entry_filter} {threshold_text}, "
                    f"reward {row.reward_points:.2f}, stop {row.stop_points:.2f}, "
                    f"risk x{row.risk_multiplier:.2f}, trailing={bool(row.trailing_stop)}, "
                    f"partial={row.partial_exit_fraction:.2f}"
                ),
                "evidence": (
                    f"{row.trade_count} trades, net PnL {row.net_pnl:.2f}, "
                    f"PF {row.profit_factor:.2f}, expectancy {row.expectancy:.2f}"
                ),
                "expected_effect": "Ranks strategy settings by profitability, not ML score.",
                "confidence": "medium",
            }
        )
    return rows


def generate_recommendations(
    filter_recs: pd.DataFrame,
    matrix: pd.DataFrame,
    top_strategies: pd.DataFrame,
    min_trades: int,
) -> pd.DataFrame:
    rows = []
    rows.extend(filter_recommendations(filter_recs))
    rows.extend(bad_segment_recommendations(matrix, min_trades))
    rows.extend(strategy_recommendations(top_strategies))
    recommendations = pd.DataFrame(rows)
    if recommendations.empty:
        return recommendations
    recommendations["schema_version"] = PHASE5_SCHEMA_VERSION
    recommendations["research_only"] = True
    return recommendations.drop_duplicates(subset=["source", "action"]).reset_index(drop=True)


def write_loss_markdown(summary: dict[str, object], grouped: pd.DataFrame, path: Path) -> None:
    lines = [
        "# Phase 5 Loss Analysis",
        "",
        "This report is generated from experimental Pipeline V2 artifacts only.",
        "",
        "## Baseline",
        "",
    ]
    baseline = summary.get("baseline", {})
    losses = summary.get("losses", {})
    if isinstance(baseline, dict):
        lines.extend(
            [
                f"- Trades: {baseline.get('trades', 0)}",
                f"- Net PnL: {float(baseline.get('net_pnl', 0.0)):.2f}",
                f"- Profit Factor: {float(baseline.get('profit_factor', 0.0)):.4f}",
                f"- Win Rate: {float(baseline.get('win_rate', 0.0)):.2%}",
                f"- Expectancy: {float(baseline.get('expectancy', 0.0)):.2f}",
            ]
        )
    if isinstance(losses, dict):
        lines.extend(
            [
                "",
                "## Losses",
                "",
                f"- Losing trades: {losses.get('losing_trades', 0)}",
                f"- Loss rate: {float(losses.get('loss_rate', 0.0)):.2%}",
                f"- Gross loss: {float(losses.get('gross_loss_abs', 0.0)):.2f}",
            ]
        )
    lines.extend(["", "## Largest Loss Buckets", ""])
    for row in grouped.head(15).itertuples(index=False):
        lines.append(
            f"- {row.dimension}={row.bucket}: losses={row.loss_count}, "
            f"gross_loss={row.total_loss_abs:.2f}, expectancy={row.bucket_expectancy:.2f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_phase5_profitability_intelligence(
    config: PipelineConfig,
    split: str = "test",
    max_rows: int = 0,
    min_trades: int = 200,
    review_limit: int = 0,
) -> dict[str, object]:
    paths = phase5_paths(config)
    dataset, dataset_metadata = load_labeled_dataset(config, split=split, max_rows=max_rows)
    scored, prediction_status = add_model_scores(config, dataset)
    trades = build_trade_table(config, scored)
    write_csv(trades, paths.trades / "completed_trades.csv")

    losers, loss_grouped, loss_summary = loss_failure_analysis(trades)
    write_csv(losers, paths.loss_analysis / "losing_trades.csv")
    write_csv(loss_grouped, paths.loss_analysis / "loss_by_dimension.csv")
    write_json(paths.loss_analysis / "loss_summary.json", loss_summary)
    write_loss_markdown(loss_summary, loss_grouped, paths.loss_analysis / "loss_analysis_report.md")

    optimizer_results, optimizer_recs, filter_masks = profitability_optimizer(trades, min_trades=min_trades)
    write_csv(optimizer_results, paths.optimizer / "filter_tests.csv")
    write_csv(optimizer_recs, paths.optimizer / "recommended_filters.csv")
    write_json(paths.optimizer / "recommended_filters.json", json_records(optimizer_recs))

    reviews = build_trade_reviews(
        trades,
        recommendations=optimizer_recs,
        filter_masks=filter_masks,
        config=config,
        review_limit=review_limit,
    )
    write_csv(reviews, paths.reviews / "trade_reviews.csv")
    write_json(paths.reviews / "trade_reviews_sample.json", json_records(reviews.head(100)))

    strategy_grid, top_strategies = strategy_optimizer(trades, config=config, min_trades=min_trades)
    write_csv(strategy_grid, paths.strategy_optimizer / "strategy_grid.csv")
    write_csv(top_strategies, paths.strategy_optimizer / "top_strategies.csv")
    write_json(paths.strategy_optimizer / "top_strategies.json", json_records(top_strategies))

    matrix, matrix_summary = regime_performance_matrix(trades, min_trades=min_trades)
    write_csv(matrix, paths.regime_matrix / "regime_performance_matrix.csv")
    write_json(paths.regime_matrix / "regime_summary.json", matrix_summary)

    recommendations = generate_recommendations(
        filter_recs=optimizer_recs,
        matrix=matrix,
        top_strategies=top_strategies,
        min_trades=min_trades,
    )
    write_csv(recommendations, paths.recommendations / "recommendations.csv")
    write_json(paths.recommendations / "recommendations.json", json_records(recommendations))

    baseline = metrics_from_pnl(trades["pnl"])
    summary = {
        "schema_version": PHASE5_SCHEMA_VERSION,
        "status": "ok",
        "phase": 5,
        "controls": {
            "experimental_only": True,
            "production_files_modified": False,
            "live_engine_integrated": False,
            "retrained_models": False,
        },
        "dataset": dataset_metadata,
        "prediction_status": prediction_status,
        "trade_count": int(len(trades)),
        "losing_trade_count": int((trades["pnl"] < 0).sum()),
        "baseline": baseline,
        "recommended_filter_count": int(len(optimizer_recs)),
        "recommendation_count": int(len(recommendations)),
        "top_recommendations": json_records(recommendations.head(10)),
        "top_strategies": json_records(top_strategies.head(10)),
        "reports": {
            "trades": str(paths.trades),
            "loss_analysis": str(paths.loss_analysis),
            "optimizer": str(paths.optimizer),
            "trade_reviews": str(paths.reviews),
            "strategy_optimizer": str(paths.strategy_optimizer),
            "regime_matrix": str(paths.regime_matrix),
            "recommendations": str(paths.recommendations),
        },
    }
    write_json(paths.base / "phase5_summary.json", summary)
    return summary
