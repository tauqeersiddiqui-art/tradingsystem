from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline_v2.config import PipelineConfig
from ml_pipeline_v2.validation import trade_metrics


BINARY_TARGET_SIDES: dict[str, str] = {
    "directional_ce": "ce",
    "directional_pe": "pe",
    "quality_profitable_ce": "ce",
    "quality_profitable_pe": "pe",
    "target_hit_ce": "ce",
    "target_hit_pe": "pe",
    "stop_hit_ce": "ce",
    "stop_hit_pe": "pe",
}

HIGHER_IS_BETTER = {
    "auc": True,
    "average_precision": True,
    "pr_auc": True,
    "expectancy": True,
    "profit_factor": True,
    "net_pnl": True,
    "precision": True,
    "recall": True,
    "trade_count": True,
    "ece": False,
    "brier": False,
    "max_drawdown": True,
}


@dataclass(frozen=True)
class Phase4Paths:
    base: Path
    recommendations: Path
    risk: Path
    ensembles: Path
    thresholds: Path
    promotion: Path


def phase4_paths(config: PipelineConfig) -> Phase4Paths:
    base = config.paths.output_dir / "reports" / "phase4"
    paths = Phase4Paths(
        base=base,
        recommendations=base / "recommendations",
        risk=base / "risk",
        ensembles=base / "ensembles",
        thresholds=base / "thresholds",
        promotion=base / "promotion",
    )
    for path in (
        paths.base,
        paths.recommendations,
        paths.risk,
        paths.ensembles,
        paths.thresholds,
        paths.promotion,
    ):
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


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def normalize_metric(values: pd.Series, higher_is_better: bool) -> pd.Series:
    values = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if values.notna().sum() == 0:
        return pd.Series(0.0, index=values.index)
    filled = values.fillna(values.median())
    min_value = float(filled.min())
    max_value = float(filled.max())
    if math.isclose(min_value, max_value):
        return pd.Series(0.5, index=values.index)
    score = (filled - min_value) / (max_value - min_value)
    return score if higher_is_better else 1.0 - score


def candidate_artifact_path(config: PipelineConfig, target: str, model: str) -> Path:
    return config.paths.output_dir / "models" / f"v2_{target}_{model}.joblib"


def score_candidates(comparison: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    ok = comparison[comparison.get("status") == "ok"].copy()
    if ok.empty:
        return ok

    for column in HIGHER_IS_BETTER:
        if column in ok:
            ok[column] = pd.to_numeric(ok[column], errors="coerce")

    metric_weights = {
        "auc": 0.16,
        "average_precision": 0.13,
        "expectancy": 0.20,
        "profit_factor": 0.13,
        "net_pnl": 0.10,
        "ece": 0.10,
        "brier": 0.06,
        "precision": 0.07,
        "trade_count": 0.05,
    }
    score = pd.Series(0.0, index=ok.index)
    used_weight = 0.0
    for metric, weight in metric_weights.items():
        if metric not in ok:
            continue
        score += normalize_metric(ok[metric], HIGHER_IS_BETTER[metric]) * weight
        used_weight += weight
    if used_weight > 0:
        score = score / used_weight

    ok["selection_score"] = score
    ok["side"] = ok["target"].map(BINARY_TARGET_SIDES).fillna("unknown")
    ok["model_artifact_path"] = [
        str(candidate_artifact_path(config, str(row.target), str(row.model)))
        for row in ok.itertuples(index=False)
    ]
    ok["artifact_available"] = [
        candidate_artifact_path(config, str(row.target), str(row.model)).exists()
        for row in ok.itertuples(index=False)
    ]
    ok["meets_expectancy_floor"] = ok["expectancy"].fillna(float("-inf")) >= 0.0
    ok["meets_profit_factor_floor"] = ok["profit_factor"].fillna(0.0) >= 1.05
    ok["meets_calibration_floor"] = ok["ece"].fillna(1.0) <= 0.05
    ok["meets_trade_count_floor"] = ok["trade_count"].fillna(0.0) >= 100
    ok["metric_viable"] = (
        ok["meets_expectancy_floor"]
        & ok["meets_profit_factor_floor"]
        & ok["meets_calibration_floor"]
        & ok["meets_trade_count_floor"]
    )
    ok["production_viable"] = ok["metric_viable"] & ok["artifact_available"]
    ok = ok.sort_values(["target", "selection_score"], ascending=[True, False])
    ok["rank_for_target"] = ok.groupby("target").cumcount() + 1
    ok["production_rank_for_target"] = (
        ok.sort_values(["target", "production_viable", "selection_score"], ascending=[True, False, False])
        .groupby("target")
        .cumcount()
        + 1
    )
    ok = ok.sort_values("selection_score", ascending=False).reset_index(drop=True)
    ok["global_rank"] = np.arange(1, len(ok) + 1)
    return ok


def champion_challenger_report(scored: pd.DataFrame, paths: Phase4Paths) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidate_pool = scored[scored["artifact_available"]].copy()
    if candidate_pool.empty:
        candidate_pool = scored.copy()
    champions = candidate_pool.sort_values(["target", "selection_score"], ascending=[True, False])
    champions = champions.groupby("target", as_index=False).head(1).copy()
    champion_keys = set(zip(champions["target"], champions["model"]))
    challengers = scored[
        ~scored.apply(lambda row: (row["target"], row["model"]) in champion_keys, axis=1)
    ].copy()
    challengers = challengers.sort_values(["target", "selection_score"], ascending=[True, False])
    challengers = challengers.groupby("target", as_index=False).head(2).copy()

    champions.to_csv(paths.recommendations / "champions.csv", index=False)
    challengers.to_csv(paths.recommendations / "challengers.csv", index=False)
    write_json(paths.recommendations / "champions.json", champions.to_dict("records"))
    write_json(paths.recommendations / "challengers.json", challengers.to_dict("records"))
    return champions, challengers


def build_ensemble_report(scored: pd.DataFrame, paths: Phase4Paths) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for target, group in scored.groupby("target"):
        viable = group[group["production_viable"]].copy()
        pool_type = "promotable" if len(viable) >= 2 else "research_only"
        pool = viable if len(viable) >= 2 else group[group["metric_viable"]].copy()
        if pool.empty:
            pool = group.copy()
        pool = pool.sort_values("selection_score", ascending=False).head(3)
        if pool.empty:
            continue
        weights_raw = np.maximum(pool["selection_score"].to_numpy(dtype=float), 0.001)
        weights = weights_raw / weights_raw.sum()
        weighted_metrics: dict[str, float] = {}
        for metric in ("auc", "average_precision", "ece", "brier", "expectancy", "profit_factor", "net_pnl"):
            if metric in pool:
                values = pd.to_numeric(pool[metric], errors="coerce").fillna(0.0).to_numpy(dtype=float)
                weighted_metrics[f"weighted_{metric}"] = float(np.dot(values, weights))
        rows.append(
            {
                "target": target,
                "side": BINARY_TARGET_SIDES.get(target, "unknown"),
                "ensemble_type": pool_type,
                "ensemble_size": int(len(pool)),
                "members": [
                    {
                        "model": row.model,
                        "weight": float(weight),
                        "selection_score": float(row.selection_score),
                        "artifact_available": bool(row.artifact_available),
                    }
                    for row, weight in zip(pool.itertuples(index=False), weights)
                ],
                "recommended": bool(
                    pool_type == "promotable"
                    and len(pool) >= 2
                    and weighted_metrics.get("weighted_expectancy", 0.0) > 0.0
                ),
                **weighted_metrics,
            }
        )
    ensemble = pd.DataFrame(rows)
    ensemble.to_csv(paths.ensembles / "ensemble_recommendations.csv", index=False)
    write_json(paths.ensembles / "ensemble_recommendations.json", rows)
    return ensemble


def dynamic_threshold_report(scored: pd.DataFrame, paths: Phase4Paths) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in scored.itertuples(index=False):
        threshold = getattr(row, "threshold", np.nan)
        ece = float(getattr(row, "ece", np.nan)) if pd.notna(getattr(row, "ece", np.nan)) else 0.05
        expectancy = float(getattr(row, "expectancy", 0.0))
        profit_factor = float(getattr(row, "profit_factor", 0.0))
        if pd.isna(threshold):
            threshold = 0.50

        drift_buffer = min(0.08, max(0.0, ece - 0.02))
        risk_buffer = 0.03 if expectancy < 150.0 or profit_factor < 1.20 else 0.0
        aggressive = max(0.01, float(threshold) - 0.03)
        balanced = min(0.99, max(0.01, float(threshold) + drift_buffer + risk_buffer))
        defensive = min(0.99, balanced + 0.05)
        rows.append(
            {
                "model": row.model,
                "target": row.target,
                "side": row.side,
                "base_threshold": float(threshold),
                "aggressive_threshold": float(aggressive),
                "balanced_threshold": float(balanced),
                "defensive_threshold": float(defensive),
                "recommended_threshold": float(balanced),
                "threshold_policy": "balanced_drift_and_risk_buffer",
                "ece_buffer": float(drift_buffer),
                "risk_buffer": float(risk_buffer),
            }
        )
    thresholds = pd.DataFrame(rows)
    thresholds.to_csv(paths.thresholds / "dynamic_thresholds.csv", index=False)
    write_json(paths.thresholds / "dynamic_thresholds.json", rows)
    return thresholds


def pnl_distribution_from_metrics(row: pd.Series) -> np.ndarray:
    trades = int(max(0, row.get("trade_count", 0) or 0))
    if trades == 0:
        return np.array([], dtype=float)

    expectancy = float(row.get("expectancy", 0.0) or 0.0)
    net_pnl = float(row.get("net_pnl", expectancy * trades) or 0.0)
    if not math.isclose(expectancy * trades, net_pnl, rel_tol=0.25, abs_tol=1000.0):
        expectancy = net_pnl / trades

    precision = float(row.get("precision", np.nan))
    if not np.isfinite(precision):
        precision = 0.45 if expectancy >= 0 else 0.35
    win_rate = min(0.85, max(0.05, precision))
    profit_factor = float(row.get("profit_factor", np.nan))
    if not np.isfinite(profit_factor) or profit_factor <= 0:
        profit_factor = 1.0 if expectancy >= 0 else 0.75

    loss_abs = max(50.0, abs(expectancy) + 100.0)
    denominator = max(1e-6, win_rate * profit_factor - (1.0 - win_rate))
    if denominator > 0:
        loss_abs = max(50.0, expectancy / denominator)
    win_amount = max(25.0, profit_factor * (1.0 - win_rate) * loss_abs / max(win_rate, 1e-6))

    wins = int(round(trades * win_rate))
    losses = max(0, trades - wins)
    pnl = np.concatenate(
        [
            np.full(wins, win_amount, dtype=float),
            np.full(losses, -loss_abs, dtype=float),
        ]
    )
    if len(pnl):
        pnl += expectancy - float(pnl.mean())
    return pnl


def monte_carlo_distribution(
    pnl: np.ndarray,
    start_capital: float,
    ruin_fraction: float,
    runs: int,
    seed: int,
) -> dict[str, float]:
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return {
            "runs": int(runs),
            "risk_of_ruin": 0.0,
            "p01_final_equity": start_capital,
            "p05_final_equity": start_capital,
            "median_final_equity": start_capital,
            "p95_final_equity": start_capital,
            "expected_final_equity": start_capital,
            "p05_max_drawdown": 0.0,
            "median_max_drawdown": 0.0,
        }
    rng = np.random.default_rng(seed)
    ruin_level = start_capital * (1.0 - ruin_fraction)
    final_equities: list[float] = []
    max_drawdowns: list[float] = []
    ruined = 0
    for _ in range(runs):
        sample = rng.choice(pnl, size=len(pnl), replace=True)
        equity = start_capital + sample.cumsum()
        peak = np.maximum.accumulate(equity)
        drawdown = equity - peak
        final_equities.append(float(equity[-1]))
        max_drawdowns.append(float(drawdown.min()))
        if float(equity.min()) <= ruin_level:
            ruined += 1
    return {
        "runs": int(runs),
        "risk_of_ruin": float(ruined / runs),
        "p01_final_equity": float(np.quantile(final_equities, 0.01)),
        "p05_final_equity": float(np.quantile(final_equities, 0.05)),
        "median_final_equity": float(np.quantile(final_equities, 0.50)),
        "p95_final_equity": float(np.quantile(final_equities, 0.95)),
        "expected_final_equity": float(np.mean(final_equities)),
        "p05_max_drawdown": float(np.quantile(max_drawdowns, 0.05)),
        "median_max_drawdown": float(np.quantile(max_drawdowns, 0.50)),
    }


def risk_reports(
    scored: pd.DataFrame,
    config: PipelineConfig,
    paths: Phase4Paths,
    start_capital: float,
    ruin_fraction: float,
    runs: int,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index, row in scored.reset_index(drop=True).iterrows():
        pnl = pnl_distribution_from_metrics(row)
        realized = trade_metrics(pnl)
        mc = monte_carlo_distribution(
            pnl,
            start_capital=start_capital,
            ruin_fraction=ruin_fraction,
            runs=runs,
            seed=config.validation.random_seed + int(index),
        )
        risk_of_ruin_pct = 100.0 * float(mc["risk_of_ruin"])
        rows.append(
            {
                "model": row["model"],
                "target": row["target"],
                "side": row["side"],
                "selection_score": float(row["selection_score"]),
                "trades": realized["trades"],
                "synthetic_expectancy": realized["expectancy"],
                "synthetic_profit_factor": realized["profit_factor"],
                "synthetic_max_drawdown": realized["max_drawdown"],
                "risk_of_ruin_pct": risk_of_ruin_pct,
                "risk_gate_passed": bool(risk_of_ruin_pct <= config.risk.max_risk_of_ruin_pct),
                "drawdown_gate_passed": bool(
                    abs(float(mc["p05_max_drawdown"])) <= start_capital * config.risk.max_daily_loss_pct / 100.0
                ),
                **mc,
            }
        )
    risk = pd.DataFrame(rows)
    risk.to_csv(paths.risk / "monte_carlo_risk.csv", index=False)
    write_json(paths.risk / "monte_carlo_risk.json", rows)
    return risk


def target_quality_rows(
    champions: pd.DataFrame,
    thresholds: pd.DataFrame,
    risk: pd.DataFrame,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for champion in champions.itertuples(index=False):
        threshold = thresholds[
            (thresholds["model"] == champion.model) & (thresholds["target"] == champion.target)
        ]
        risk_row = risk[(risk["model"] == champion.model) & (risk["target"] == champion.target)]
        threshold_record = threshold.iloc[0].to_dict() if not threshold.empty else {}
        risk_record = risk_row.iloc[0].to_dict() if not risk_row.empty else {}
        rows.append(
            {
                "target": champion.target,
                "side": champion.side,
                "champion_model": champion.model,
                "selection_score": float(champion.selection_score),
                "metric_viable": bool(champion.metric_viable),
                "production_viable": bool(champion.production_viable),
                "artifact_available": bool(champion.artifact_available),
                "model_artifact_path": champion.model_artifact_path,
                "recommended_threshold": threshold_record.get("recommended_threshold"),
                "auc": getattr(champion, "auc", None),
                "average_precision": getattr(champion, "average_precision", None),
                "ece": getattr(champion, "ece", None),
                "expectancy": getattr(champion, "expectancy", None),
                "profit_factor": getattr(champion, "profit_factor", None),
                "net_pnl": getattr(champion, "net_pnl", None),
                "trade_count": getattr(champion, "trade_count", None),
                "risk_of_ruin_pct": risk_record.get("risk_of_ruin_pct"),
                "risk_gate_passed": risk_record.get("risk_gate_passed"),
            }
        )
    return rows


def promotion_decision(
    scored: pd.DataFrame,
    champions: pd.DataFrame,
    ensemble: pd.DataFrame,
    thresholds: pd.DataFrame,
    risk: pd.DataFrame,
    phase3_summary: dict[str, object],
    drift_summary: dict[str, object],
    config: PipelineConfig,
) -> dict[str, object]:
    champion_rows = target_quality_rows(champions, thresholds, risk)
    viable_rows = [row for row in champion_rows if row["production_viable"] and row.get("risk_gate_passed")]
    ce_rows = [row for row in viable_rows if row["side"] == "ce"]
    pe_rows = [row for row in viable_rows if row["side"] == "pe"]

    leakage_passed = bool(
        ((phase3_summary.get("walkforward") or {}) if isinstance(phase3_summary, dict) else {}).get(
            "all_leakage_checks_passed",
            False,
        )
    )
    feature_drift = (drift_summary.get("feature_drift") or {}) if isinstance(drift_summary, dict) else {}
    probability_drift = (drift_summary.get("probability_drift") or {}) if isinstance(drift_summary, dict) else {}
    feature_drift_over_limit = int(feature_drift.get("features_over_0_25", 999))
    probability_drift_max = float(probability_drift.get("max_psi_vs_previous_fold", 999.0))

    blockers = []
    warnings = []
    research_winners = (
        scored.sort_values(["target", "selection_score"], ascending=[True, False])
        .groupby("target", as_index=False)
        .head(1)
    )
    research_only_winners = [
        {
            "target": row.target,
            "model": row.model,
            "selection_score": float(row.selection_score),
            "reason": "model artifact is not available for promotion",
            "model_artifact_path": row.model_artifact_path,
        }
        for row in research_winners.itertuples(index=False)
        if not bool(row.artifact_available)
    ]
    if not leakage_passed:
        blockers.append("walkforward leakage checks did not pass")
    if not ce_rows:
        blockers.append("no CE champion passed production and risk gates")
    if not pe_rows:
        blockers.append("no PE champion passed production and risk gates")
    if research_only_winners:
        warnings.append(
            f"{len(research_only_winners)} research-best targets use models without promotion artifacts"
        )
    if feature_drift_over_limit > 0:
        warnings.append(f"{feature_drift_over_limit} features exceed PSI 0.25")
    if probability_drift_max > 0.50:
        warnings.append(f"probability drift PSI is high: {probability_drift_max}")
    promotable_ensembles = []
    if not ensemble.empty and "ensemble_type" in ensemble:
        promotable_ensembles = ensemble[
            (ensemble["ensemble_type"] == "promotable") & (ensemble["recommended"].astype(bool))
        ].to_dict("records")
    if not promotable_ensembles:
        warnings.append("no promotable ensembles available; only one persisted model family per target")

    promotion_ready = not blockers and feature_drift_over_limit <= 3 and probability_drift_max <= 1.0
    decision = "promote_candidate" if promotion_ready else "research_candidate_only"
    if not blockers and not promotion_ready:
        decision = "conditional_candidate"

    preferred_targets = sorted(
        viable_rows,
        key=lambda row: (
            float(row.get("selection_score") or 0.0),
            float(row.get("expectancy") or 0.0),
        ),
        reverse=True,
    )
    return {
        "schema_version": "phase4.production_candidate.v1",
        "decision": decision,
        "promotion_ready": bool(promotion_ready),
        "blockers": blockers,
        "warnings": warnings,
        "risk_limits": {
            "max_risk_of_ruin_pct": config.risk.max_risk_of_ruin_pct,
            "max_daily_loss_pct": config.risk.max_daily_loss_pct,
            "min_expected_net_pnl_rs": config.risk.min_expected_net_pnl_rs,
        },
        "champions": champion_rows,
        "research_only_winners": research_only_winners,
        "preferred_targets": preferred_targets[:4],
        "recommended_ensembles": promotable_ensembles,
        "controls": {
            "requires_manual_review_before_production_write": True,
            "uses_experimental_artifacts_only": True,
            "production_files_modified": False,
        },
    }


def run_phase4_recommendation(
    config: PipelineConfig,
    start_capital: float,
    ruin_fraction: float,
    monte_carlo_runs: int,
) -> dict[str, object]:
    paths = phase4_paths(config)
    reports = config.paths.output_dir / "reports"
    comparison = load_csv(reports / "comparison" / "model_comparison.csv")
    if comparison.empty:
        raise FileNotFoundError("missing Phase 3 comparison report")

    scored = score_candidates(comparison, config)
    if scored.empty:
        raise RuntimeError("Phase 3 comparison report has no usable ok rows")
    scored.to_csv(paths.recommendations / "trading_model_rankings.csv", index=False)
    write_json(paths.recommendations / "trading_model_rankings.json", scored.to_dict("records"))

    champions, challengers = champion_challenger_report(scored, paths)
    ensemble = build_ensemble_report(scored, paths)
    thresholds = dynamic_threshold_report(scored, paths)
    risk = risk_reports(
        scored,
        config,
        paths,
        start_capital=start_capital,
        ruin_fraction=ruin_fraction,
        runs=monte_carlo_runs,
    )

    phase3_summary = read_json(reports / "validation" / "phase3_validation_summary.json", {})
    drift_summary = read_json(reports / "drift" / "drift_summary.json", {})
    if not isinstance(phase3_summary, dict):
        phase3_summary = {}
    if not isinstance(drift_summary, dict):
        drift_summary = {}

    candidate = promotion_decision(
        scored=scored,
        champions=champions,
        ensemble=ensemble,
        thresholds=thresholds,
        risk=risk,
        phase3_summary=phase3_summary,
        drift_summary=drift_summary,
        config=config,
    )
    write_json(paths.promotion / "production_candidate.json", candidate)

    summary = {
        "status": "ok",
        "phase": 4,
        "comparison_rows": int(len(comparison)),
        "ranked_rows": int(len(scored)),
        "artifact_available_rows": int(scored["artifact_available"].sum()),
        "production_viable_rows": int(scored["production_viable"].sum()),
        "champions": int(len(champions)),
        "challengers": int(len(challengers)),
        "ensembles": int(len(ensemble)),
        "risk_rows": int(len(risk)),
        "promotion_decision": candidate["decision"],
        "promotion_ready": candidate["promotion_ready"],
        "production_candidate": str(paths.promotion / "production_candidate.json"),
        "reports": {
            "recommendations": str(paths.recommendations),
            "risk": str(paths.risk),
            "ensembles": str(paths.ensembles),
            "thresholds": str(paths.thresholds),
            "promotion": str(paths.promotion),
        },
    }
    write_json(paths.base / "phase4_summary.json", summary)
    return summary
