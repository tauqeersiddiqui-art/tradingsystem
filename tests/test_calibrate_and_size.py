# tests/test_calibrate_and_size.py
#
# Unit tests for tools/calibrate_and_size.py

import numpy as np
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.calibrate_and_size import compute_size_frac, calibrate_probabilities


def test_compute_size_frac():
    # p=0.5 (no edge) -> 0
    assert compute_size_frac(0.5, edge_fraction=0.3, max_frac=0.02) == 0.0

    # p=0.95 (max edge) -> clipped to max_frac
    assert compute_size_frac(0.95, edge_fraction=0.3, max_frac=0.02) == pytest.approx(0.02)

    # p=0.0 -> 0
    assert compute_size_frac(0.0, edge_fraction=0.3, max_frac=0.02) == 0.0

    # p=1.0 -> clipped
    assert compute_size_frac(1.0, edge_fraction=0.3, max_frac=0.02) == pytest.approx(0.02)

    # None -> 0
    assert compute_size_frac(None, edge_fraction=0.3, max_frac=0.02) == 0.0


def _make_synthetic_calibration(n=2000, calibrate_quality=0.7):
    """Create synthetic signals where score correlates with win probability."""
    np.random.seed(123)
    timestamps = pd.date_range("2026-01-01 09:15", periods=n, freq="1min")

    scores = np.random.beta(2, 2, n)

    # True probability = score (perfect calibration for test)
    true_probs = scores
    # Add some noise
    wins = np.random.binomial(1, true_probs)
    pnl = np.where(wins == 1, np.random.exponential(100, n), -np.random.exponential(80, n))

    df = pd.DataFrame({
        "timestamp": timestamps,
        "score": scores,
        "direction": np.random.choice(["CE", "PE"], n),
        "future_pnl_5m": pnl,
    })
    return df


def test_calibrate_probabilities():
    df = _make_synthetic_calibration(1000)

    calibrator, method, logloss = calibrate_probabilities(df, horizon=5, n_splits=3)

    assert calibrator is not None
    assert method in ("platt", "isotonic")
    assert np.isfinite(logloss)
    assert logloss < 1.0  # Should be reasonable


def test_calibration_monotonic():
    """Calibrated probabilities should be monotonic with scores."""
    df = _make_synthetic_calibration(1000)

    calibrator, method, _ = calibrate_probabilities(df, horizon=5, n_splits=3)

    # Test on a range of scores
    test_scores = np.linspace(0.1, 0.9, 9).reshape(-1, 1)
    if method == "platt":
        cal_probs = calibrator.predict_proba(test_scores)[:, 1]
    else:
        cal_probs = calibrator.predict(test_scores.ravel())

    # Should be monotonically increasing
    assert np.all(np.diff(cal_probs) >= -1e-6)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])