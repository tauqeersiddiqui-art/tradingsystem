# research/backtest/golden_trades.py
from dataclasses import dataclass, asdict
from typing import List, Dict

@dataclass
class GoldenTradeCase:
    id: str
    description: str
    candle_time: str
    symbol: str
    direction: str  # "PE" or "CE"
    ml_prob: float
    features: Dict
    expected_entry: bool
    expected_qty_lots: int
    expected_entry_price: float
    expected_stop: float
    expected_target: float
    expected_exit_reason: str
    expected_exit_price: float
    notes: str = ""

def canonical_cases() -> List[GoldenTradeCase]:
    # 10 illustrative cases, adjust numbers to your data / price tick format
    return [
        GoldenTradeCase(
            id="PE_WIN_1",
            description="PE ORB breakout + ML strong -> target hit",
            candle_time="2026-07-01T09:31:00",
            symbol="BANKNIFTY",
            direction="PE",
            ml_prob=0.85,
            features={"vwap_bias": -0.15, "supertrend_1m": "BULL", "regime": "TREND"},
            expected_entry=True,
            expected_qty_lots=2,
            expected_entry_price=107.50,
            expected_stop=106.00,
            expected_target=110.00,
            expected_exit_reason="TARGET",
            expected_exit_price=110.00
        ),
        GoldenTradeCase(
            id="PE_STOP_1",
            description="PE entry then quick reversal -> stop",
            candle_time="2026-07-01T09:50:00",
            symbol="BANKNIFTY",
            direction="PE",
            ml_prob=0.78,
            features={"vwap_bias": -0.02, "supertrend_1m": "NEUTRAL", "regime": "RANGE"},
            expected_entry=True,
            expected_qty_lots=1,
            expected_entry_price=108.20,
            expected_stop=106.80,
            expected_target=110.50,
            expected_exit_reason="STOP",
            expected_exit_price=106.80
        ),
        # Add the remaining 8 cases similarly (CE winner/loser, trailing, ml-exit, time-exit, day-end, broker SL)
    ]

def as_dict_list():
    return [asdict(c) for c in canonical_cases()]