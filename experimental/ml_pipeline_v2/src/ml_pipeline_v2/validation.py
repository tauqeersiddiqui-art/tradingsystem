from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
)


@dataclass(frozen=True)
class ClassificationMetrics:
    auc: float
    average_precision: float
    brier: float
    prediction_mean: float
    target_mean: float
    expected_calibration_error: float


def purged_walkforward_splits(
    n_rows: int,
    folds: int,
    min_train_rows: int,
    test_rows: int | None = None,
    purge_bars: int = 0,
    embargo_bars: int = 0,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    if folds <= 0:
        raise ValueError("folds must be positive")
    if n_rows <= min_train_rows:
        raise ValueError("not enough rows for requested min_train_rows")

    if test_rows is None:
        remaining = n_rows - min_train_rows
        test_rows = max(1, remaining // folds)

    for fold in range(folds):
        train_end = min_train_rows + fold * test_rows
        test_start = train_end + purge_bars
        test_end = min(test_start + test_rows, n_rows)
        if test_start >= n_rows or test_end <= test_start:
            break
        train_stop = max(0, train_end - embargo_bars)
        yield np.arange(0, train_stop), np.arange(test_start, test_end)


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10
) -> float:
    y_true = np.asarray(y_true).astype(float)
    y_prob = np.asarray(y_prob).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_prob >= lo) & (y_prob <= hi if i == bins - 1 else y_prob < hi)
        if not mask.any():
            continue
        ece += float(mask.mean()) * abs(float(y_prob[mask].mean()) - float(y_true[mask].mean()))
    return ece


def classification_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> ClassificationMetrics:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    auc = roc_auc_score(y_true, y_prob) if len(np.unique(y_true)) > 1 else float("nan")
    ap = average_precision_score(y_true, y_prob) if len(np.unique(y_true)) > 1 else float("nan")
    return ClassificationMetrics(
        auc=float(auc),
        average_precision=float(ap),
        brier=float(brier_score_loss(y_true, y_prob)),
        prediction_mean=float(y_prob.mean()),
        target_mean=float(y_true.mean()),
        expected_calibration_error=float(expected_calibration_error(y_true, y_prob)),
    )


def threshold_candidates_from_calibration(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    min_precision: float,
    min_recall: float,
    min_samples: int,
) -> pd.DataFrame:
    precision, recall, thresholds = precision_recall_curve(y_true, y_prob)
    rows = []
    for p, r, t in zip(precision[:-1], recall[:-1], thresholds):
        n = int((y_prob >= t).sum())
        if n < min_samples:
            continue
        if p < min_precision or r < min_recall:
            continue
        rows.append({"threshold": float(t), "precision": float(p), "recall": float(r), "samples": n})
    return pd.DataFrame(rows).sort_values(["precision", "recall"], ascending=False)


def trade_metrics(pnl: np.ndarray) -> dict[str, float]:
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return {
            "trades": 0,
            "net_pnl": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
        }
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    equity = pnl.cumsum()
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    return {
        "trades": int(len(pnl)),
        "net_pnl": float(pnl.sum()),
        "expectancy": float(pnl.mean()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "win_rate": float((pnl > 0).mean()),
        "max_drawdown": float(drawdown.min()),
        "avg_winner": float(wins.mean()) if len(wins) else 0.0,
        "avg_loser": float(losses.mean()) if len(losses) else 0.0,
    }


def monte_carlo_risk(
    pnl: np.ndarray,
    start_capital: float,
    ruin_fraction: float = 0.30,
    runs: int = 2000,
    seed: int = 42,
) -> dict[str, float]:
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return {"risk_of_ruin": 0.0, "p05_final_equity": start_capital, "median_final_equity": start_capital}
    rng = np.random.default_rng(seed)
    final_equities = []
    ruined = 0
    ruin_level = start_capital * (1.0 - ruin_fraction)
    for _ in range(runs):
        sample = rng.choice(pnl, size=len(pnl), replace=True)
        curve = start_capital + sample.cumsum()
        final_equities.append(float(curve[-1]))
        if curve.min() <= ruin_level:
            ruined += 1
    return {
        "risk_of_ruin": float(ruined / runs),
        "p05_final_equity": float(np.quantile(final_equities, 0.05)),
        "median_final_equity": float(np.quantile(final_equities, 0.50)),
    }

