from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline_v2.config import FEATURE_COLUMNS, PipelineConfig
from ml_pipeline_v2.phase4 import read_json, write_json
from ml_pipeline_v2.phase5 import (
    FilterCandidate,
    build_filter_candidates,
    counterfactual_strategy_pnl,
    expectancy_lift_stats,
    json_records,
    load_feature_importance,
    metrics_from_pnl,
    phase5_paths,
    primary_failure_reason,
    write_csv,
    why_trade_taken,
    why_trade_won_or_lost,
)


PHASE55_SCHEMA_VERSION = "phase55.autonomous_strategy_improvement.v1"


@dataclass(frozen=True)
class Phase55Paths:
    base: Path
    validation: Path
    combinations: Path
    counterfactuals: Path
    replay: Path
    filters: Path
    final: Path


@dataclass(frozen=True)
class RecommendationCandidate:
    key: str
    source: str
    recommendation_type: str
    action: str
    description: str
    category: str
    context: str
    mask: pd.Series
    strategy_params: dict[str, object] | None = None


def phase55_paths(config: PipelineConfig) -> Phase55Paths:
    base = config.paths.output_dir / "reports" / "phase55"
    paths = Phase55Paths(
        base=base,
        validation=base / "single_recommendation_validation",
        combinations=base / "combination_optimizer",
        counterfactuals=base / "counterfactual_trade_simulator",
        replay=base / "trade_replay_intelligence",
        filters=base / "filter_ranking",
        final=base / "final_recommendation_engine",
    )
    for path in (
        paths.base,
        paths.validation,
        paths.combinations,
        paths.counterfactuals,
        paths.replay,
        paths.filters,
        paths.final,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def load_phase5_trades(config: PipelineConfig) -> pd.DataFrame:
    path = phase5_paths(config).trades / "completed_trades.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Phase 5 completed trades not found: {path}. Run Phase 5 before Phase 5.5."
        )
    trades = pd.read_csv(path)
    trades["date"] = pd.to_datetime(trades["date"], errors="coerce")
    return trades.sort_values(["date", "side", "trade_id"]).reset_index(drop=True)


def load_phase5_recommendations(config: PipelineConfig) -> list[dict[str, object]]:
    path = phase5_paths(config).recommendations / "recommendations.json"
    payload = read_json(path, [])
    if not isinstance(payload, list):
        raise ValueError(f"Phase 5 recommendations are not a list: {path}")
    return [row for row in payload if isinstance(row, dict)]


def load_phase5_summary(config: PipelineConfig) -> dict[str, object]:
    payload = read_json(phase5_paths(config).base / "phase5_summary.json", {})
    return payload if isinstance(payload, dict) else {}


def candidate_lookup_from_phase5(trades: pd.DataFrame) -> dict[str, tuple[str, str, str, pd.Series]]:
    lookup: dict[str, tuple[str, str, str, pd.Series]] = {}
    for candidate in build_filter_candidates(trades):
        mask = strategy_keep_mask(trades, candidate)
        lookup[candidate.description] = (
            candidate.key,
            candidate.category,
            candidate.context,
            mask,
        )
    return lookup


def strategy_keep_mask(trades: pd.DataFrame, candidate: FilterCandidate) -> pd.Series:
    raw = candidate.mask.reindex(trades.index, fill_value=False).astype(bool)
    if candidate.context in {"ce", "pe"}:
        side_mask = trades["side"].astype(str).str.lower() == candidate.context
        return ((~side_mask) | raw).astype(bool)
    return raw


def avoid_segment_mask(trades: pd.DataFrame, action: str) -> tuple[str, str, str, pd.Series] | None:
    prefix = "Reduce or disable "
    if not action.startswith(prefix) or " trades in " not in action:
        return None
    left, bucket = action[len(prefix) :].split(" trades in ", 1)
    context = left.strip().lower()
    bucket = bucket.strip()
    if context not in {"all", "ce", "pe"}:
        return None

    context_mask = pd.Series(True, index=trades.index)
    if context in {"ce", "pe"}:
        context_mask = trades["side"].astype(str).str.lower() == context

    bucket_lower = bucket.lower()
    if bucket_lower in set(trades.get("market_regime", pd.Series(dtype=str)).dropna().astype(str).str.lower()):
        segment = trades["market_regime"].astype(str).str.lower() == bucket_lower
    elif bucket_lower == "low volatility":
        segment = trades["volatility_bucket"].astype(str).str.lower() == "low_volatility"
    elif bucket_lower == "high volatility":
        segment = trades["volatility_bucket"].astype(str).str.lower() == "high_volatility"
    elif bucket_lower == "range":
        segment = (trades["trend_strength_bucket"].astype(str).str.lower() == "low_adx") | (
            trades["market_regime"].astype(str).str.lower() == "range"
        )
    elif bucket_lower == "trending":
        segment = pd.to_numeric(trades["adx"], errors="coerce") >= 25.0
    elif bucket_lower == "gap opening":
        segment = trades["opening_gap_proxy"].astype(bool)
    elif bucket_lower == "expiry proxy":
        segment = trades["expiry_proxy"].astype(bool)
    else:
        return None

    mask = (~context_mask) | (context_mask & ~segment)
    key = f"avoid:{context}:{bucket_lower.replace(' ', '_')}"
    return key, "avoid_weak_segment", context, mask.astype(bool)


def parse_strategy_action(action: str) -> dict[str, object] | None:
    if not action.startswith("Research "):
        return None
    try:
        body = action[len("Research ") :]
        entry_part, params_part = body.split(", reward ", 1)
        if " without confidence threshold" in entry_part:
            entry_filter = entry_part.replace(" without confidence threshold", "").strip()
            threshold = 0.0
        elif " with threshold " in entry_part:
            entry_filter, threshold_text = entry_part.split(" with threshold ", 1)
            threshold = float(threshold_text)
        else:
            return None

        reward_text, rest = params_part.split(", stop ", 1)
        stop_text, rest = rest.split(", risk x", 1)
        risk_text, rest = rest.split(", trailing=", 1)
        trailing_text, partial_text = rest.split(", partial=", 1)
        return {
            "entry_filter": entry_filter.strip(),
            "confidence_threshold": float(threshold),
            "reward_points": float(reward_text),
            "stop_points": float(stop_text),
            "risk_multiplier": float(risk_text),
            "trailing_stop": trailing_text.strip().lower() == "true",
            "partial_exit_fraction": float(partial_text),
        }
    except (TypeError, ValueError):
        return None


def strategy_entry_mask(trades: pd.DataFrame, params: dict[str, object]) -> pd.Series:
    name = str(params.get("entry_filter", "none"))
    threshold = float(params.get("confidence_threshold", 0.0))
    confidence = pd.to_numeric(trades["confidence"], errors="coerce")
    if name == "none":
        return pd.Series(True, index=trades.index)
    if name == "confidence_threshold":
        return confidence >= threshold
    if name == "confidence_and_adx":
        return (confidence >= threshold) & (pd.to_numeric(trades["adx"], errors="coerce") >= 20.0)
    if name == "confidence_no_after_2pm":
        return (confidence >= threshold) & (~trades["after_2pm"].astype(bool))
    if name == "confidence_no_high_vol":
        return (confidence >= threshold) & (trades["volatility_bucket"].astype(str) != "high_volatility")
    return pd.Series(False, index=trades.index)


def recommendation_candidates(
    trades: pd.DataFrame,
    recommendations: list[dict[str, object]],
) -> list[RecommendationCandidate]:
    lookup = candidate_lookup_from_phase5(trades)
    rows: list[RecommendationCandidate] = []
    seen: set[str] = set()
    for index, rec in enumerate(recommendations, start=1):
        action = str(rec.get("action", ""))
        source = str(rec.get("source", "unknown"))
        rec_type = str(rec.get("recommendation_type", "unknown"))
        key = ""
        category = rec_type
        context = "all"
        mask = pd.Series(True, index=trades.index)
        params = None

        if action in lookup:
            key, category, context, mask = lookup[action]
        else:
            avoid = avoid_segment_mask(trades, action)
            if avoid is not None:
                key, category, context, mask = avoid
            else:
                params = parse_strategy_action(action)
                if params is not None:
                    key = (
                        f"strategy:{params['entry_filter']}:"
                        f"{float(params['confidence_threshold']):.4f}:"
                        f"{float(params['reward_points']):.2f}:"
                        f"{float(params['stop_points']):.2f}:"
                        f"{float(params['risk_multiplier']):.2f}:"
                        f"{bool(params['trailing_stop'])}:"
                        f"{float(params['partial_exit_fraction']):.2f}"
                    )
                    category = "exit_strategy"
                    context = "all"
                    mask = strategy_entry_mask(trades, params)

        if not key:
            key = f"unparsed:{index}"
            category = "unparsed"
            mask = pd.Series(True, index=trades.index)

        unique_key = key
        counter = 2
        while unique_key in seen:
            unique_key = f"{key}:{counter}"
            counter += 1
        seen.add(unique_key)

        rows.append(
            RecommendationCandidate(
                key=unique_key,
                source=source,
                recommendation_type=rec_type,
                action=action,
                description=action,
                category=category,
                context=context,
                mask=mask.reindex(trades.index, fill_value=False).astype(bool),
                strategy_params=params,
            )
        )
    return rows


def pnl_for_candidate(
    trades: pd.DataFrame,
    candidate: RecommendationCandidate,
    config: PipelineConfig,
) -> pd.Series:
    selected = trades.loc[candidate.mask].copy()
    if candidate.strategy_params is None:
        return pd.to_numeric(selected["pnl"], errors="coerce").dropna()
    params = candidate.strategy_params
    return counterfactual_strategy_pnl(
        selected,
        reward_points=float(params["reward_points"]),
        stop_points=float(params["stop_points"]),
        risk_multiplier=float(params["risk_multiplier"]),
        trailing_stop=bool(params["trailing_stop"]),
        partial_exit=float(params["partial_exit_fraction"]),
        config=config,
    )


def metrics_row(prefix: str, metrics: dict[str, float]) -> dict[str, float]:
    return {f"{prefix}_{key}": value for key, value in metrics.items()}


def is_statistically_acceptable(
    row: dict[str, object],
    baseline: dict[str, float],
    min_trades: int,
    min_trade_coverage: float,
) -> bool:
    trade_count = int(row.get("trade_count", 0))
    baseline_trades = max(1, int(baseline.get("trades", 0)))
    return bool(
        trade_count >= min_trades
        and trade_count >= math.ceil(baseline_trades * min_trade_coverage)
        and float(row.get("net_pnl", 0.0)) > 0.0
        and float(row.get("expectancy", 0.0)) > float(baseline.get("expectancy", 0.0))
        and float(row.get("profit_factor", 0.0)) > float(baseline.get("profit_factor", 0.0))
        and float(row.get("expectancy_lift_ci_low", 0.0)) > 0.0
        and float(row.get("p_value_approx", 1.0)) <= 0.05
    )


def validation_result(
    trades: pd.DataFrame,
    pnl: pd.Series,
    baseline_pnl: pd.Series,
    baseline: dict[str, float],
    min_trades: int,
    min_trade_coverage: float,
) -> dict[str, object]:
    metrics = metrics_from_pnl(pnl)
    lift = expectancy_lift_stats(pnl, baseline_pnl)
    row: dict[str, object] = {
        "trade_count": metrics["trades"],
        "net_pnl": metrics["net_pnl"],
        "profit_factor": metrics["profit_factor"],
        "win_rate": metrics["win_rate"],
        "expectancy": metrics["expectancy"],
        "max_drawdown": metrics["max_drawdown"],
        "avg_winner": metrics["avg_winner"],
        "avg_loser": metrics["avg_loser"],
        "trade_coverage": float(metrics["trades"] / max(1, baseline["trades"])),
        "net_pnl_delta": float(metrics["net_pnl"] - baseline["net_pnl"]),
        "profit_factor_delta": float(metrics["profit_factor"] - baseline["profit_factor"]),
        "expectancy_delta": float(metrics["expectancy"] - baseline["expectancy"]),
        "max_drawdown_delta": float(metrics["max_drawdown"] - baseline["max_drawdown"]),
        **metrics_row("baseline", baseline),
        **lift,
    }
    row["statistically_acceptable"] = is_statistically_acceptable(
        row,
        baseline=baseline,
        min_trades=min_trades,
        min_trade_coverage=min_trade_coverage,
    )
    return row


def single_recommendation_validation(
    trades: pd.DataFrame,
    candidates: list[RecommendationCandidate],
    config: PipelineConfig,
    min_trades: int,
    min_trade_coverage: float,
) -> pd.DataFrame:
    baseline_pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna()
    baseline = metrics_from_pnl(baseline_pnl)
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        pnl = pnl_for_candidate(trades, candidate, config)
        row = validation_result(
            trades,
            pnl=pnl,
            baseline_pnl=baseline_pnl,
            baseline=baseline,
            min_trades=min_trades,
            min_trade_coverage=min_trade_coverage,
        )
        rows.append(
            {
                "candidate_key": candidate.key,
                "source": candidate.source,
                "recommendation_type": candidate.recommendation_type,
                "category": candidate.category,
                "context": candidate.context,
                "action": candidate.action,
                **row,
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(
        ["statistically_acceptable", "profit_factor", "expectancy", "net_pnl", "trade_count"],
        ascending=[False, False, False, False, False],
    ).reset_index(drop=True)


def temporal_stability_for_mask(
    trades: pd.DataFrame,
    mask: pd.Series,
    min_fold_trades: int,
    folds: int = 4,
) -> dict[str, object]:
    ordered = trades.sort_values("date").copy()
    fold_indexes = np.array_split(ordered.index.to_numpy(), folds)
    pass_count = 0
    evaluated = 0
    fold_rows: list[dict[str, object]] = []
    for fold_id, indexes in enumerate(fold_indexes, start=1):
        if len(indexes) == 0:
            continue
        fold_mask = pd.Series(False, index=trades.index)
        fold_mask.loc[indexes] = True
        baseline_pnl = pd.to_numeric(trades.loc[fold_mask, "pnl"], errors="coerce").dropna()
        selected_pnl = pd.to_numeric(trades.loc[fold_mask & mask, "pnl"], errors="coerce").dropna()
        if len(selected_pnl) < min_fold_trades:
            fold_rows.append(
                {
                    "fold": fold_id,
                    "selected_trades": int(len(selected_pnl)),
                    "baseline_expectancy": float(baseline_pnl.mean()) if len(baseline_pnl) else 0.0,
                    "selected_expectancy": 0.0,
                    "passed": False,
                }
            )
            continue
        evaluated += 1
        baseline_metrics = metrics_from_pnl(baseline_pnl)
        selected_metrics = metrics_from_pnl(selected_pnl)
        passed = bool(
            selected_metrics["expectancy"] > baseline_metrics["expectancy"]
            and selected_metrics["profit_factor"] > baseline_metrics["profit_factor"]
        )
        pass_count += int(passed)
        fold_rows.append(
            {
                "fold": fold_id,
                "selected_trades": int(len(selected_pnl)),
                "baseline_expectancy": baseline_metrics["expectancy"],
                "selected_expectancy": selected_metrics["expectancy"],
                "baseline_profit_factor": baseline_metrics["profit_factor"],
                "selected_profit_factor": selected_metrics["profit_factor"],
                "passed": passed,
            }
        )
    required = max(1, math.ceil(max(1, evaluated) * 0.75))
    return {
        "temporal_folds_evaluated": int(evaluated),
        "temporal_pass_folds": int(pass_count),
        "temporal_stability": bool(evaluated > 0 and pass_count >= required),
        "temporal_fold_details": fold_rows,
    }


def combination_optimizer(
    trades: pd.DataFrame,
    candidates: list[RecommendationCandidate],
    config: PipelineConfig,
    min_trades: int,
    min_trade_coverage: float,
    max_combination_size: int,
) -> pd.DataFrame:
    baseline_pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna()
    baseline = metrics_from_pnl(baseline_pnl)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.strategy_params is None and candidate.category != "unparsed"
    ]
    rows: list[dict[str, object]] = []
    for size in range(1, max_combination_size + 1):
        for combo in itertools.combinations(eligible, size):
            mask = pd.Series(True, index=trades.index)
            keys: list[str] = []
            actions: list[str] = []
            categories: list[str] = []
            for candidate in combo:
                mask &= candidate.mask
                keys.append(candidate.key)
                actions.append(candidate.action)
                categories.append(candidate.category)
            pnl = pd.to_numeric(trades.loc[mask, "pnl"], errors="coerce").dropna()
            row = validation_result(
                trades,
                pnl=pnl,
                baseline_pnl=baseline_pnl,
                baseline=baseline,
                min_trades=min_trades,
                min_trade_coverage=min_trade_coverage,
            )
            overfit_penalty = 0.0
            if row["trade_coverage"] < 0.20:
                overfit_penalty += 1.0
            if size > 3:
                overfit_penalty += float(size - 3) * 0.5
            stability = temporal_stability_for_mask(
                trades,
                mask=mask,
                min_fold_trades=max(50, min_trades // 4),
            )
            row["statistically_acceptable"] = bool(
                row["statistically_acceptable"] and stability["temporal_stability"]
            )
            score = (
                float(row["profit_factor"]) * 4.0
                + float(row["expectancy"]) / 100.0
                + float(row["net_pnl"]) / 1_000_000.0
                + min(0.0, float(row["max_drawdown"])) / 1_000_000.0
                + float(row["trade_coverage"])
                - overfit_penalty
            )
            rows.append(
                {
                    "combination_key": "+".join(keys),
                    "combination_size": size,
                    "actions": " | ".join(actions),
                    "categories": "|".join(sorted(set(categories))),
                    "overfit_penalty": overfit_penalty,
                    "rank_score": score,
                    "temporal_folds_evaluated": stability["temporal_folds_evaluated"],
                    "temporal_pass_folds": stability["temporal_pass_folds"],
                    "temporal_stability": stability["temporal_stability"],
                    **row,
                }
            )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    valid = result[result["statistically_acceptable"].astype(bool)].copy()
    if valid.empty:
        valid = result.copy()
    return valid.sort_values(
        ["statistically_acceptable", "rank_score", "profit_factor", "expectancy", "net_pnl", "trade_count"],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def counterfactual_alternatives(
    trades: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    losing = trades[pd.to_numeric(trades["pnl"], errors="coerce") < 0].copy()
    if losing.empty:
        return pd.DataFrame()
    confidence = pd.to_numeric(losing["confidence"], errors="coerce")
    directional = pd.to_numeric(losing["directional_confidence"], errors="coerce")
    alternatives: list[tuple[str, pd.Series]] = [
        ("higher_confidence_threshold", np.where(confidence >= 0.55, losing["pnl"], 0.0)),
        ("lower_confidence_threshold", np.where(confidence >= 0.35, losing["pnl"], 0.0)),
        ("different_regime_filter", np.where(losing["market_regime"].astype(str) == "volatile_trend", losing["pnl"], 0.0)),
        ("trade_skipped", np.zeros(len(losing))),
        ("different_stop_loss", counterfactual_strategy_pnl(
            losing,
            reward_points=config.labels.directional_target_points,
            stop_points=config.labels.directional_target_points,
            risk_multiplier=1.0,
            trailing_stop=False,
            partial_exit=0.0,
            config=config,
        )),
        ("different_target", counterfactual_strategy_pnl(
            losing,
            reward_points=config.labels.directional_target_points * 0.75,
            stop_points=config.labels.directional_target_points * 1.5,
            risk_multiplier=1.0,
            trailing_stop=False,
            partial_exit=0.0,
            config=config,
        )),
        ("trailing_stop", counterfactual_strategy_pnl(
            losing,
            reward_points=config.labels.directional_target_points,
            stop_points=config.labels.directional_target_points * 2.0,
            risk_multiplier=1.0,
            trailing_stop=True,
            partial_exit=0.0,
            config=config,
        )),
        ("partial_profit_booking", counterfactual_strategy_pnl(
            losing,
            reward_points=config.labels.directional_target_points,
            stop_points=config.labels.directional_target_points * 2.0,
            risk_multiplier=1.0,
            trailing_stop=False,
            partial_exit=0.5,
            config=config,
        )),
        ("directional_confidence_filter", np.where(directional >= 0.50, losing["pnl"], 0.0)),
    ]
    alt_frame = pd.DataFrame(
        {name: pd.to_numeric(values, errors="coerce") for name, values in alternatives},
        index=losing.index,
    )
    best_name = alt_frame.idxmax(axis=1)
    best_value = alt_frame.max(axis=1)
    rows = losing[
        [
            "trade_id",
            "date",
            "side",
            "pnl",
            "market_regime",
            "confidence",
            "directional_confidence",
            "mfe_points",
            "mae_points",
            "target_hit",
            "stop_hit",
        ]
    ].copy()
    rows["best_counterfactual"] = best_name.values
    rows["best_counterfactual_pnl"] = best_value.values
    rows["counterfactual_improvement"] = rows["best_counterfactual_pnl"] - pd.to_numeric(rows["pnl"], errors="coerce")
    rows["should_skip"] = rows["best_counterfactual"].isin(
        ["trade_skipped", "higher_confidence_threshold", "different_regime_filter", "directional_confidence_filter"]
    )
    for name in alt_frame.columns:
        rows[f"pnl_if_{name}"] = alt_frame[name].values
    return rows.sort_values(["counterfactual_improvement", "pnl"], ascending=[False, True]).reset_index(drop=True)


def feature_summary_for_trade(
    row: pd.Series,
    features: list[str],
    quantiles: dict[str, tuple[float, float]],
) -> str:
    pieces: list[str] = []
    for feature in features[:8]:
        if feature not in row or pd.isna(row[feature]):
            continue
        value = float(row[feature])
        low, high = quantiles.get(feature, (float("-inf"), float("inf")))
        bucket = "low" if value <= low else "high" if value >= high else "normal"
        pieces.append(f"{feature}={value:.4g}({bucket})")
    return "; ".join(pieces) if pieces else "feature explanation unavailable"


def feature_quantiles(trades: pd.DataFrame, features: list[str]) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for feature in features:
        if feature not in trades:
            continue
        values = pd.to_numeric(trades[feature], errors="coerce").dropna()
        if not values.empty:
            result[feature] = (float(values.quantile(0.25)), float(values.quantile(0.75)))
    return result


def trade_replay_intelligence(
    trades: pd.DataFrame,
    counterfactuals: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    cf_lookup = counterfactuals.set_index("trade_id") if not counterfactuals.empty else pd.DataFrame()
    importance = load_feature_importance(config)
    default_features = [
        item
        for item in ["atr", "ema50", "mins_since_open", "ema20", "price_vs_vwap", "volatility", "rsi", "adx"]
        if item in trades.columns
    ] or FEATURE_COLUMNS[:8]
    feature_cache: dict[str, tuple[list[str], dict[str, tuple[float, float]]]] = {}
    rows: list[dict[str, object]] = []
    for _, row in trades.iterrows():
        side = str(row["side"])
        target = f"quality_profitable_{side}"
        if target not in feature_cache:
            features = importance.get(target) or importance.get(f"directional_{side}") or importance.get("directional_ce") or default_features
            feature_cache[target] = (features, feature_quantiles(trades, features))
        features, quantiles = feature_cache[target]
        cf = cf_lookup.loc[row["trade_id"]] if not cf_lookup.empty and row["trade_id"] in cf_lookup.index else None
        cf_name = "not_applicable_win"
        cf_pnl = float(row["pnl"])
        should_skip = False
        if cf is not None:
            cf_name = str(cf["best_counterfactual"])
            cf_pnl = float(cf["best_counterfactual_pnl"])
            should_skip = bool(cf["should_skip"])
        elif float(row["pnl"]) < 0:
            should_skip = True
        risk = float(pd.to_numeric(pd.Series([row.get("drawdown_rs", 0.0)]), errors="coerce").iloc[0])
        rows.append(
            {
                "trade_id": row["trade_id"],
                "date": row["date"],
                "entry_reason": why_trade_taken(row),
                "exit_reason": why_trade_won_or_lost(row),
                "shap_explanation": feature_summary_for_trade(row, features, quantiles),
                "regime": row.get("market_regime"),
                "confidence": row.get("confidence"),
                "risk": risk,
                "counterfactual_outcome": cf_name,
                "counterfactual_pnl": cf_pnl,
                "counterfactual_improvement": cf_pnl - float(row["pnl"]),
                "should_trade_have_been_skipped": should_skip,
                "primary_failure": primary_failure_reason(row) if float(row["pnl"]) < 0 else "not_applicable_win",
            }
        )
    return pd.DataFrame(rows)


def numeric_quantiles(series: pd.Series, quantiles: tuple[float, ...]) -> list[float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return []
    result = sorted({float(values.quantile(q)) for q in quantiles})
    return [value for value in result if math.isfinite(value)]


def stage55_specific_filter_candidates(trades: pd.DataFrame) -> list[FilterCandidate]:
    candidates: list[FilterCandidate] = []
    contexts = {"all": pd.Series(True, index=trades.index)}
    for side in ("ce", "pe"):
        contexts[side] = trades["side"].astype(str).str.lower() == side

    close = pd.to_numeric(trades.get("close"), errors="coerce").replace(0, np.nan).abs()
    ema20 = pd.to_numeric(trades.get("ema20"), errors="coerce")
    ema50 = pd.to_numeric(trades.get("ema50"), errors="coerce")
    ema_distance = ((pd.to_numeric(trades.get("close"), errors="coerce") - ema20).abs() / close).replace(
        [np.inf, -np.inf], np.nan
    )
    ema_spread = ((ema20 - ema50).abs() / close).replace([np.inf, -np.inf], np.nan)
    vwap_distance = pd.to_numeric(trades.get("price_vs_vwap"), errors="coerce")
    abs_vwap_distance = vwap_distance.abs()
    day_values = trades.get("day_of_week", pd.Series("", index=trades.index)).astype(str)

    for context, context_mask in contexts.items():
        prefix = f"{context}:"
        ema_q = numeric_quantiles(ema_distance[context_mask], (0.25, 0.50, 0.75))
        if ema_q:
            q25, q50, q75 = ema_q[0], ema_q[len(ema_q) // 2], ema_q[-1]
            candidates.extend(
                [
                    FilterCandidate(
                        key=f"{prefix}ema_distance_below_{q75:.6f}",
                        category="ema_distance_filter",
                        description=f"{context} require EMA20 distance <= {q75:.6f}",
                        context=context,
                        mask=context_mask & (ema_distance <= q75),
                    ),
                    FilterCandidate(
                        key=f"{prefix}ema_distance_above_{q25:.6f}",
                        category="ema_distance_filter",
                        description=f"{context} require EMA20 distance >= {q25:.6f}",
                        context=context,
                        mask=context_mask & (ema_distance >= q25),
                    ),
                    FilterCandidate(
                        key=f"{prefix}ema_distance_mid_{q25:.6f}_{q75:.6f}",
                        category="ema_distance_filter",
                        description=f"{context} require EMA20 distance between {q25:.6f} and {q75:.6f}",
                        context=context,
                        mask=context_mask & ema_distance.between(q25, q75, inclusive="both"),
                    ),
                    FilterCandidate(
                        key=f"{prefix}ema_spread_above_{q50:.6f}",
                        category="ema_distance_filter",
                        description=f"{context} require EMA20/EMA50 spread >= {q50:.6f}",
                        context=context,
                        mask=context_mask & (ema_spread >= q50),
                    ),
                ]
            )

        vwap_q = numeric_quantiles(abs_vwap_distance[context_mask], (0.25, 0.50, 0.75))
        if vwap_q:
            q25, q50, q75 = vwap_q[0], vwap_q[len(vwap_q) // 2], vwap_q[-1]
            candidates.extend(
                [
                    FilterCandidate(
                        key=f"{prefix}vwap_distance_below_{q75:.6f}",
                        category="vwap_distance_filter",
                        description=f"{context} require absolute VWAP distance <= {q75:.6f}",
                        context=context,
                        mask=context_mask & (abs_vwap_distance <= q75),
                    ),
                    FilterCandidate(
                        key=f"{prefix}vwap_distance_above_{q25:.6f}",
                        category="vwap_distance_filter",
                        description=f"{context} require absolute VWAP distance >= {q25:.6f}",
                        context=context,
                        mask=context_mask & (abs_vwap_distance >= q25),
                    ),
                    FilterCandidate(
                        key=f"{prefix}vwap_distance_mid_{q25:.6f}_{q75:.6f}",
                        category="vwap_distance_filter",
                        description=f"{context} require absolute VWAP distance between {q25:.6f} and {q75:.6f}",
                        context=context,
                        mask=context_mask & abs_vwap_distance.between(q25, q75, inclusive="both"),
                    ),
                    FilterCandidate(
                        key=f"{prefix}vwap_distance_above_{q50:.6f}",
                        category="vwap_distance_filter",
                        description=f"{context} require absolute VWAP distance >= {q50:.6f}",
                        context=context,
                        mask=context_mask & (abs_vwap_distance >= q50),
                    ),
                ]
            )

        if context == "all":
            aligned = (
                ((trades["side"].astype(str).str.lower() == "ce") & (vwap_distance >= 0.0))
                | ((trades["side"].astype(str).str.lower() == "pe") & (vwap_distance <= 0.0))
            )
        elif context == "ce":
            aligned = vwap_distance >= 0.0
        else:
            aligned = vwap_distance <= 0.0
        candidates.append(
            FilterCandidate(
                key=f"{prefix}vwap_side_aligned",
                category="vwap_distance_filter",
                description=f"{context} require side-aligned VWAP distance",
                context=context,
                mask=context_mask & aligned,
            )
        )

        for day in sorted(day_values[context_mask].dropna().unique()):
            if not day:
                continue
            day_mask = day_values == day
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}exclude_day_{day.lower()}",
                    category="day_filter",
                    description=f"{context} exclude {day}",
                    context=context,
                    mask=context_mask & ~day_mask,
                )
            )
            candidates.append(
                FilterCandidate(
                    key=f"{prefix}only_day_{day.lower()}",
                    category="day_filter",
                    description=f"{context} trade only {day}",
                    context=context,
                    mask=context_mask & day_mask,
                )
            )
    return candidates


def stage55_filter_candidates(trades: pd.DataFrame) -> list[FilterCandidate]:
    seen: set[str] = set()
    candidates: list[FilterCandidate] = []
    for candidate in [*build_filter_candidates(trades), *stage55_specific_filter_candidates(trades)]:
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        candidates.append(candidate)
    return candidates


def automatic_filter_ranking(
    trades: pd.DataFrame,
    min_trades: int,
    min_trade_coverage: float,
) -> pd.DataFrame:
    baseline_pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna()
    baseline = metrics_from_pnl(baseline_pnl)
    rows: list[dict[str, object]] = []
    for candidate in stage55_filter_candidates(trades):
        keep_mask = strategy_keep_mask(trades, candidate)
        pnl = pd.to_numeric(trades.loc[keep_mask, "pnl"], errors="coerce").dropna()
        row = validation_result(
            trades,
            pnl=pnl,
            baseline_pnl=baseline_pnl,
            baseline=baseline,
            min_trades=min_trades,
            min_trade_coverage=min_trade_coverage,
        )
        trade_reduction = 1.0 - float(row["trade_coverage"])
        baseline_dd = abs(float(baseline.get("max_drawdown", 0.0)))
        risk_reduction = (
            (baseline_dd - abs(float(row["max_drawdown"]))) / baseline_dd
            if baseline_dd > 0
            else 0.0
        )
        score = (
            float(row["net_pnl_delta"]) / 1_000_000.0
            + float(row["expectancy_lift_ci_low"]) / 100.0
            + risk_reduction
            - max(0.0, trade_reduction - 0.75)
        )
        rows.append(
            {
                "filter_key": candidate.key,
                "category": candidate.category,
                "context": candidate.context,
                "description": candidate.description,
                "net_profitability_improvement": row["net_pnl_delta"],
                "statistical_confidence": max(0.0, 1.0 - float(row["p_value_approx"])),
                "trade_reduction": trade_reduction,
                "risk_reduction": risk_reduction,
                "rank_score": score,
                **row,
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(
        ["statistically_acceptable", "rank_score", "net_profitability_improvement"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def recommendation_payload(
    single: pd.DataFrame,
    combinations: pd.DataFrame,
    filters: pd.DataFrame,
    baseline: dict[str, float],
    min_trade_coverage: float,
) -> dict[str, object]:
    accepted_single = single[single["statistically_acceptable"].astype(bool)].copy() if not single.empty else single
    accepted_combo = (
        combinations[combinations["statistically_acceptable"].astype(bool)].copy()
        if not combinations.empty
        else combinations
    )
    accepted_filters = filters[filters["statistically_acceptable"].astype(bool)].copy() if not filters.empty else filters

    best_combo = accepted_combo.head(1).to_dict("records")
    best_single = accepted_single.head(10).to_dict("records")
    top_filters = accepted_filters.head(10).to_dict("records")

    supporting_standalone_filters: list[dict[str, object]] = []
    for row in top_filters:
        supporting_standalone_filters.append(
            {
                "filter_key": row.get("filter_key"),
                "category": row.get("category"),
                "context": row.get("context"),
                "description": row.get("description"),
                "trade_count": row.get("trade_count"),
                "profit_factor": row.get("profit_factor"),
                "expectancy": row.get("expectancy"),
                "net_pnl_delta": row.get("net_pnl_delta"),
                "expectancy_lift_ci_low": row.get("expectancy_lift_ci_low"),
            }
        )

    expected_improvement = {}
    confidence = "none"
    recommended_filters: list[dict[str, object]] = []
    recommended_thresholds: list[dict[str, object]] = []
    if best_combo:
        row = best_combo[0]
        expected_improvement = {
            "source": "combination_optimizer",
            "actions": row.get("actions"),
            "trade_count": row.get("trade_count"),
            "net_pnl": row.get("net_pnl"),
            "net_pnl_delta": row.get("net_pnl_delta"),
            "profit_factor": row.get("profit_factor"),
            "profit_factor_delta": row.get("profit_factor_delta"),
            "expectancy": row.get("expectancy"),
            "expectancy_delta": row.get("expectancy_delta"),
            "max_drawdown": row.get("max_drawdown"),
            "trade_coverage": row.get("trade_coverage"),
        }
        confidence = "high" if float(row.get("p_value_approx", 1.0)) <= 0.01 else "medium"
        for index, action in enumerate(str(row.get("actions", "")).split(" | "), start=1):
            if not action:
                continue
            recommended_filters.append(
                {
                    "component": index,
                    "source": "recommended_combination",
                    "description": action,
                    "combination_key": row.get("combination_key"),
                    "combination_profit_factor": row.get("profit_factor"),
                    "combination_expectancy": row.get("expectancy"),
                    "combination_trade_count": row.get("trade_count"),
                    "expectancy_lift_ci_low": row.get("expectancy_lift_ci_low"),
                }
            )
            if ">=" in action or "<=" in action or "between" in action:
                recommended_thresholds.append(
                    {
                        "source": "recommended_combination",
                        "threshold_rule": action,
                        "confidence": confidence,
                    }
                )
    elif best_single:
        row = best_single[0]
        expected_improvement = {
            "source": "single_recommendation_validation",
            "action": row.get("action"),
            "trade_count": row.get("trade_count"),
            "net_pnl": row.get("net_pnl"),
            "net_pnl_delta": row.get("net_pnl_delta"),
            "profit_factor": row.get("profit_factor"),
            "profit_factor_delta": row.get("profit_factor_delta"),
            "expectancy": row.get("expectancy"),
            "expectancy_delta": row.get("expectancy_delta"),
            "max_drawdown": row.get("max_drawdown"),
            "trade_coverage": row.get("trade_coverage"),
        }
        confidence = "high" if float(row.get("p_value_approx", 1.0)) <= 0.01 else "medium"
        action = str(row.get("action", ""))
        recommended_filters.append(
            {
                "component": 1,
                "source": "single_recommendation_validation",
                "description": action,
                "profit_factor": row.get("profit_factor"),
                "expectancy": row.get("expectancy"),
                "trade_count": row.get("trade_count"),
                "expectancy_lift_ci_low": row.get("expectancy_lift_ci_low"),
            }
        )
        if ">=" in action or "<=" in action or "between" in action:
            recommended_thresholds.append(
                {
                    "source": "single_recommendation_validation",
                    "threshold_rule": action,
                    "confidence": confidence,
                }
            )

    risks = [
        "Offline replay uses reconstructed V2 trade outcomes, not live execution fills.",
        "Counterfactual exit logic is estimated from MFE/MAE path summaries, not full tick-level path ordering.",
        f"Final gates require at least {min_trade_coverage:.0%} baseline trade coverage to reduce overfit risk.",
        "Recommendations remain research-only until validated in paper trading or live shadow mode.",
    ]

    return {
        "schema_version": PHASE55_SCHEMA_VERSION,
        "status": "ok" if expected_improvement else "no_promotable_change",
        "baseline": baseline,
        "recommended_filters": recommended_filters,
        "recommended_thresholds": recommended_thresholds,
        "supporting_standalone_filters": supporting_standalone_filters,
        "recommended_combination": best_combo[0] if best_combo else None,
        "recommended_single_changes": best_single,
        "expected_improvement": expected_improvement,
        "confidence": confidence,
        "potential_risks": risks,
        "promotion_rules": {
            "positive_statistical_significance": True,
            "improved_profit_factor": True,
            "positive_expectancy_improvement": True,
            "minimum_trade_coverage": min_trade_coverage,
            "research_only": True,
        },
    }


def write_improvement_markdown(
    baseline: dict[str, float],
    single: pd.DataFrame,
    combinations: pd.DataFrame,
    final_payload: dict[str, object],
    path: Path,
) -> None:
    lines = [
        "# Phase 5.5 Improvement Report",
        "",
        "Generated from existing Phase 5 offline artifacts only.",
        "",
        "## Baseline",
        "",
        f"- Trades: {int(baseline.get('trades', 0))}",
        f"- Net PnL: {float(baseline.get('net_pnl', 0.0)):.2f}",
        f"- Profit Factor: {float(baseline.get('profit_factor', 0.0)):.4f}",
        f"- Win Rate: {float(baseline.get('win_rate', 0.0)):.2%}",
        f"- Expectancy: {float(baseline.get('expectancy', 0.0)):.2f}",
        f"- Max Drawdown: {float(baseline.get('max_drawdown', 0.0)):.2f}",
        "",
        "## Top Single Recommendation Tests",
        "",
    ]
    for row in single.head(10).itertuples(index=False):
        lines.append(
            f"- {row.action}: trades={row.trade_count}, PF={row.profit_factor:.4f}, "
            f"expectancy={row.expectancy:.2f}, net PnL={row.net_pnl:.2f}, "
            f"accepted={bool(row.statistically_acceptable)}"
        )
    lines.extend(["", "## Top Combinations", ""])
    for row in combinations.head(10).itertuples(index=False):
        lines.append(
            f"- {row.actions}: trades={row.trade_count}, PF={row.profit_factor:.4f}, "
            f"expectancy={row.expectancy:.2f}, net PnL={row.net_pnl:.2f}, "
            f"coverage={row.trade_coverage:.2%}"
        )
    lines.extend(["", "## Final Recommendation", ""])
    expected = final_payload.get("expected_improvement") or {}
    if expected:
        lines.append(f"- Source: {expected.get('source')}")
        lines.append(f"- Expected PF: {float(expected.get('profit_factor', 0.0)):.4f}")
        lines.append(f"- Expected expectancy: {float(expected.get('expectancy', 0.0)):.2f}")
        lines.append(f"- Expected net PnL: {float(expected.get('net_pnl', 0.0)):.2f}")
        lines.append(f"- Confidence: {final_payload.get('confidence')}")
    else:
        lines.append("- No recommendation passed all Phase 5.5 gates.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_phase55_autonomous_strategy_improvement(
    config: PipelineConfig,
    min_trades: int = 200,
    min_trade_coverage: float = 0.20,
    max_combination_size: int = 3,
) -> dict[str, object]:
    paths = phase55_paths(config)
    trades = load_phase5_trades(config)
    recommendations = load_phase5_recommendations(config)
    phase5_summary = load_phase5_summary(config)
    candidates = recommendation_candidates(trades, recommendations)

    baseline_pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna()
    baseline = metrics_from_pnl(baseline_pnl)

    single = single_recommendation_validation(
        trades,
        candidates,
        config=config,
        min_trades=min_trades,
        min_trade_coverage=min_trade_coverage,
    )
    write_csv(single, paths.validation / "single_recommendation_validation.csv")
    write_json(paths.validation / "single_recommendation_validation.json", json_records(single))

    combinations = combination_optimizer(
        trades,
        candidates,
        config=config,
        min_trades=min_trades,
        min_trade_coverage=min_trade_coverage,
        max_combination_size=max_combination_size,
    )
    write_csv(combinations, paths.combinations / "combination_optimizer.csv")
    write_json(paths.combinations / "combination_optimizer.json", json_records(combinations.head(100)))

    counterfactuals = counterfactual_alternatives(trades, config=config)
    write_csv(counterfactuals, paths.counterfactuals / "counterfactual_losing_trades.csv")
    write_json(paths.counterfactuals / "counterfactual_losing_trades_sample.json", json_records(counterfactuals.head(500)))

    replay = trade_replay_intelligence(trades, counterfactuals, config=config)
    write_csv(replay, paths.replay / "trade_replay_intelligence.csv")
    write_json(paths.replay / "trade_replay_intelligence_sample.json", json_records(replay.head(500)))

    filter_ranking = automatic_filter_ranking(
        trades,
        min_trades=min_trades,
        min_trade_coverage=min_trade_coverage,
    )
    write_csv(filter_ranking, paths.filters / "filter_ranking.csv")
    write_json(paths.filters / "filter_ranking.json", json_records(filter_ranking.head(100)))

    final_payload = recommendation_payload(
        single=single,
        combinations=combinations,
        filters=filter_ranking,
        baseline=baseline,
        min_trade_coverage=min_trade_coverage,
    )
    write_json(paths.final / "recommended_strategy.json", final_payload)
    write_improvement_markdown(
        baseline=baseline,
        single=single,
        combinations=combinations,
        final_payload=final_payload,
        path=paths.base / "improvement_report.md",
    )

    summary = {
        "schema_version": PHASE55_SCHEMA_VERSION,
        "status": "ok",
        "phase": 5.5,
        "controls": {
            "experimental_only": True,
            "production_files_modified": False,
            "live_engine_integrated": False,
            "retrained_models": False,
            "reused_phase5_artifacts": True,
        },
        "phase5_baseline": phase5_summary.get("baseline", baseline),
        "baseline": baseline,
        "candidate_recommendations": int(len(candidates)),
        "single_tests": int(len(single)),
        "single_accepted": int(single["statistically_acceptable"].sum()) if not single.empty else 0,
        "combination_tests_ranked": int(len(combinations)),
        "combination_accepted": int(combinations["statistically_acceptable"].sum()) if not combinations.empty else 0,
        "losing_trade_counterfactuals": int(len(counterfactuals)),
        "replay_records": int(len(replay)),
        "filter_tests_ranked": int(len(filter_ranking)),
        "filter_accepted": int(filter_ranking["statistically_acceptable"].sum()) if not filter_ranking.empty else 0,
        "recommended_strategy_status": final_payload.get("status"),
        "recommended_strategy": final_payload.get("expected_improvement"),
        "reports": {
            "single_recommendation_validation": str(paths.validation),
            "combination_optimizer": str(paths.combinations),
            "counterfactual_trade_simulator": str(paths.counterfactuals),
            "trade_replay_intelligence": str(paths.replay),
            "filter_ranking": str(paths.filters),
            "final_recommendation_engine": str(paths.final),
            "improvement_report": str(paths.base / "improvement_report.md"),
        },
    }
    write_json(paths.base / "phase55_summary.json", summary)
    return summary
