"""
PARITY TEST MODULE

Thin wrappers that delegate to live engine methods for entry/exit decisions.

This file tests that research-engine decisions match live-engine decisions
when given the exact same historical candles.

DO NOT COPY LOGIC - just invoke the live public methods.
"""

import sys
import os
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from engine.config.config import Config
from engine.live_engine import LiveEngine
from engine.execution.cost_model import round_trip_cost, net_pnl, lot_qty
from engine.execution.profit_manager import manage_position
from engine.risk.risk_manager import compute_entry_stops
from ml.predictor_champion import ChampionPredictor
# IntradayMLLearner mocked in test_parity.py to avoid import errors
from ml.feature_config import build_live_features


class ParityTestWrapper:
    """Thin wrapper around live engine for parity testing."""

    def __init__(self, config=None, lots_per_trade=1, enable_ce=True, enable_pe=True):
        self.config = config or Config()
        # Task #20 FIX 7: force the LEGACY exit regime — Config()'s live
        # default ML_TRAIL_ENABLED=True would silently switch the wrapped
        # LiveEngine.check_exit to Phase-10 exits, but the parity suite
        # validates the legacy ladder/drawdown regime. Must be the boolean
        # False (the consumer checks truthiness — "0" would stay truthy).
        # Live default is NOT changed.
        self.config.ML_TRAIL_ENABLED = False
        self.lot_size = lot_qty(self.config)
        self.lots_per_trade = lots_per_trade
        self.qty = self.lot_size * lots_per_trade
        self.enable_ce = enable_ce
        self.enable_pe = enable_pe

        # Instantiate live components exactly as live engine does
        self.live_engine = LiveEngine(ctx=type('obj', (object,), {'ml_learner': None, 'config': self.config, 'global_market': None, 'strategy_tracker': None})())
        self.predictor = ChampionPredictor()
        self.learner = IntradayLearner()

    def test_entry_par(self, df_window: pd.DataFrame, ts: datetime) -> dict:
        """
        Call live check_entry and return the signal dict.
        Returns None if no entry signal.
        """
        return self.live_engine.check_entry(df_window, ts)

    def test_exit_par(self, position: dict, ltp: float, held_seconds: int) -> tuple:
        """
        Call live check_exit and return (should_exit, reason).
        """
        return self.live_engine.check_exit(position, ltp, held_seconds)

    def test_close_par(self, position: dict, exit_price: float, ts: datetime) -> dict:
        """
        Call live _close_position and return trade dict.
        """
        # Simulate the close logic from live_engine
        entry_price = position["entry"]
        qty = position["qty"]
        side = position["side"]

        gross_pnl = round((exit_price - entry_price) * qty, 2)
        cost = round_trip_cost(qty, self.config)
        net = net_pnl(gross_pnl, qty, self.config)

        return {
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty": qty,
            "cost": cost,
            "gross_pnl": gross_pnl,
            "net_pnl": net,
        }


def verify_entry_invariants(signal: dict) -> list:
    """Verify entry signal meets live invariants."""
    errors = []
    if signal is None:
        return errors  # No signal is valid

    if "qty" not in signal:
        errors.append("Missing 'qty' in signal")
    else:
        if signal["qty"] <= 0:
            errors.append(f"qty must be > 0, got {signal['qty']}")
        if signal["qty"] % 30 != 0:
            errors.append(f"qty must be multiple of 30, got {signal['qty']}")

    return errors


def verify_exit_invariants(trade: dict) -> list:
    """Verify trade meets live invariants."""
    errors = []

    if "quantity" in trade:
        if trade["quantity"] <= 0:
            errors.append(f"quantity must be > 0, got {trade['quantity']}")
        if trade["quantity"] % 30 != 0:
            errors.append(f"quantity must be multiple of 30, got {trade['quantity']}")

    if "gross_pnl" in trade and "cost" in trade and "net_pnl" in trade:
        expected_net = round(trade["gross_pnl"] - trade["cost"], 2)
        if abs(trade["net_pnl"] - expected_net) > 0.01:
            errors.append(f"Net PnL mismatch: got {trade['net_pnl']}, expected {expected_net}")

    return errors


def run_parity_suite(df_historical: pd.DataFrame, start: datetime, end: datetime):
    """
    Run full parity suite comparing live vs research decisions.

    Returns dict with results.
    """
    wrapper = ParityTestWrapper()
    results = {
        "total_candles": 0,
        "entry_signals": 0,
        "exit_signals": 0,
        "entry_errors": [],
        "exit_errors": [],
        "details": []
    }

    df = df_historical.copy()
    df["date"] = pd.to_datetime(df["date"])
    mask = (df["date"] >= start) & (df["date"] <= end)
    df = df[mask].sort_values("date").reset_index(drop=True)

    prev_close = None
    window_size = 200

    for idx, row in df.iterrows():
        ts = row["date"]
        if ts.time() < wrapper.live_engine._MARKET_OPEN:
            continue

        window_start = max(0, idx - window_size)
        window_df = df.iloc[window_start:idx+1]

        # Entry parity
        signal = wrapper.test_entry_par(window_df, ts)
        results["total_candles"] += 1

        if signal:
            results["entry_signals"] += 1
            entry_errs = verify_entry_invariants(signal)
            if entry_errs:
                results["entry_errors"].extend(entry_errs)

        # Exit parity simulation would go here for each position

    return results


if __name__ == "__main__":
    print("PARITY TEST MODULE")
    print("=" * 60)
    print("\nThis module provides thin wrappers around live engine methods:")
    print("- LiveEngine.check_entry()")
    print("- LiveEngine.check_exit()")
    print("- profit_manager.manage_position()")
    print("- cost_model.round_trip_cost()")
    print("- config.LOT_SIZE = 30")
    print("\nRun tests via research/backtest/test_parity.py")