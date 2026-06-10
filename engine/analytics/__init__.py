"""Post-trade analytics — reads from trade-log CSVs, never modifies trading state."""
from engine.analytics.performance import (
    eod_review,
    regime_breakdown,
    ml_bucket_breakdown,
    drift_check,
    setup_breakdown,
    equity_curve_stats,
)
from engine.analytics.trade_replay import TradeReplay
from engine.analytics.slippage import log_slip, slippage_stats

__all__ = [
    "eod_review", "regime_breakdown", "ml_bucket_breakdown",
    "drift_check", "setup_breakdown", "equity_curve_stats",
    "TradeReplay", "log_slip", "slippage_stats",
]
