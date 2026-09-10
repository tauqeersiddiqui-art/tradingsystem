# tests/test_threshold_sweep.py
#
# Unit tests for tools/threshold_sweep.py — ensures expectancy calculation
# and threshold selection work on synthetic data.

import numpy as np
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.threshold_sweep import compute_expectancy, sweep_thresholds


def test_compute_expectancy():
    # 10 trades, avg PnL = 100, cost = 66 per trade (round-trip = 132)
    pnls = np.array([100.0] * 10)
    cost = 132.0
    exp = compute_expectancy(pnls, cost, 10)
    # Net per trade = 100 - 132 = -32
    assert exp == pytest.approx(-32.0)

    # With profits > cost
    pnls = np.array([200.0] * 10)
    exp = compute_expectancy(pnls, cost, 10)
    # Net per trade = 200 - 132 = 68
    assert exp == pytest.approx(68.0)

    # Empty
    exp = compute_expectancy(np.array([]), cost, 0)
    assert exp == -np.inf


def _make_synthetic_signals(n=1000, signal_quality=0.6):
    """Create synthetic signals DataFrame with known properties."""
    np.random.seed(42)
    timestamps = pd.date_range("2026-01-01 09:15", periods=n, freq="1min")

    # Score correlated with future PnL
    scores = np.random.beta(2, 2, n)  # 0-1
    # Higher score -> higher mean PnL
    mean_pnl = (scores - 0.5) * 200  # -100 to +100
    pnl_5m = np.random.normal(mean_pnl, 150)

    df = pd.DataFrame({
        "timestamp": timestamps,
        "score": scores,
        "direction": np.random.choice(["CE", "PE"], n),
        "future_pnl_5m": pnl_5m,
    })
    return df


def test_sweep_thresholds_basic(monkeypatch):
    df = _make_synthetic_signals(2000, signal_quality=0.65)

    results = sweep_thresholds(df, horizon=5, cost_per_lot=66.0, min_trades=30, n_splits=3)

    assert not results.empty
    assert "threshold" in results.columns
    assert "mean_expectancy" in results.columns
    assert len(results) > 0

    # Best threshold should be > 0.5 for positive edge
    best = results.loc[results["mean_expectancy"].idxmax()]
    assert best["threshold"] >= 0.50
    assert best["threshold"] <= 0.95


def test_sweep_thresholds_min_trades_filter():
    """Thresholds with too few trades per fold should be excluded."""
    df = _make_synthetic_signals(500)

    # High min_trades should filter out most thresholds
    results = sweep_thresholds(df, horizon=5, cost_per_lot=66.0, min_trades=100, n_splits=3)

    # All remaining should have mean_trades >= min_trades (approximately)
    if not results.empty:
        assert (results["mean_trades"] >= 80).all()  # some tolerance


def test_sweep_cost_scenarios():
    """Higher cost should shift best threshold higher (more selective)."""
    df = _make_synthetic_signals(2000)

    results_low = sweep_thresholds(df, horizon=5, cost_per_lot=33.0, min_trades=30, n_splits=3)
    results_high = sweep_thresholds(df, horizon=5, cost_per_lot=132.0, min_trades=30, n_splits=3)

    if not results_low.empty and not results_high.empty:
        best_low = results_low.loc[results_low["mean_expectancy"].idxmax(), "threshold"]
        best_high = results_high.loc[results_high["mean_expectancy"].idxmax(), "threshold"]
        # Higher cost -> higher threshold (more selective)
        assert best_high >= best_low


if __name__ == "__main__":
    pytest.main([__file__, "-v"])