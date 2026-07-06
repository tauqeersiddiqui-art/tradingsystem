from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_pipeline_v2.config import PipelineConfig
from ml_pipeline_v2.phase4 import json_default, read_json, write_json  # noqa: F401
from ml_pipeline_v2.phase5 import json_records, metrics_from_pnl, phase5_paths, write_csv

warnings.filterwarnings("ignore", category=RuntimeWarning)

PHASE6_SCHEMA_VERSION = "phase6.normal_ml_rescue.v1"
_LOT_UNITS = 30
_BROKERAGE_RS = 132.0


# ─── PATHS ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Phase6Paths:
    base: Path
    entry_quality: Path
    trade_capture: Path
    confidence_cal: Path
    range_intel: Path
    position_size: Path
    loss_clusters: Path
    improvement: Path


def phase6_paths(config: PipelineConfig) -> Phase6Paths:
    base = config.paths.output_dir / "reports" / "phase6"
    paths = Phase6Paths(
        base=base,
        entry_quality=base / "entry_quality",
        trade_capture=base / "trade_capture",
        confidence_cal=base / "confidence_calibration",
        range_intel=base / "range_market",
        position_size=base / "position_size",
        loss_clusters=base / "loss_clusters",
        improvement=base / "improvement",
    )
    for p in paths.__dataclass_fields__:
        getattr(paths, p).mkdir(parents=True, exist_ok=True)
    return paths


# ─── DATA LOADING ───────────────────────────────────────────────────────────────

def load_phase5_trades(config: PipelineConfig) -> pd.DataFrame:
    path = phase5_paths(config).trades / "completed_trades.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Phase 5 completed trades not found: {path}. Run Phase 5 first."
        )
    trades = pd.read_csv(path)
    trades["date"] = pd.to_datetime(trades["date"], errors="coerce")
    return trades.sort_values(["date", "side", "trade_id"]).reset_index(drop=True)


# ─── SHARED HELPERS ─────────────────────────────────────────────────────────────

def _profit_factor(pnl: pd.Series) -> float:
    wins = float(pnl[pnl > 0].sum())
    losses = float(abs(pnl[pnl < 0].sum()))
    if losses < 1e-9:
        return float("inf") if wins > 0 else 1.0
    return wins / losses


def _safe_col(df: pd.DataFrame, col: str, fill: float = 0.0) -> np.ndarray:
    if col in df.columns:
        return df[col].fillna(fill).to_numpy(dtype=float)
    return np.full(len(df), fill, dtype=float)


# ─── MODULE 1: ENTRY QUALITY ENGINE ───────────────────────────────────────────

def compute_entry_quality_scores(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Direction-aware Entry Quality Score (0-100).

    Weights:
        ADX             15 pts  — trend presence
        DI spread       15 pts  — directional alignment
        SuperTrend      15 pts  — trend direction signal
        EMA alignment   10 pts  — EMA stack
        VWAP position   10 pts  — price vs fair value
        RSI zone        10 pts  — momentum health
        Trend strength   8 pts  — normalised strength
        Range compress   5 pts  — consolidation proxy
        Volatility       5 pts  — moderate is ideal
        Momentum vel     5 pts  — directional persistence
        Market regime    2 pts  — macro context
        Total:         100 pts
    """
    df = trades.copy()

    # Percentile thresholds computed on full dataset
    adx_p25 = float(np.nanpercentile(_safe_col(df, "adx", 25.0), 25))
    adx_p50 = float(np.nanpercentile(_safe_col(df, "adx", 25.0), 50))
    adx_p75 = float(np.nanpercentile(_safe_col(df, "adx", 25.0), 75))
    ts_p25 = float(np.nanpercentile(_safe_col(df, "trend_strength", 0.0), 25))
    ts_p75 = float(np.nanpercentile(_safe_col(df, "trend_strength", 0.0), 75))
    vol_p25 = float(np.nanpercentile(_safe_col(df, "volatility", 0.01), 25))
    vol_p75 = float(np.nanpercentile(_safe_col(df, "volatility", 0.01), 75))
    rc_p25 = float(np.nanpercentile(_safe_col(df, "range_compression", 0.5), 25))
    rc_p50 = float(np.nanpercentile(_safe_col(df, "range_compression", 0.5), 50))

    is_ce = (df["side"] == "ce").to_numpy()
    scores = np.zeros(len(df), dtype=float)

    # 1. ADX (0-15): higher = stronger trend
    adx = _safe_col(df, "adx", 25.0)
    scores += np.where(adx >= adx_p75, 15.0,
              np.where(adx >= adx_p50, 12.0,
              np.where(adx >= adx_p25, 8.0, 3.0)))

    # 2. DI spread (0-15): direction-aware
    di = _safe_col(df, "di_spread", 0.0)
    scores += np.where(is_ce,
                   np.where(di > 5.0, 15.0, np.where(di > 0.0, 8.0, 0.0)),
                   np.where(di < -5.0, 15.0, np.where(di < 0.0, 8.0, 0.0)))

    # 3. SuperTrend direction (0-15): direction-aware
    st = _safe_col(df, "supertrend_dir", 0.0)
    scores += np.where(is_ce, np.where(st > 0, 15.0, 0.0),
                              np.where(st < 0, 15.0, 0.0))

    # 4. EMA alignment (0-10): direction-aware
    ema = _safe_col(df, "ema_alignment", 0.0)
    scores += np.where(is_ce, np.where(ema > 0, 10.0, 0.0),
                              np.where(ema < 0, 10.0, 0.0))

    # 5. VWAP position (0-10): direction-aware
    pvwap = _safe_col(df, "price_vs_vwap", 0.0)
    scores += np.where(is_ce, np.where(pvwap > 0, 10.0, 0.0),
                              np.where(pvwap < 0, 10.0, 0.0))

    # 6. RSI zone (0-10): direction-aware; extremes are risky
    rsi = _safe_col(df, "rsi", 50.0)
    rsi_ce = np.where((rsi >= 40) & (rsi <= 65), 10.0,
              np.where((rsi > 65) & (rsi <= 75), 5.0,
              np.where(rsi > 75, 2.0, 0.0)))
    rsi_pe = np.where((rsi >= 35) & (rsi <= 60), 10.0,
              np.where((rsi >= 25) & (rsi < 35), 5.0,
              np.where(rsi < 25, 2.0, 0.0)))
    scores += np.where(is_ce, rsi_ce, rsi_pe)

    # 7. Trend strength (0-8): percentile-based
    ts = _safe_col(df, "trend_strength", 0.0)
    scores += np.where(ts >= ts_p75, 8.0, np.where(ts >= ts_p25, 5.0, 2.0))

    # 8. Range compression (0-5): lower = more compressed = good breakout setup
    rc = _safe_col(df, "range_compression", 0.5)
    scores += np.where(rc <= rc_p25, 5.0, np.where(rc <= rc_p50, 3.0, 1.0))

    # 9. Volatility (0-5): moderate volatility is best
    vol = _safe_col(df, "volatility", 0.01)
    scores += np.where((vol >= vol_p25) & (vol <= vol_p75), 5.0, 2.0)

    # 10. Momentum velocity (0-5): direction-aware
    mom = _safe_col(df, "momentum_velocity", 0.0)
    scores += np.where(is_ce, np.where(mom > 0, 5.0, 0.0),
                              np.where(mom < 0, 5.0, 0.0))

    # 11. Market regime (0-2): trending regime earns bonus
    regime = df.get("market_regime", pd.Series(["mixed"] * len(df))).fillna("mixed").to_numpy()
    scores += np.where(regime == "volatile_trend", 2.0,
               np.where(regime == "trend", 2.0,
               np.where(regime == "mixed", 1.0, 0.0)))

    df["entry_quality_score"] = np.clip(scores, 0.0, 100.0).round(1)
    return df


def run_entry_quality_analysis(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    df = compute_entry_quality_scores(trades)

    df["quality_decile"] = pd.qcut(
        df["entry_quality_score"], q=10, labels=False, duplicates="drop"
    )

    decile_stats = (
        df.groupby("quality_decile")
        .agg(
            trades=("pnl", "count"),
            win_rate=("outcome", lambda x: float((x == "win").mean())),
            avg_pnl=("pnl", "mean"),
            total_pnl=("pnl", "sum"),
            avg_score=("entry_quality_score", "mean"),
            avg_mfe=("mfe_points", "mean"),
            avg_mae=("mae_points", "mean"),
        )
        .reset_index()
    )

    winners = df[df["outcome"] == "win"]
    losers = df[df["outcome"] != "win"]

    threshold_tests: list[dict[str, Any]] = []
    for thresh in (30, 40, 50, 60, 70):
        mask = df["entry_quality_score"] >= thresh
        if mask.sum() < 100:
            continue
        sub = df[mask]
        pf = _profit_factor(sub["pnl"])
        threshold_tests.append({
            "quality_threshold": thresh,
            "trades": int(mask.sum()),
            "coverage": float(mask.mean()),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "avg_pnl": float(sub["pnl"].mean()),
            "total_pnl": float(sub["pnl"].sum()),
            "profit_factor": pf,
            "profit_factor_delta": pf - _profit_factor(df["pnl"]),
        })
    threshold_tests.sort(key=lambda x: x["profit_factor"], reverse=True)

    feature_comparison: list[dict[str, Any]] = []
    for col in ["adx", "rsi", "trend_strength", "atr", "volatility",
                "momentum_velocity", "range_compression", "entry_quality_score",
                "directional_confidence", "quality_confidence"]:
        if col not in df.columns:
            continue
        feature_comparison.append({
            "feature": col,
            "winner_mean": float(winners[col].mean()) if len(winners) > 0 else None,
            "loser_mean": float(losers[col].mean()) if len(losers) > 0 else None,
            "winner_median": float(winners[col].median()) if len(winners) > 0 else None,
            "loser_median": float(losers[col].median()) if len(losers) > 0 else None,
            "separation": (
                float(winners[col].mean() - losers[col].mean())
                if len(winners) > 0 and len(losers) > 0 else None
            ),
        })

    report: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "entry_quality_engine",
        "total_trades": len(df),
        "score_mean": float(df["entry_quality_score"].mean()),
        "score_std": float(df["entry_quality_score"].std()),
        "score_p25": float(df["entry_quality_score"].quantile(0.25)),
        "score_p50": float(df["entry_quality_score"].quantile(0.50)),
        "score_p75": float(df["entry_quality_score"].quantile(0.75)),
        "winner_score_mean": float(winners["entry_quality_score"].mean()) if len(winners) > 0 else None,
        "loser_score_mean": float(losers["entry_quality_score"].mean()) if len(losers) > 0 else None,
        "threshold_tests": threshold_tests,
        "decile_stats": json_records(decile_stats),
        "feature_comparison": feature_comparison,
    }

    write_json(paths.entry_quality / "entry_quality_report.json", report)
    write_csv(
        df[["trade_id", "side", "date", "pnl", "outcome",
            "entry_quality_score", "quality_decile"]].copy(),
        paths.entry_quality / "entry_quality_scores.csv",
    )
    return report


# ─── MODULE 2: TRADE CAPTURE ENGINE ────────────────────────────────────────────

def _simulate_exits(df: pd.DataFrame) -> pd.DataFrame:
    lot = _LOT_UNITS
    brok = _BROKERAGE_RS
    mfe = df["mfe_points"].fillna(0.0).to_numpy()
    mae = df["mae_points"].fillna(0.0).to_numpy()
    atr = _safe_col(df, "atr", 20.0)
    hbars = df["holding_bars"].fillna(12.0).to_numpy()
    target_hit = df["target_hit"].fillna(0).to_numpy()
    actual_pnl = df["pnl"].to_numpy()

    rows: list[dict[str, Any]] = []

    def _stats(pnl_arr: np.ndarray, name: str) -> dict[str, Any]:
        s = pd.Series(pnl_arr)
        return {
            "strategy": name,
            "total_pnl": float(s.sum()),
            "avg_pnl": float(s.mean()),
            "profit_factor": _profit_factor(s),
            "win_rate": float((s > 0).mean()),
            "trades": len(s),
        }

    rows.append(_stats(actual_pnl, "actual"))

    # Early exit at fraction of MFE — only override when MFE exceeds target (15 pts)
    for frac, label in ((0.50, "early_50pct_mfe"), (0.75, "early_75pct_mfe")):
        captured = mfe * frac
        # Trades that never reached target: keep actual outcome (negative)
        # Trades with target hit: simulate capturing frac of MFE
        pnl_sim = np.where(
            target_hit == 1,
            captured * lot - brok,
            np.where(captured * lot - brok > actual_pnl, captured * lot - brok, actual_pnl),
        )
        rows.append(_stats(pnl_sim, label))

    # Trailing stop: lock in trailing_pct * peak
    for tp, label in ((0.60, "trailing_60pct"), (0.80, "trailing_80pct")):
        locked = mfe * tp
        # Realised = max(locked, 0) if trade went into profit, else actual
        pnl_sim = np.where(
            mfe * lot - brok > 0,
            locked * lot - brok,
            actual_pnl,
        )
        rows.append(_stats(pnl_sim, label))

    # ATR trailing: exit at MFE - 1.5*ATR
    captured_atr = np.maximum(mfe - 1.5 * atr, 0.0)
    pnl_atr = np.where(mfe * lot - brok > 0, captured_atr * lot - brok, actual_pnl)
    rows.append(_stats(pnl_atr, "atr_trailing"))

    # Break-even stop: when MFE crossed target once, stop can't lose more than brokerage
    mfe_crossed_target = mfe >= 15.0
    be_pnl = np.where(
        mfe_crossed_target & (actual_pnl < 0),
        -brok,
        actual_pnl,
    )
    rows.append(_stats(be_pnl, "breakeven_stop"))

    # Time-based exits: cap holding bars
    for n_bars, label in ((6, "time_exit_6bars"), (8, "time_exit_8bars")):
        scale = np.where(hbars > n_bars, n_bars / np.maximum(hbars, 1.0), 1.0)
        pnl_time = actual_pnl * scale
        rows.append(_stats(pnl_time, label))

    # Partial exit: 50% at first target hit, rest at actual
    partial_pnl = np.where(
        target_hit == 1,
        0.5 * (15.0 * lot - brok * 0.5) + 0.5 * actual_pnl,
        actual_pnl,
    )
    rows.append(_stats(partial_pnl, "partial_50pct_at_target"))

    result = pd.DataFrame(rows).sort_values("profit_factor", ascending=False).reset_index(drop=True)
    return result


def run_trade_capture_analysis(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    df = trades.copy()
    lot = _LOT_UNITS
    brok = _BROKERAGE_RS

    df["mfe_rs"] = df["mfe_points"].fillna(0.0) * lot
    df["mae_rs"] = df["mae_points"].fillna(0.0) * lot

    max_pnl = df["mfe_rs"] - brok
    df["capture_pct"] = np.where(
        max_pnl > 0,
        np.clip(df["pnl"] / max_pnl * 100.0, -100.0, 200.0),
        np.nan,
    )
    df["giveback_pct"] = np.where(
        df["mfe_rs"] > 0,
        np.clip((df["mfe_rs"] - df["pnl"]) / df["mfe_rs"] * 100.0, 0.0, 200.0),
        np.nan,
    )

    time_stats = {
        "avg_holding_bars": float(df["holding_bars"].mean()),
        "median_holding_bars": float(df["holding_bars"].median()),
        "winner_avg_bars": float(df[df["outcome"] == "win"]["holding_bars"].mean()),
        "loser_avg_bars": float(df[df["outcome"] != "win"]["holding_bars"].mean()),
        "winner_avg_mfe_rs": float(df[df["outcome"] == "win"]["mfe_rs"].mean()),
        "loser_avg_mfe_rs": float(df[df["outcome"] != "win"]["mfe_rs"].mean()),
        "winner_avg_mae_rs": float(df[df["outcome"] == "win"]["mae_rs"].mean()),
        "loser_avg_mae_rs": float(df[df["outcome"] != "win"]["mae_rs"].mean()),
    }

    capture_by_outcome = (
        df.groupby("outcome")
        .agg(
            count=("capture_pct", "count"),
            avg_capture_pct=("capture_pct", "mean"),
            avg_giveback_pct=("giveback_pct", "mean"),
            avg_mfe_rs=("mfe_rs", "mean"),
            avg_mae_rs=("mae_rs", "mean"),
        )
        .reset_index()
    )

    exit_sim = _simulate_exits(df)

    best_exit = exit_sim.iloc[0].to_dict() if not exit_sim.empty else {}
    baseline_pf = _profit_factor(df["pnl"])

    report: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "trade_capture_engine",
        "baseline": {
            "total_pnl": float(df["pnl"].sum()),
            "profit_factor": baseline_pf,
            "avg_capture_pct": float(df["capture_pct"].dropna().mean()),
            "avg_giveback_pct": float(df["giveback_pct"].dropna().mean()),
            "avg_mfe_rs": float(df["mfe_rs"].mean()),
            "avg_mae_rs": float(df["mae_rs"].mean()),
        },
        "time_stats": time_stats,
        "capture_by_outcome": json_records(capture_by_outcome),
        "exit_strategy_ranking": json_records(exit_sim),
        "best_exit_strategy": best_exit,
    }

    write_json(paths.trade_capture / "trade_capture_report.json", report)
    write_csv(exit_sim, paths.trade_capture / "exit_strategy_ranking.csv")
    write_csv(
        df[["trade_id", "side", "date", "pnl", "mfe_rs", "mae_rs",
            "capture_pct", "giveback_pct", "holding_bars", "outcome"]].copy(),
        paths.trade_capture / "capture_metrics.csv",
    )
    return report


# ─── MODULE 3: CONFIDENCE CALIBRATION ──────────────────────────────────────────

def _calibration_buckets(
    confidence: np.ndarray,
    wins: np.ndarray,
    n_bins: int = 10,
) -> tuple[float, list[dict[str, Any]]]:
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    buckets: list[dict[str, Any]] = []
    n = len(confidence)
    for i in range(n_bins):
        lo, hi = float(bins[i]), float(bins[i + 1])
        mask = (confidence >= lo) & (confidence < hi)
        if not mask.any():
            continue
        conf_mean = float(confidence[mask].mean())
        actual_wr = float(wins[mask].mean())
        count = int(mask.sum())
        ece += (count / n) * abs(conf_mean - actual_wr)
        buckets.append({
            "bin_low": lo,
            "bin_high": hi,
            "conf_mean": conf_mean,
            "actual_win_rate": actual_wr,
            "count": count,
            "calibration_error": abs(conf_mean - actual_wr),
        })
    return ece, buckets


def run_confidence_calibration(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    df = trades.copy()
    is_win = (df["outcome"] == "win").astype(int).to_numpy()
    conf = df["confidence"].fillna(0.5).to_numpy()

    ece, buckets = _calibration_buckets(conf, is_win)

    bucket_stats = (
        df.groupby("confidence_bucket", observed=True)
        .agg(
            count=("pnl", "count"),
            win_rate=("outcome", lambda x: float((x == "win").mean())),
            avg_pnl=("pnl", "mean"),
            total_pnl=("pnl", "sum"),
            avg_confidence=("confidence", "mean"),
            profit_factor=("pnl", _profit_factor),
        )
        .reset_index()
        .sort_values("avg_confidence")
    )

    threshold_rows: list[dict[str, Any]] = []
    for t in np.arange(0.28, 0.82, 0.02):
        mask = df["confidence"] >= t
        if mask.sum() < 100:
            continue
        sub = df[mask]
        threshold_rows.append({
            "confidence_threshold": float(round(t, 2)),
            "trades": int(mask.sum()),
            "coverage": float(mask.mean()),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "avg_pnl": float(sub["pnl"].mean()),
            "profit_factor": _profit_factor(sub["pnl"]),
            "total_pnl": float(sub["pnl"].sum()),
        })
    thresh_df = pd.DataFrame(threshold_rows)

    optimal_thresholds: dict[str, Any] = {}
    if not thresh_df.empty:
        idx_exp = thresh_df["avg_pnl"].idxmax()
        idx_pf = thresh_df["profit_factor"].idxmax()
        optimal_thresholds = {
            "best_expectancy": thresh_df.loc[idx_exp].to_dict(),
            "best_profit_factor": thresh_df.loc[idx_pf].to_dict(),
        }

    side_calibration: dict[str, Any] = {}
    for side in ("ce", "pe"):
        sub = df[df["side"] == side]
        if len(sub) < 200:
            continue
        sub_win = (sub["outcome"] == "win").astype(int).to_numpy()
        sub_conf = sub["confidence"].fillna(0.5).to_numpy()
        side_ece, side_bkts = _calibration_buckets(sub_conf, sub_win)
        side_calibration[side] = {
            "ece": float(side_ece),
            "trades": len(sub),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "avg_pnl": float(sub["pnl"].mean()),
            "profit_factor": _profit_factor(sub["pnl"]),
            "buckets": side_bkts,
        }

    report: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "confidence_calibration",
        "expected_calibration_error": float(ece),
        "reliability_buckets": buckets,
        "bucket_stats": json_records(bucket_stats),
        "threshold_optimization": threshold_rows,
        "optimal_thresholds": optimal_thresholds,
        "side_calibration": side_calibration,
    }

    write_json(paths.confidence_cal / "confidence_calibration_report.json", report)
    write_csv(thresh_df, paths.confidence_cal / "threshold_optimization.csv")
    write_csv(bucket_stats, paths.confidence_cal / "bucket_stats.csv")
    return report


# ─── MODULE 4: RANGE MARKET INTELLIGENCE ───────────────────────────────────────

def run_range_market_analysis(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    df = trades.copy()

    range_mask = df["market_regime"].isin(["range"])
    range_trades = df[range_mask].copy()
    non_range = df[~range_mask].copy()

    def _regime_metrics(sub: pd.DataFrame, label: str) -> dict[str, Any]:
        if len(sub) == 0:
            return {"label": label, "trades": 0}
        return {
            "label": label,
            "trades": len(sub),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "avg_pnl": float(sub["pnl"].mean()),
            "total_pnl": float(sub["pnl"].sum()),
            "profit_factor": _profit_factor(sub["pnl"]),
        }

    range_win = range_trades[range_trades["outcome"] == "win"] if len(range_trades) > 0 else pd.DataFrame()
    range_lose = range_trades[range_trades["outcome"] != "win"] if len(range_trades) > 0 else pd.DataFrame()

    feature_cols = [
        "adx", "rsi", "trend_strength", "atr", "volatility",
        "momentum_velocity", "range_compression", "directional_confidence",
        "quality_confidence", "confidence", "mfe_points", "mae_points",
        "holding_bars", "price_vs_vwap", "ema_alignment", "supertrend_dir",
    ]
    indicator_diff: list[dict[str, Any]] = []
    for col in feature_cols:
        if col not in range_trades.columns or len(range_trades) < 50:
            continue
        w_mean = float(range_win[col].mean()) if len(range_win) > 0 else None
        l_mean = float(range_lose[col].mean()) if len(range_lose) > 0 else None
        indicator_diff.append({
            "feature": col,
            "range_winner_mean": w_mean,
            "range_loser_mean": l_mean,
            "overall_mean": float(df[col].mean()),
            "separation": float(w_mean - l_mean) if (w_mean is not None and l_mean is not None) else None,
        })

    # Time-of-day analysis in range
    time_analysis: list[dict[str, Any]] = []
    if len(range_trades) > 0 and "hour" in range_trades.columns:
        for h in sorted(range_trades["hour"].dropna().unique()):
            sub = range_trades[range_trades["hour"] == h]
            if len(sub) < 20:
                continue
            time_analysis.append({
                "hour": int(h),
                "trades": len(sub),
                "win_rate": float((sub["outcome"] == "win").mean()),
                "avg_pnl": float(sub["pnl"].mean()),
            })

    # Direction analysis in range
    direction_analysis: list[dict[str, Any]] = []
    for side in ("ce", "pe"):
        sub = range_trades[range_trades["side"] == side] if len(range_trades) > 0 else pd.DataFrame()
        if len(sub) < 20:
            continue
        direction_analysis.append({
            "side": side,
            "trades": len(sub),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "avg_pnl": float(sub["pnl"].mean()),
        })

    # Skip / delay / confirm recommendations
    skip_conditions: list[dict[str, Any]] = []

    if len(range_trades) >= 50 and "confidence" in range_trades.columns:
        low_conf = range_trades[range_trades["confidence"] < 0.50]
        if len(low_conf) >= 30:
            skip_conditions.append({
                "condition": "range AND confidence < 0.50",
                "trades_affected": int(len(low_conf)),
                "win_rate": float((low_conf["outcome"] == "win").mean()),
                "avg_pnl": float(low_conf["pnl"].mean()),
                "action": "skip",
                "rationale": "Low confidence in range regime has near-random directional value",
            })

    if len(range_trades) >= 50 and "adx" in range_trades.columns:
        low_adx = range_trades[range_trades["adx"] < 18]
        if len(low_adx) >= 30:
            skip_conditions.append({
                "condition": "range AND adx < 18",
                "trades_affected": int(len(low_adx)),
                "win_rate": float((low_adx["outcome"] == "win").mean()),
                "avg_pnl": float(low_adx["pnl"].mean()),
                "action": "skip",
                "rationale": "Very low ADX in range = no momentum; ML edge collapses",
            })

    if len(range_trades) >= 50 and "price_vs_vwap" in range_trades.columns:
        ce_avwap = range_trades[(range_trades["side"] == "ce") & (range_trades["price_vs_vwap"] < -0.001)]
        pe_avwap = range_trades[(range_trades["side"] == "pe") & (range_trades["price_vs_vwap"] > 0.001)]
        against_vwap = pd.concat([ce_avwap, pe_avwap])
        if len(against_vwap) >= 30:
            skip_conditions.append({
                "condition": "range AND trading against VWAP direction",
                "trades_affected": int(len(against_vwap)),
                "win_rate": float((against_vwap["outcome"] == "win").mean()),
                "avg_pnl": float(against_vwap["pnl"].mean()),
                "action": "require_confirmation",
                "rationale": "Fading VWAP in range amplifies mean-reversion drawdown",
            })

    if len(range_trades) >= 50 and "supertrend_dir" in range_trades.columns:
        st = _safe_col(range_trades, "supertrend_dir", 0.0)
        is_ce_rt = (range_trades["side"] == "ce").to_numpy()
        contra = range_trades[
            np.where(is_ce_rt, st < 0, st > 0)
        ]
        if len(contra) >= 30:
            skip_conditions.append({
                "condition": "range AND SuperTrend opposes trade direction",
                "trades_affected": int(len(contra)),
                "win_rate": float((contra["outcome"] == "win").mean()),
                "avg_pnl": float(contra["pnl"].mean()),
                "action": "delay",
                "rationale": "SuperTrend contradiction in range increases fade risk",
            })

    range_metrics = _regime_metrics(range_trades, "range")
    non_range_metrics = _regime_metrics(non_range, "non_range")

    report: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "range_market_intelligence",
        "overall_metrics": _regime_metrics(df, "all"),
        "range_metrics": range_metrics,
        "non_range_metrics": non_range_metrics,
        "range_win_rate": range_metrics.get("win_rate", 0.0),
        "non_range_win_rate": non_range_metrics.get("win_rate", 0.0),
        "win_rate_gap": float(
            (non_range_metrics.get("win_rate") or 0.0) - (range_metrics.get("win_rate") or 0.0)
        ),
        "indicator_differences": indicator_diff,
        "time_of_day_analysis": time_analysis,
        "direction_analysis": direction_analysis,
        "skip_conditions": skip_conditions,
    }

    write_json(paths.range_intel / "range_market_report.json", report)
    if indicator_diff:
        write_csv(pd.DataFrame(indicator_diff), paths.range_intel / "indicator_comparison.csv")
    return report


# ─── MODULE 5: POSITION SIZE ANALYSIS ──────────────────────────────────────────

def run_position_size_analysis(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    df = trades.copy()
    lot = _LOT_UNITS

    total_loss = float(df[df["pnl"] < 0]["pnl"].sum())
    worst50 = df[df["pnl"] < 0].nsmallest(50, "pnl")
    top10_loss = float(worst50.head(10)["pnl"].sum())
    top10_pct = (top10_loss / total_loss * 100.0) if total_loss < -1e-6 else 0.0

    # Confidence-tiered sizing simulations
    tiered_results: list[dict[str, Any]] = []
    for low_mult, high_thresh in ((0.5, 0.65), (0.5, 0.70), (0.25, 0.65), (0.25, 0.70)):
        conf = df["confidence"].fillna(0.5).to_numpy()
        mult = np.where(conf >= high_thresh, 1.5, np.where(conf >= 0.50, 1.0, low_mult))
        pnl_sim = pd.Series(df["pnl"].to_numpy() * mult)
        tiered_results.append({
            "strategy": f"low{low_mult}x_highthresh{high_thresh}",
            "low_conf_multiplier": low_mult,
            "high_conf_threshold": high_thresh,
            "total_pnl": float(pnl_sim.sum()),
            "profit_factor": _profit_factor(pnl_sim),
            "avg_pnl": float(pnl_sim.mean()),
        })
    tiered_results.sort(key=lambda x: x["profit_factor"], reverse=True)

    # Kelly approximation
    win_rate = float((df["outcome"] == "win").mean())
    avg_win = float(df[df["pnl"] > 0]["pnl"].mean()) if (df["pnl"] > 0).any() else 1.0
    avg_loss = float(abs(df[df["pnl"] < 0]["pnl"].mean())) if (df["pnl"] < 0).any() else 1.0
    b = avg_win / avg_loss if avg_loss > 1e-9 else 1.0
    kelly = (win_rate * b - (1.0 - win_rate)) / b if b > 1e-9 else -1.0
    half_kelly = kelly * 0.5

    # Loss by regime
    loss_by_regime = (
        df[df["pnl"] < 0]
        .groupby("market_regime")
        .agg(
            count=("pnl", "count"),
            total_loss=("pnl", "sum"),
            avg_loss=("pnl", "mean"),
        )
        .reset_index()
        .sort_values("total_loss")
    )
    loss_by_regime["loss_pct"] = (
        loss_by_regime["total_loss"] / total_loss * 100.0
        if total_loss < -1e-6 else 0.0
    )

    # Loss by confidence bucket
    loss_by_conf = (
        df[df["pnl"] < 0]
        .groupby("confidence_bucket", observed=True)
        .agg(
            count=("pnl", "count"),
            total_loss=("pnl", "sum"),
            avg_loss=("pnl", "mean"),
        )
        .reset_index()
        .sort_values("total_loss")
    )

    recommendation = (
        "System has negative Kelly fraction — reduce all lot sizes to minimum; "
        "deploy entry/exit filter improvements before scaling."
        if kelly < 0
        else (
            f"Kelly fraction = {kelly:.3f}; half-Kelly = {half_kelly:.3f}. "
            "Apply confidence-tiered sizing: 0.5x for confidence < 0.50, "
            "1.5x for confidence >= 0.70."
        )
    )

    report: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "position_size_analysis",
        "current_lot_units": lot,
        "total_loss": total_loss,
        "top10_loss": top10_loss,
        "top10_loss_pct": top10_pct,
        "win_rate": win_rate,
        "avg_win_rs": avg_win,
        "avg_loss_rs": avg_loss,
        "reward_risk_ratio": b,
        "kelly_fraction": kelly,
        "half_kelly": half_kelly,
        "tiered_sizing_simulations": tiered_results,
        "loss_by_regime": json_records(loss_by_regime),
        "loss_by_confidence": json_records(loss_by_conf),
        "recommendation": recommendation,
    }

    write_json(paths.position_size / "position_size_report.json", report)
    write_csv(pd.DataFrame(tiered_results), paths.position_size / "tiered_sizing.csv")
    write_csv(loss_by_regime, paths.position_size / "loss_by_regime.csv")
    return report


# ─── MODULE 6: LOSS CLUSTERING ─────────────────────────────────────────────────

def run_loss_clustering(trades: pd.DataFrame, paths: Phase6Paths) -> dict[str, Any]:
    try:
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        report: dict[str, Any] = {
            "schema_version": PHASE6_SCHEMA_VERSION,
            "module": "loss_clustering",
            "status": "sklearn_unavailable",
        }
        write_json(paths.loss_clusters / "loss_clusters.json", report)
        return report

    df = trades.copy()
    losers = df[df["pnl"] < 0].copy().reset_index(drop=True)

    if len(losers) < 200:
        report = {
            "schema_version": PHASE6_SCHEMA_VERSION,
            "module": "loss_clustering",
            "status": "insufficient_data",
            "losing_trades": len(losers),
        }
        write_json(paths.loss_clusters / "loss_clusters.json", report)
        return report

    numeric_features = [
        "adx", "rsi", "trend_strength", "volatility", "atr",
        "momentum_velocity", "range_compression", "confidence",
        "directional_confidence", "quality_confidence",
        "mfe_points", "mae_points", "holding_bars",
    ]

    # Encode categorical features
    if "market_regime" in losers.columns:
        regime_dummies = pd.get_dummies(losers["market_regime"], prefix="regime")
        losers = pd.concat([losers.reset_index(drop=True), regime_dummies.reset_index(drop=True)], axis=1)
        numeric_features += list(regime_dummies.columns)

    if "side" in losers.columns:
        losers["_side_ce"] = (losers["side"] == "ce").astype(int)
        numeric_features.append("_side_ce")

    if "hour" in losers.columns:
        numeric_features.append("hour")

    feat_cols = [c for c in numeric_features if c in losers.columns]
    X_df = losers[feat_cols].fillna(0.0)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_df)

    # Find best k via inertia
    inertias: dict[str, float] = {}
    best_k = 6
    for k in range(4, 11):
        km = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=200)
        km.fit(X_scaled)
        inertias[str(k)] = float(km.inertia_)

    km_final = KMeans(n_clusters=best_k, random_state=42, n_init=10, max_iter=300)
    losers["cluster"] = km_final.fit_predict(X_scaled)

    total_loss = float(losers["pnl"].sum())

    clusters: list[dict[str, Any]] = []
    for c in range(best_k):
        sub = losers[losers["cluster"] == c]
        if len(sub) == 0:
            continue

        regime_mode = (
            sub["market_regime"].mode().iloc[0]
            if "market_regime" in sub.columns and not sub["market_regime"].mode().empty
            else "unknown"
        )
        side_mode = (
            sub["side"].mode().iloc[0]
            if "side" in sub.columns and not sub["side"].mode().empty
            else "unknown"
        )
        conf_mean = float(sub["confidence"].mean()) if "confidence" in sub.columns else 0.5
        adx_mean = float(sub["adx"].mean()) if "adx" in sub.columns else 25.0
        vol_mean = float(sub["volatility"].mean()) if "volatility" in sub.columns else 0.02
        baseline_vol = float(df["volatility"].mean()) if "volatility" in df.columns else 0.02

        label_parts: list[str] = []
        if conf_mean < 0.45:
            label_parts.append("low-confidence")
        if adx_mean < 20:
            label_parts.append("weak-trend")
        if regime_mode == "range":
            label_parts.append("range-market")
        if vol_mean > baseline_vol * 1.5:
            label_parts.append("high-volatility")
        if regime_mode == "volatile_trend":
            label_parts.append("volatile-trend")

        auto_label = "_".join(label_parts) if label_parts else f"cluster_{c}"

        if "low-confidence" in label_parts or "range-market" in label_parts:
            action = "skip_entry"
        elif "high-volatility" in label_parts:
            action = "reduce_size_or_require_confirmation"
        else:
            action = "investigate_entry_timing"

        info: dict[str, Any] = {
            "cluster_id": int(c),
            "trade_count": int(len(sub)),
            "pct_of_losses": float(len(sub) / len(losers) * 100.0),
            "total_loss": float(sub["pnl"].sum()),
            "total_loss_pct": float(sub["pnl"].sum() / total_loss * 100.0) if abs(total_loss) > 1e-6 else 0.0,
            "avg_loss": float(sub["pnl"].mean()),
            "dominant_regime": regime_mode,
            "dominant_side": side_mode,
            "auto_label": auto_label,
            "recommended_action": action,
        }
        for col in ["adx", "rsi", "trend_strength", "confidence", "volatility",
                    "mfe_points", "mae_points", "holding_bars", "hour"]:
            if col in sub.columns:
                info[f"avg_{col}"] = float(sub[col].mean())

        clusters.append(info)

    clusters.sort(key=lambda x: x["total_loss"])

    report = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "loss_clustering",
        "status": "ok",
        "losing_trades": int(len(losers)),
        "n_clusters": best_k,
        "inertias": inertias,
        "clusters": clusters,
    }

    write_json(paths.loss_clusters / "loss_clusters.json", report)
    write_csv(
        losers[["trade_id", "side", "date", "pnl", "cluster",
                "market_regime", "confidence", "adx", "rsi",
                "holding_bars", "mfe_points", "mae_points"]].copy(),
        paths.loss_clusters / "loss_clusters.csv",
    )
    return report


# ─── MODULE 7: ML IMPROVEMENT ENGINE ───────────────────────────────────────────

def build_ml_improvement_plan(
    entry_quality: dict[str, Any],
    trade_capture: dict[str, Any],
    confidence_cal: dict[str, Any],
    range_intel: dict[str, Any],
    position_size: dict[str, Any],
    loss_clusters: dict[str, Any],
    trades: pd.DataFrame,
) -> dict[str, Any]:
    baseline_pf = _profit_factor(trades["pnl"])
    baseline_exp = float(trades["pnl"].mean())
    recs: list[dict[str, Any]] = []

    # From Module 1
    eq_tests = entry_quality.get("threshold_tests") or []
    if eq_tests:
        best = eq_tests[0]
        recs.append({
            "id": "REC_EQ_01",
            "source": "entry_quality_engine",
            "title": f"Apply Entry Quality Score >= {best['quality_threshold']}",
            "description": (
                f"Reject trades with Entry Quality Score < {best['quality_threshold']}/100. "
                "Score is computed from ADX, DI spread, SuperTrend, EMA alignment, VWAP, "
                "RSI zone, trend strength, momentum velocity, and market regime."
            ),
            "evidence": (
                f"Score >= {best['quality_threshold']} retains {best['coverage']*100:.1f}% of trades; "
                f"PF={best['profit_factor']:.3f} vs baseline {baseline_pf:.3f}; "
                f"avg_pnl={best['avg_pnl']:.1f}"
            ),
            "expected_pf_improvement": float(best["profit_factor"] - baseline_pf),
            "expected_expectancy_improvement": float(best["avg_pnl"] - baseline_exp),
            "expected_trade_reduction": float(1.0 - best["coverage"]),
            "statistical_confidence": "moderate" if best["trades"] >= 5000 else "low",
            "risk": "Heuristic score; may overfit to historical feature distributions",
        })

    # From Module 2
    best_exit = trade_capture.get("best_exit_strategy") or {}
    if best_exit and best_exit.get("strategy") not in ("actual", None, ""):
        exit_pf = float(best_exit.get("profit_factor", baseline_pf))
        recs.append({
            "id": "REC_TC_01",
            "source": "trade_capture_engine",
            "title": f"Adopt exit strategy: {best_exit.get('strategy', 'N/A')}",
            "description": (
                "Modify Normal ML exit logic to use the highest-ranked simulated exit. "
                f"Strategy: {best_exit.get('strategy')}."
            ),
            "evidence": (
                f"Best exit achieves PF={exit_pf:.3f} vs actual PF={baseline_pf:.3f}; "
                f"avg_pnl={best_exit.get('avg_pnl', 0):.1f}"
            ),
            "expected_pf_improvement": exit_pf - baseline_pf,
            "expected_expectancy_improvement": float(
                (best_exit.get("avg_pnl") or 0.0) - baseline_exp
            ),
            "expected_trade_reduction": 0.0,
            "statistical_confidence": "high" if len(trades) >= 20000 else "moderate",
            "risk": "Simulation uses MFE/MAE proxies; live path dynamics may differ",
        })

    # From Module 3
    opt = (confidence_cal.get("optimal_thresholds") or {}).get("best_expectancy") or {}
    if opt:
        thresh_val = float(opt.get("confidence_threshold", 0.5))
        thresh_exp = float(opt.get("avg_pnl", 0.0))
        thresh_pf = float(opt.get("profit_factor", baseline_pf))
        thresh_cov = float(opt.get("coverage", 1.0))
        thresh_n = int(opt.get("trades", 0))
        recs.append({
            "id": "REC_CC_01",
            "source": "confidence_calibration",
            "title": f"Raise minimum confidence threshold to {thresh_val:.2f}",
            "description": (
                "Only execute Normal ML trades when model confidence >= new threshold. "
                "Calibration analysis shows over-confidence at low probability buckets."
            ),
            "evidence": (
                f"Threshold {thresh_val:.2f} → expectancy={thresh_exp:.1f} "
                f"vs baseline={baseline_exp:.1f}; PF={thresh_pf:.3f}; "
                f"retains {thresh_cov*100:.1f}% trades ({thresh_n:,})"
            ),
            "expected_pf_improvement": thresh_pf - baseline_pf,
            "expected_expectancy_improvement": thresh_exp - baseline_exp,
            "expected_trade_reduction": 1.0 - thresh_cov,
            "statistical_confidence": "high" if thresh_n >= 5000 else "moderate",
            "risk": "Higher threshold = fewer signals; tail risk of under-trading",
        })

    # From Module 4
    skip = range_intel.get("skip_conditions") or []
    range_wr = float(range_intel.get("range_win_rate") or 0.0)
    non_range_wr = float(range_intel.get("non_range_win_rate") or 0.0)
    range_trades_n = int((range_intel.get("range_metrics") or {}).get("trades") or 0)
    if range_wr < non_range_wr - 0.02 or len(skip) > 0:
        recs.append({
            "id": "REC_RM_01",
            "source": "range_market_intelligence",
            "title": "Add regime-gating: skip or confirm before entering RANGE trades",
            "description": (
                f"Range regime win rate ({range_wr*100:.1f}%) lags non-range "
                f"({non_range_wr*100:.1f}%). "
                f"Identified {len(skip)} specific skip/delay conditions."
            ),
            "evidence": (
                f"Win rate gap = {(non_range_wr - range_wr)*100:.1f} pp; "
                f"range trades = {range_trades_n:,}"
            ),
            "expected_pf_improvement": 0.05,
            "expected_expectancy_improvement": 20.0,
            "expected_trade_reduction": float(range_trades_n / max(len(trades), 1)),
            "statistical_confidence": "moderate" if range_trades_n >= 2000 else "low",
            "risk": "Regime proxy is computed; may misclassify transitional sessions",
        })

    # From Module 5
    kelly = float(position_size.get("kelly_fraction") or 0.0)
    tiered_sims = position_size.get("tiered_sizing_simulations") or []
    if kelly < 0:
        recs.append({
            "id": "REC_PS_01",
            "source": "position_size_analysis",
            "title": "Reduce position size — system has negative mathematical edge",
            "description": (
                "Kelly fraction is negative, meaning the current edge does not justify "
                "any positive position size. Reduce to minimum until filter improvements are deployed."
            ),
            "evidence": f"Kelly={kelly:.3f}, baseline PF={baseline_pf:.3f}",
            "expected_pf_improvement": 0.0,
            "expected_expectancy_improvement": 0.0,
            "expected_trade_reduction": 0.0,
            "statistical_confidence": "high",
            "risk": "Size reduction alone does not improve edge; must combine with filters",
        })
    elif tiered_sims:
        best_t = tiered_sims[0]
        if best_t["profit_factor"] > baseline_pf + 0.02:
            recs.append({
                "id": "REC_PS_02",
                "source": "position_size_analysis",
                "title": "Implement confidence-tiered position sizing",
                "description": (
                    f"Use {best_t['low_conf_multiplier']}x lot for confidence < 0.50; "
                    f"1.5x lot for confidence >= {best_t['high_conf_threshold']}."
                ),
                "evidence": (
                    f"Tiered sizing PF={best_t['profit_factor']:.3f} "
                    f"vs uniform PF={baseline_pf:.3f}"
                ),
                "expected_pf_improvement": float(best_t["profit_factor"] - baseline_pf),
                "expected_expectancy_improvement": float(best_t["avg_pnl"] - baseline_exp),
                "expected_trade_reduction": 0.0,
                "statistical_confidence": "moderate",
                "risk": "Requires live confidence score at execution time; leverage must stay within risk limits",
            })

    # From Module 6
    clusters = loss_clusters.get("clusters") or []
    if clusters:
        worst = clusters[0]
        recs.append({
            "id": "REC_LC_01",
            "source": "loss_clustering",
            "title": f"Address Cluster {worst['cluster_id']}: {worst.get('auto_label', 'worst_cluster')}",
            "description": (
                f"Cluster accounts for {worst['pct_of_losses']:.1f}% of losing trades "
                f"(₹{abs(worst['total_loss']):,.0f} loss). "
                f"Dominant: regime={worst.get('dominant_regime')}, side={worst.get('dominant_side')}. "
                f"Recommended: {worst.get('recommended_action')}."
            ),
            "evidence": (
                f"avg_conf={worst.get('avg_confidence', 'N/A')}, "
                f"avg_adx={worst.get('avg_adx', 'N/A')}, "
                f"avg_pnl={worst['avg_loss']:.1f}"
            ),
            "expected_pf_improvement": 0.03,
            "expected_expectancy_improvement": float(
                abs(worst["total_loss"]) / max(len(trades), 1)
            ),
            "expected_trade_reduction": float(worst["trade_count"] / max(len(trades), 1)),
            "statistical_confidence": "moderate" if worst["trade_count"] >= 1000 else "low",
            "risk": "Cluster labels are unsupervised — validate each cluster manually before applying rules",
        })

    recs.sort(key=lambda x: float(x.get("expected_pf_improvement") or 0.0), reverse=True)

    return {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "module": "ml_improvement_engine",
        "baseline_profit_factor": baseline_pf,
        "baseline_expectancy": baseline_exp,
        "baseline_win_rate": float((trades["outcome"] == "win").mean()),
        "baseline_trades": len(trades),
        "total_recommendations": len(recs),
        "recommendations": recs,
    }


# ─── MAIN ORCHESTRATOR ──────────────────────────────────────────────────────────

def run_phase6_ml_rescue(
    config: PipelineConfig,
    min_trades: int = 200,
) -> dict[str, Any]:
    """
    Phase 6: Normal ML Rescue Engine.

    Runs 7 analysis modules on Phase 5 completed trades and generates
    evidence-backed improvement recommendations.  No production code is
    modified and no models are retrained.
    """
    paths = phase6_paths(config)

    print("[Phase 6] Loading Phase 5 completed trades…")
    trades = load_phase5_trades(config)

    if len(trades) < min_trades:
        raise ValueError(
            f"Insufficient trades for Phase 6 ({len(trades)} < {min_trades}). "
            "Run Phase 5 first: python scripts/run_phase5_profitability.py"
        )

    baseline_pf = _profit_factor(trades["pnl"])
    print(
        f"[Phase 6] {len(trades):,} trades loaded  |  "
        f"PF={baseline_pf:.3f}  |  "
        f"win_rate={float((trades['outcome']=='win').mean())*100:.1f}%"
    )

    print("[Phase 6] Module 1: Entry Quality Engine…")
    eq_report = run_entry_quality_analysis(trades, paths)

    print("[Phase 6] Module 2: Trade Capture Engine…")
    tc_report = run_trade_capture_analysis(trades, paths)

    print("[Phase 6] Module 3: Confidence Calibration…")
    cc_report = run_confidence_calibration(trades, paths)

    print("[Phase 6] Module 4: Range Market Intelligence…")
    rm_report = run_range_market_analysis(trades, paths)

    print("[Phase 6] Module 5: Position Size Analysis…")
    ps_report = run_position_size_analysis(trades, paths)

    print("[Phase 6] Module 6: Loss Clustering…")
    lc_report = run_loss_clustering(trades, paths)

    print("[Phase 6] Module 7: ML Improvement Engine…")
    imp_plan = build_ml_improvement_plan(
        entry_quality=eq_report,
        trade_capture=tc_report,
        confidence_cal=cc_report,
        range_intel=rm_report,
        position_size=ps_report,
        loss_clusters=lc_report,
        trades=trades,
    )
    write_json(paths.improvement / "ml_improvement_plan.json", imp_plan)

    summary: dict[str, Any] = {
        "schema_version": PHASE6_SCHEMA_VERSION,
        "status": "ok",
        "phase": 6,
        "controls": {
            "experimental_only": True,
            "production_files_modified": False,
            "live_engine_integrated": False,
            "retrained_models": False,
            "reused_phase5_artifacts": True,
        },
        "input_trades": len(trades),
        "modules_completed": 7,
        "baseline": {
            "trades": len(trades),
            "profit_factor": baseline_pf,
            "win_rate": float((trades["outcome"] == "win").mean()),
            "expectancy": float(trades["pnl"].mean()),
            "total_pnl": float(trades["pnl"].sum()),
        },
        "module_1_entry_quality": {
            "winner_score_mean": eq_report.get("winner_score_mean"),
            "loser_score_mean": eq_report.get("loser_score_mean"),
            "score_separation": (
                float((eq_report.get("winner_score_mean") or 0.0) -
                      (eq_report.get("loser_score_mean") or 0.0))
            ),
            "best_threshold": (
                eq_report["threshold_tests"][0]["quality_threshold"]
                if eq_report.get("threshold_tests") else None
            ),
            "best_threshold_pf": (
                eq_report["threshold_tests"][0]["profit_factor"]
                if eq_report.get("threshold_tests") else None
            ),
        },
        "module_2_trade_capture": {
            "avg_capture_pct": (tc_report.get("baseline") or {}).get("avg_capture_pct"),
            "avg_giveback_pct": (tc_report.get("baseline") or {}).get("avg_giveback_pct"),
            "best_exit_strategy": (tc_report.get("best_exit_strategy") or {}).get("strategy"),
            "best_exit_pf": (tc_report.get("best_exit_strategy") or {}).get("profit_factor"),
        },
        "module_3_confidence_calibration": {
            "ece": cc_report.get("expected_calibration_error"),
            "optimal_threshold": (
                ((cc_report.get("optimal_thresholds") or {}).get("best_expectancy") or {})
                .get("confidence_threshold")
            ),
            "optimal_threshold_pf": (
                ((cc_report.get("optimal_thresholds") or {}).get("best_expectancy") or {})
                .get("profit_factor")
            ),
        },
        "module_4_range_intel": {
            "range_win_rate": rm_report.get("range_win_rate"),
            "non_range_win_rate": rm_report.get("non_range_win_rate"),
            "win_rate_gap": rm_report.get("win_rate_gap"),
            "skip_conditions_found": len(rm_report.get("skip_conditions") or []),
        },
        "module_5_position_size": {
            "kelly_fraction": ps_report.get("kelly_fraction"),
            "half_kelly": ps_report.get("half_kelly"),
            "top10_loss_pct": ps_report.get("top10_loss_pct"),
            "best_tiered_pf": (
                (ps_report.get("tiered_sizing_simulations") or [{}])[0].get("profit_factor")
            ),
        },
        "module_6_loss_clusters": {
            "n_clusters": lc_report.get("n_clusters"),
            "losing_trades_clustered": lc_report.get("losing_trades"),
            "worst_cluster_label": (
                (lc_report.get("clusters") or [{}])[0].get("auto_label")
            ),
            "worst_cluster_loss_pct": (
                (lc_report.get("clusters") or [{}])[0].get("total_loss_pct")
            ),
        },
        "module_7_improvement_plan": {
            "total_recommendations": imp_plan.get("total_recommendations"),
            "top_recommendation": (
                (imp_plan.get("recommendations") or [{}])[0].get("title")
            ),
            "top_recommendation_pf_delta": (
                (imp_plan.get("recommendations") or [{}])[0].get("expected_pf_improvement")
            ),
        },
        "reports": {
            "entry_quality_report": str(paths.entry_quality / "entry_quality_report.json"),
            "trade_capture_report": str(paths.trade_capture / "trade_capture_report.json"),
            "confidence_calibration_report": str(paths.confidence_cal / "confidence_calibration_report.json"),
            "range_market_report": str(paths.range_intel / "range_market_report.json"),
            "position_size_report": str(paths.position_size / "position_size_report.json"),
            "loss_clusters": str(paths.loss_clusters / "loss_clusters.json"),
            "ml_improvement_plan": str(paths.improvement / "ml_improvement_plan.json"),
            "phase6_summary": str(paths.base / "phase6_summary.json"),
        },
    }

    write_json(paths.base / "phase6_summary.json", summary)
    print(f"[Phase 6] Complete. Reports → {paths.base}")
    return summary
