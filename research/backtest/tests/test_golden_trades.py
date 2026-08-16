# research/backtest/test_golden_trades.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pytest
from research.backtest.tests.golden_trades import canonical_cases, as_dict_list
from research.backtest.wrapper import ResearchWrapper

# adapt actual import path for ChampionPredictor used by live engine
# LiveEngine uses ml.predictor_champion.ChampionPredictor
PREDICTOR_PATH = "ml.predictor_champion.ChampionPredictor.predict"
# LiveEngine feeds use IntradayMLLearner which feeds on candles; no separate PriceFeed class in live code
# We'll monkeypatch the learner's update_candle instead of a separate feed class
PRICE_FEED_PATH = "ml.ml_intraday_learner.IntradayMLLearner.update_candle"

def fake_predict_factory(value):
    def fake_predict(features, direction=None):
        return value
    return fake_predict

class FakePriceFeed:
    def __init__(self, path):
        self.path = path
        self.i = 0
    def current_price(self, symbol):
        return self.path[min(self.i, len(self.path)-1)]
    def advance(self):
        self.i += 1
    def reset(self):
        self.i = 0

def stub_feed(monkeypatch, price_path):
    fake = FakePriceFeed(price_path)
    # monkeypatch the class/constructor used by live engine to create a feed
    monkeypatch.setattr(PRICE_FEED_PATH, lambda *args, **kwargs: fake)
    return fake

@pytest.fixture
def live_engine_instance(monkeypatch):
    # Import here to let tests monkeypatch predictor/feed before LiveEngine instantiation if needed
    from engine.live_engine import LiveEngine
    from engine.config.config import Config
    from unittest.mock import MagicMock, Mock

    # Create a minimal context object for LiveEngine
    config = Config()
    class Context:
        def __init__(self, config):
            self.config = config
            self.ml_learner = self
            self.global_market = None
            self.strategy_tracker = None

    ctx = Context(config)

    # Create mocks exactly like in test_parity.py
    def _make_mock_predictor():
        predictor = Mock()
        # This will be overridden by the specific test case's ml_prob via monkeypatch
        predictor.predict.side_effect = lambda features_dict, direction: 0.64  # default PE prob
        predictor.ce_threshold = 0.5
        predictor.pe_threshold = 0.5
        return predictor

    def _make_mock_learner():
        learner = Mock()
        learner.get_ml_threshold.return_value = 0.55
        learner.get_adjusted_ml_prob.side_effect = lambda ce, pe, side: (0.70 if side == "CE" else 0.62)
        learner.get_day_type.return_value = "RANGE"
        learner.should_exit_early.return_value = (False, "")
        learner.is_side_blocked.return_value = False
        learner.update_candle = Mock()
        learner.set_open_price = Mock()
        learner.get_confidence_adjustment = Mock(return_value=1.0)
        return learner

    # Create mocks
    mock_predictor = _make_mock_predictor()
    mock_learner = _make_mock_learner()

    # Patch the classes to return our mocks
    monkeypatch.setattr('engine.live_engine.ChampionPredictor', lambda: mock_predictor)
    monkeypatch.setattr('engine.live_engine.IntradayMLLearner', lambda: mock_learner)

    # Create LiveEngine with context
    engine = LiveEngine(ctx)
    # Override the predictor and learner instances with our mocks
    engine.predictor = mock_predictor
    engine.learner = mock_learner

    return engine, mock_predictor

@pytest.fixture
def research_wrapper(live_engine_instance):
    live_engine, mock_predictor = live_engine_instance
    return ResearchWrapper(live_engine), mock_predictor

@pytest.mark.parametrize("case", as_dict_list())
def test_golden_case_parity(case, research_wrapper, monkeypatch):
    research_wrapper, mock_predictor = research_wrapper
    # 1) stub predictor to return specified ml_prob
    mock_predictor.predict.return_value = case['ml_prob']

    # 2) build a price path to force the expected exit reason
    if case['expected_exit_reason'] == "TARGET":
        price_path = [case['expected_entry_price'], case['expected_target']]
    elif case['expected_exit_reason'] == "STOP":
        price_path = [case['expected_entry_price'], case['expected_stop']]
    elif case['expected_exit_reason'] == "TIME_EXIT":
        price_path = [case['expected_entry_price']] * 10
    else:
        price_path = [case['expected_entry_price'], case['expected_exit_price']]

    fake_feed = stub_feed(monkeypatch, price_path)

    # 3) Simulate single candle via wrapper, providing the fake feed to drive the simulation
    record = research_wrapper.simulate_single_candle(case['candle_time'], case, price_feed=fake_feed)

    # Debug: print what we got
    print(f"\nDEBUG: case={case['id']}")
    print(f"DEBUG: record={record}")
    print(f"DEBUG: expected_entry={case['expected_entry']}")
    print(f"DEBUG: record['entry_taken']={record.get('entry_taken')}")

    # For the wrapper fallback which doesn't run the loop, tests might need to trigger engine loops:
    # If the wrapper didn't produce exit_price because live engine expects the test harness to advance the feed,
    # the test harness should simulate that by calling engine.check_exit/manage_position as needed.
    # For many live engine implementations this will be automatic if execute_entry_simulated exists.

    # Assertions: basic structure parity
    assert record['case_id'] == case['id']
    assert record['entry_taken'] == case['expected_entry']
    if case['expected_entry']:
        assert record['lots'] == case['expected_qty_lots']
        # numeric comparisons: allow small tolerance
        assert abs(record['entry_price'] - case['expected_entry_price']) < 0.5
        assert record['exit_reason'] == case['expected_exit_reason']
        assert abs(record['exit_price'] - case['expected_exit_price']) < 0.5
        # verify net pnl arithmetic if fields present
        if record.get('exit_price') is not None and record.get('entry_price') is not None:
            expected_gross = (record['exit_price'] - record['entry_price']) * record['qty']
            # cost may be missing in fallback; only assert net_pnl if cost is present
            if record.get('cost') is not None:
                assert abs(record['net_pnl'] - (expected_gross - record['cost'])) < 1e-3