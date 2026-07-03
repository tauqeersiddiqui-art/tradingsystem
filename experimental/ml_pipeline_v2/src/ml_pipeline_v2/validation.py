from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    f1_score,
    log_loss,
    mean_absolute_error,
    mean_squared_error,
    precision_recall_curve,
    r2_score,
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


@dataclass(frozen=True)
class MulticlassClassificationMetrics:
    accuracy: float
    balanced_accuracy: float
    macro_f1: float
    log_loss: float
    multiclass_brier: float
    mean_max_probability: float
    expected_calibration_error: float
    target_distribution: dict[str, float]
    prediction_distribution: dict[str, float]


@dataclass(frozen=True)
class RegressionMetrics:
    mae: float
    rmse: float
    r2: float
    prediction_mean: float
    target_mean: float
    residual_mean: float
    residual_p95_abs: float


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


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> RegressionMetrics:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    residual = y_pred - y_true
    r2 = r2_score(y_true, y_pred) if len(np.unique(y_true)) > 1 else float("nan")
    return RegressionMetrics(
        mae=float(mean_absolute_error(y_true, y_pred)),
        rmse=float(np.sqrt(mean_squared_error(y_true, y_pred))),
        r2=float(r2),
        prediction_mean=float(y_pred.mean()),
        target_mean=float(y_true.mean()),
        residual_mean=float(residual.mean()),
        residual_p95_abs=float(np.quantile(np.abs(residual), 0.95)),
    )


def multiclass_expected_calibration_error(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_prob: np.ndarray,
    bins: int = 10,
) -> float:
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    y_prob = np.asarray(y_prob, dtype=float)
    confidence = y_prob.max(axis=1)
    correct = (y_true == y_pred).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (confidence >= lo) & (confidence <= hi if i == bins - 1 else confidence < hi)
        if not mask.any():
            continue
        ece += float(mask.mean()) * abs(float(confidence[mask].mean()) - float(correct[mask].mean()))
    return ece


def multiclass_classification_metrics(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    classes: np.ndarray,
) -> MulticlassClassificationMetrics:
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_prob, dtype=float)
    classes = np.asarray(classes)
    if y_prob.ndim != 2:
        raise ValueError("y_prob must be a 2D class-probability array")
    if y_prob.shape[1] != len(classes):
        raise ValueError("class count must match y_prob columns")

    pred_idx = np.argmax(y_prob, axis=1)
    y_pred = classes[pred_idx]
    one_hot = (y_true[:, None] == classes[None, :]).astype(float)
    target_dist = {str(cls): float((y_true == cls).mean()) for cls in classes}
    pred_dist = {str(cls): float((y_pred == cls).mean()) for cls in classes}
    return MulticlassClassificationMetrics(
        accuracy=float(accuracy_score(y_true, y_pred)),
        balanced_accuracy=float(balanced_accuracy_score(y_true, y_pred)),
        macro_f1=float(f1_score(y_true, y_pred, average="macro")),
        log_loss=float(log_loss(y_true, y_prob, labels=list(classes))),
        multiclass_brier=float(np.mean(np.sum((y_prob - one_hot) ** 2, axis=1))),
        mean_max_probability=float(y_prob.max(axis=1).mean()),
        expected_calibration_error=float(
            multiclass_expected_calibration_error(y_true, y_pred, y_prob)
        ),
        target_distribution=target_dist,
        prediction_distribution=pred_dist,
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
    frame = pd.DataFrame(rows, columns=["threshold", "precision", "recall", "samples"])
    if frame.empty:
        return frame
    return frame.sort_values(["precision", "recall"], ascending=False)


def threshold_recommendation_report(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    min_samples: int,
    quantiles: tuple[float, ...] = (0.50, 0.70, 0.80, 0.90, 0.95, 0.975, 0.99),
    value: np.ndarray | None = None,
) -> dict[str, object]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    if y_true.shape[0] != y_prob.shape[0]:
        raise ValueError("y_true and y_prob must have equal length")
    if value is not None:
        value = np.asarray(value, dtype=float)
        if value.shape[0] != y_true.shape[0]:
            raise ValueError("value must have equal length")

    if len(y_true) == 0:
        return {
            "base_rate": None,
            "positives": 0,
            "min_samples": int(min_samples),
            "recommended": None,
            "candidates": [],
        }

    positives = int(y_true.sum())
    base_rate = float(y_true.mean())
    thresholds = sorted({float(np.quantile(y_prob, q)) for q in quantiles}, reverse=True)
    rows: list[dict[str, float | int | None]] = []
    for threshold in thresholds:
        selected = y_prob >= threshold
        samples = int(selected.sum())
        if samples == 0:
            continue
        selected_positive = int(y_true[selected].sum())
        precision = float(selected_positive / samples)
        recall = float(selected_positive / positives) if positives else None
        f1 = (
            float(2.0 * precision * recall / (precision + recall))
            if recall is not None and (precision + recall) > 0
            else None
        )
        row: dict[str, float | int | None] = {
            "threshold": float(threshold),
            "samples": samples,
            "coverage": float(samples / len(y_true)),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "lift": float(precision / base_rate) if base_rate > 0 else None,
        }
        if value is not None:
            selected_value = value[selected]
            row["mean_value"] = float(selected_value.mean())
            row["total_value"] = float(selected_value.sum())
        rows.append(row)

    eligible = [row for row in rows if int(row["samples"]) >= min_samples]
    if value is not None:
        recommended = max(
            eligible,
            key=lambda row: (
                float(row["mean_value"]) if row["mean_value"] is not None else float("-inf"),
                float(row["precision"]) if row["precision"] is not None else float("-inf"),
                int(row["samples"]),
            ),
            default=None,
        )
    else:
        recommended = max(
            eligible,
            key=lambda row: (
                float(row["f1"]) if row["f1"] is not None else float("-inf"),
                float(row["precision"]) if row["precision"] is not None else float("-inf"),
                int(row["samples"]),
            ),
            default=None,
        )

    return {
        "base_rate": base_rate,
        "positives": positives,
        "min_samples": int(min_samples),
        "recommended": recommended,
        "candidates": rows,
    }


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
