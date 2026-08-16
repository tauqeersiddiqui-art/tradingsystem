# research/backtest/tests/test_qty_lot_invariants.py
# Ensure backtest trade quantities are multiples of LOT_SIZE=30

import csv
from pathlib import Path


def test_qty_multiples_of_lot():
    """Verify that all trade quantities in backtest results are multiples of 30 (Bank Nifty lot size)."""
    trade_log_path = Path("research/backtest/results/trade_log.csv")

    # If the file doesn't exist, skip the test (no backtest run yet)
    if not trade_log_path.exists():
        import pytest
        pytest.skip("research/backtest/results/trade_log.csv not found - run a backtest first")

    with open(trade_log_path, newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            q_str = row.get("qty", "0").strip()
            if not q_str:
                continue
            q = int(q_str)
            assert q % 30 == 0, f"Row {i}: invalid qty {q} (not multiple of 30)"