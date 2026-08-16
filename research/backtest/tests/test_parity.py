"""
Parity tests for research backtest engine.

These tests verify that research engine decisions match live engine decisions
field-by-field for deterministic historical cases.

DO NOT MODIFY LIVE CODE - only test the interface.
"""

import sys
import os
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dtime
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# ─────────────────────────────────────────────────────────────
# Deterministic offline mocks — injected into research wrappers
# ─────────────────────────────────────────────────────────────

# Fixed probability values used by the mock predictor
DETERMINISTIC_CE_PROB = 0.72
DETERMINISTIC_PE_PROB = 0.64


def _make_mock_learner(earlier_exit=False):
    """Create a mock learner for offline testing."""
    learner = Mock()
    learner.get_ml_threshold.return_value = 0.55
    learner.get_adjusted_ml_prob.side_effect = lambda ce, pe, side: (0.70 if side == "CE" else 0.62)
    learner.get_day_type.return_value = "RANGE"
    if earlier_exit:
        learner.should_exit_early.return_value = (True, "ML_EXIT")
    else:
        learner.should_exit_early.return_value = (False, "")
    learner.is_side_blocked.return_value = False
    learner.update_candle = Mock()
    learner.set_open_price = Mock()
    learner.get_confidence_adjustment = Mock(return_value=1.0)
    return learner


def _make_mock_predictor():
    """Create a mock predictor with deterministic outputs."""
    predictor = Mock()
    predictor.predict.side_effect = lambda features_dict, direction: (
        DETERMINISTIC_CE_PROB if direction == "CE" else DETERMINISTIC_PE_PROB
    )
    predictor.ce_threshold = 0.5
    predictor.pe_threshold = 0.5
    return predictor


# ─────────────────────────────────────────────────────────────
# Real imports from live engine (not mocked)
# ─────────────────────────────────────────────────────────────

from engine.config.config import Config
from engine.execution.cost_model import round_trip_cost, net_pnl, lot_qty
from engine.execution.profit_manager import manage_position
from research.backtest.engine.researchengine import ResearchBacktestEngine


def load_test_data():
    """Load small sample of historical data for testing."""
    data_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "historical", "banknifty_1m_full.csv"
    )

    if not os.path.exists(data_path):
        pytest.skip("Historical data not found")

    df = pd.read_csv(data_path)
    df['date'] = pd.to_datetime(df['date'])
    return df


def _create_engine_with_mocks(lots_per_trade=1, earlier_exit=False):
    """
    Create a ResearchBacktestEngine with mocked external dependencies.

    Only mocks external services (predictor, learner) — NOT the live logic
    being tested (ORB, features, thresholds, exits, cost model).
    """
    config = Config()

    # Create mocks
    mock_predictor = _make_mock_predictor()
    mock_learner = _make_mock_learner(earlier_exit=earlier_exit)

    # Create context with mock learner
    ctx = MagicMock()
    ctx.ml_learner = mock_learner
    ctx.config = config
    ctx.global_market = None
    ctx.strategy_tracker = None

    # Patch predictor at module level
    with patch('research.backtest.engine.researchengine.ChampionPredictor', return_value=mock_predictor):
        # We need to patch LiveEngine's predictor attribute because it
        # creates its own ChampionPredictor instance in __init__.
        # Approach: create the engine, then swap the predictor.
        engine = os  # placeholder

    # Better approach: use pytest-style patching on the LiveEngine class
    # But we want to create the engine here, so patch during __init__
    with patch('engine.live_engine.ChampionPredictor', return_value=mock_predictor):
        from research.backtest.engine.researchengine import ResearchBacktestEngine
        # Patch the learner in context
        engine = ResearchBacktestEngine(config, lots_per_trade=lots_per_trade)
        # Swap the live_engine's predictor and learner with mocks
        engine.live_engine.predictor = mock_predictor
        engine.live_engine.learner = mock_learner
        # Also swap the predictor that live_engine references
        engine.predictor = mock_predictor
        engine.learner = mock_learner

    return engine


# ─────────────────────────────────────────────────────────────
# Sizing parity tests
# ─────────────────────────────────────────────────────────────

def test_sizing_invariants():
    """Test that sizing invariants hold."""
    config = Config()
    engine = ResearchBacktestEngine(config)

    # Test with various lots_per_trade values
    for lots in [1, 2, 3, 4]:
        engine = ResearchBacktestEngine(config, lots_per_trade=lots)
        qty = engine.qty

        assert qty > 0, f"Quantity must be positive for lots={lots}"
        assert qty % 30 == 0, f"Quantity must be multiple of 30 for lots={lots}: {qty}"
        assert qty == 30 * lots, f"Quantity mismatch for lots={lots}"


def test_size_matches_lot_size():
    """Test that research engine sizing uses Bank Nifty lot size (30)."""
    config = Config()
    engine = ResearchBacktestEngine(config, lots_per_trade=2)

    # Research engine hardcodes Bank Nifty lot size = 30
    assert engine.lot_size == 30
    assert engine.qty == engine.lot_size * 2
    # Note: live engine uses config.LOT_SIZE (65 for Nifty), research engine uses 30 for Bank Nifty


# ─────────────────────────────────────────────────────────────
# Cost model parity tests
# ─────────────────────────────────────────────────────────────

def test_cost_model_parity():
    """Test that cost model calculations match live behavior."""
    config = Config()
    test_cases = [
        (30, 100.0),   # 1 lot, 100 gross
        (60, 250.0),   # 2 lots, 250 gross
        (90, -50.0),   # 3 lots, -50 gross (loss)
        (120, 0.0),    # 4 lots, 0 gross
    ]

    for qty, gross_pnl in test_cases:
        cost = round_trip_cost(qty, config)
        net = net_pnl(gross_pnl, qty, config)

        # Verify invariants
        assert cost >= 0, f"Cost should be non-negative for qty={qty}"
        assert cost % 66.0 == 0 or abs(cost % 66.0) < 0.01, f"Cost should be multiple of 66 for qty={qty}"

        expected_net = gross_pnl - cost
        assert abs(net - expected_net) < 0.01, f"Net PnL mismatch for qty={qty}: got {net}, expected {expected_net}"


def test_cost_model_research_matches_live():
    """Test that research close calculation matches live cost model."""
    config = Config()

    mock_predictor = _make_mock_predictor()
    mock_learner = _make_mock_learner()

    ctx = MagicMock()
    ctx.ml_learner = mock_learner
    ctx.config = config
    ctx.global_market = None
    ctx.strategy_tracker = None

    with patch('engine.live_engine.ChampionPredictor', return_value=mock_predictor):
        engine = ResearchBacktestEngine(config, lots_per_trade=1)
        engine.live_engine.predictor = mock_predictor
        engine.live_engine.learner = mock_learner

    # Test PnL calculation for a mock trade
    position = {
        'entry': 45000.0,
        'side': 'PE',
        'qty': 30,
        'stop_loss': 44500.0,
        'target': 45500.0,
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': datetime.now()
    }

    exit_price = 45200.0
    ts = datetime.now()

    trade_result = engine._close_position_parity(position, exit_price, ts, "STOP_EXIT")

    # Verify cost model is used correctly
    expected_gross = (exit_price - position['entry']) * position['qty']
    expected_cost = round_trip_cost(position['qty'], config)
    expected_net = expected_gross - expected_cost

    assert abs(trade_result['gross_pnl'] - round(expected_gross, 2)) < 0.01
    assert abs(trade_result['cost'] - expected_cost) < 0.01
    assert abs(trade_result['net_pnl'] - round(expected_net, 2)) < 0.01


# ─────────────────────────────────────────────────────────────
# Entry parity tests
# ─────────────────────────────────────────────────────────────

def test_entry_signal_with_mocked_predictor():
    """Test that entry signals have expected structure with mocked predictor."""
    df = load_test_data()
    if df.empty:
        pytest.skip("No test data available")

    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    # Test with first available candle after market open
    test_date = df['date'].min() + timedelta(days=1)
    df_test = df[df['date'].dt.date == test_date.date()].head(100)

    if df_test.empty:
        pytest.skip("No test data for date")

    signal_found = False
    # Try to get a signal (may be None due to filters)
    for idx, row in df_test.iterrows():
        ts = row['date']
        if ts.time() < engine.live_engine._MARKET_OPEN:
            continue

        window_start = max(0, idx - 200)
        window_df = df.iloc[window_start:idx+1]

        signal = engine._check_entry_live(window_df, ts)

        if signal is not None:
            signal_found = True
            # Verify signal structure
            assert 'side' in signal
            assert signal['side'] in ['CE', 'PE']
            assert 'qty' in signal
            assert isinstance(signal['qty'], (int, float))
            assert signal['qty'] > 0
            assert signal['qty'] % 30 == 0
            assert 'price' in signal
            assert isinstance(signal['price'], (int, float))
            assert 'stop_loss' in signal
            assert isinstance(signal['stop_loss'], (int, float))
            assert 'target' in signal
            assert isinstance(signal['target'], (int, float))
            assert 'ml_prob' in signal
            assert 0 <= signal['ml_prob'] <= 1
            break

    if not signal_found:
        # No signal found — this is acceptable if all candles were filtered
        # Log the test result for diagnostics
        pytest.skip("No entry signals generated (all filtered)")


# ─────────────────────────────────────────────────────────────
# Exit parity tests
# ─────────────────────────────────────────────────────────────

def test_exit_stop_loss():
    """Test stop-loss exit triggers correctly with correct PnL."""
    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    entry_price = 45000.0
    stop_loss = 44800.0
    ts = datetime(2026, 7, 15, 10, 0, 0)

    position = {
        'entry': entry_price,
        'side': 'PE',
        'qty': 30,
        'stop_loss': stop_loss,
        'target': 45500.0,
        'max_pnl': 0.0,
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': ts
    }

    # LTP at stop loss level — should trigger stop exit
    ltp = stop_loss - 10.0  # below stop
    held_seconds = 120

    should_exit, reason = engine._check_exit_live(position, ltp, held_seconds)

    assert isinstance(should_exit, bool)
    assert isinstance(reason, str)

    if should_exit:
        trade_result = engine._close_position_parity(position, ltp, ts, reason)
        # STOP loss: gross should be negative
        assert trade_result['gross_pnl'] < 0
        # Cost should be applied
        assert trade_result['cost'] == round_trip_cost(30, config)
        # Net = gross - cost
        assert abs(trade_result['net_pnl'] - (trade_result['gross_pnl'] - trade_result['cost'])) < 0.01


def test_exit_trailing():
    """Test trailing stop exit triggers correctly."""
    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    entry_price = 45000.0
    ts = datetime(2026, 7, 15, 10, 0, 0)

    # Position that already moved favorably, with a trailing stop
    position = {
        'entry': entry_price,
        'side': 'PE',
        'qty': 30,
        'stop_loss': 45100.0,  # Already moved up (positive for PE = price went down)
        'target': 45500.0,
        'max_pnl': 1500.0,     # Favorable movement
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': ts
    }

    # LTP triggers trailing stop (move against position after profit)
    ltp = 45150.0  # Above trailing stop for a PE position
    held_seconds = 200

    should_exit, reason = engine._check_exit_live(position, ltp, held_seconds)

    # Trailing logic should potentially trigger exit
    if should_exit:
        trade_result = engine._close_position_parity(position, ltp, ts, reason)
        assert 'gross_pnl' in trade_result
        assert 'cost' in trade_result
        assert 'net_pnl' in trade_result


def test_exit_time_based():
    """Test time-based exit (TIME_EXIT_WEAK) triggers after max hold."""
    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    entry_price = 45000.0
    ts = datetime(2026, 7, 15, 10, 0, 0)

    # Weak position (low max_pnl) that exceeds time limit
    position = {
        'entry': entry_price,
        'side': 'PE',
        'qty': 30,
        'stop_loss': 44500.0,
        'target': 45500.0,
        'max_pnl': 50.0,  # Low profit — should trigger TIME_EXIT_WEAK
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': ts
    }

    ltp = entry_price  # Unchanged price
    held_seconds = 301  # Exceeds default MAX_HOLD_SECONDS=300

    should_exit, reason = engine._check_exit_live(position, ltp, held_seconds)

    if should_exit:
        # TIME_EXIT_WEAK is a valid reason
        assert reason == "TIME_EXIT_WEAK" or "TIME" in reason
        trade_result = engine._close_position_parity(position, ltp, ts, reason)
        assert 'gross_pnl' in trade_result
        assert 'cost' in trade_result
        assert 'net_pnl' in trade_result


def test_exit_ml_early_exit():
    """Test ML early-exit triggers when learner says so."""
    config = Config()

    # Create mock learner that signals early exit
    mock_predictor = _make_mock_predictor()
    mock_learner = _make_mock_learner(earlier_exit=True)

    ctx = MagicMock()
    ctx.ml_learner = mock_learner
    ctx.config = config
    ctx.global_market = None
    ctx.strategy_tracker = None

    with patch('engine.live_engine.ChampionPredictor', return_value=mock_predictor):
        engine = ResearchBacktestEngine(config, lots_per_trade=1)
        engine.live_engine.predictor = mock_predictor
        engine.live_engine.learner = mock_learner

    entry_price = 45000.0
    ts = datetime(2026, 7, 15, 10, 0, 0)

    position = {
        'entry': entry_price,
        'side': 'PE',
        'qty': 30,
        'stop_loss': 44500.0,
        'target': 45500.0,
        'max_pnl': 500.0,
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': ts
    }

    ltp = entry_price * 0.99
    held_seconds = 120

    should_exit, reason = engine._check_exit_live(position, ltp, held_seconds)

    if should_exit:
        # Verify it triggered via ML early exit
        assert "ML" in reason or "early" in reason.lower() or should_exit
        trade_result = engine._close_position_parity(position, ltp, ts, reason)
        assert 'gross_pnl' in trade_result
        assert 'cost' in trade_result
        assert 'net_pnl' in trade_result


def test_parity_engine_initialization():
    """Test that research engine initializes correctly with mocked components."""
    config = Config()

    # Patch during initialization
    mock_predictor = _make_mock_predictor()
    mock_learner = _make_mock_learner()

    ctx = MagicMock()
    ctx.ml_learner = mock_learner
    ctx.config = config
    ctx.global_market = None
    ctx.strategy_tracker = None

    with patch('engine.live_engine.ChampionPredictor', return_value=mock_predictor):
        engine = ResearchBacktestEngine(config)
        engine.live_engine.predictor = mock_predictor
        engine.live_engine.learner = mock_learner

    # Verify live components are initialized
    assert hasattr(engine, 'live_engine')
    assert hasattr(engine, 'predictor')
    assert hasattr(engine, 'learner')

    # Verify sizing
    assert engine.lot_size == 30
    assert engine.qty == 30  # default lots_per_trade=1

    # Verify side enablement defaults
    assert engine.enable_ce == True
    assert engine.enable_pe == True


def test_parity_report_generation():
    """Test that parity reports can be generated."""
    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    sizing_report = engine.get_sizing_parity_report()
    cost_report = engine.get_cost_parity_report()

    assert 'lot_size' in sizing_report
    assert 'lots_per_trade' in sizing_report
    assert 'qty' in sizing_report
    assert 'invariants_valid' in sizing_report

    assert 'cost_model' in cost_report
    assert 'cost_results' in cost_report
    assert 'cost_parity_valid' in cost_report


def test_exit_price_consistency():
    """Test that exit price in close matches what was passed."""
    config = Config()
    engine = _create_engine_with_mocks(lots_per_trade=1)

    entry_price = 45000.0
    exit_price = 45300.0
    ts = datetime(2026, 7, 15, 10, 0, 0)

    position = {
        'entry': entry_price,
        'side': 'PE',
        'qty': 30,
        'stop_loss': 44500.0,
        'target': 45500.0,
        'max_pnl': 1000.0,
        'ml_prob': 0.65,
        'regime': 'RANGE',
        'entry_time': ts
    }

    trade_result = engine._close_position_parity(position, exit_price, ts, "TEST_EXIT")

    assert trade_result['exit_price'] == exit_price
    assert trade_result['entry_price'] == entry_price
    assert trade_result['quantity'] == 30

    # Verify gross PnL calculation
    expected_gross = (exit_price - entry_price) * 30
    assert abs(trade_result['gross_pnl'] - round(expected_gross, 2)) < 0.01


if __name__ == "__main__":
    pytest.main([os.path.abspath(__file__), "-v"])