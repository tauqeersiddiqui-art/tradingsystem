# engine/execution/cost_model.py
#
# AUTHORITATIVE source of truth for round-trip cost and net-vs-gross PnL.
# Every consumer (master_runner, profit_manager, trade_journal,
# services/trade_logger, analytics) must route its cost arithmetic through
# this module so that all risk decisions and analytics agree on ONE number.
#
# Rule: /lot = Rs66. Expense = (qty / LOT_QTY) x 66 rounded to
# the nearest whole lot.  Net PnL is what the account actually keeps: gross
# PnL minus the round-trip cost of the position qty.

from engine.config.config import Config

# Backward-compatible defaults preserved so callers that pass no Config
# (existing production call sites and regression fixtures) keep the prior
# BANKNIFTY behaviour: 30-qty lot, Rs66 round trip per lot. Config, when
# supplied, is the single source of truth and overrides these.
_DEFAULT_LOT_QTY = 30
_DEFAULT_COST_PER_LOT = 66.0


def lot_qty(config: Config = None) -> int:
    """Get lot size from Config (single source of truth)."""
    if config is not None:
        return max(1, int(config.LOT_SIZE))
    return _DEFAULT_LOT_QTY


def round_trip_cost(qty, config: Config = None) -> float:
    """Brokerage+charges for a round trip (buy+sell) of `qty` units."""
    cost_per_lot = float(getattr(config, "COST_PER_LOT", _DEFAULT_COST_PER_LOT) or _DEFAULT_COST_PER_LOT) if config else _DEFAULT_COST_PER_LOT
    lots = max(1, round(int(qty or 0) / lot_qty(config)))
    return lots * cost_per_lot


def net_pnl(gross_pnl, qty, config: Config = None) -> float:
    """Net realized PnL = gross PnL minus the round-trip cost of the position."""
    return float(gross_pnl) - round_trip_cost(qty, config)


def gross_net_split(gross_pnl, qty, config: Config = None):
    """Return (net_pnl, round_trip_cost) — convenience for dual reporting."""
    cost = round_trip_cost(qty, config)
    return float(gross_pnl) - cost, cost
