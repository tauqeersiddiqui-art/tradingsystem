# engine/storage/integration.py
#
# Integration hooks for PostgreSQL persistence layer.
# These are the ONLY places where DB writes should be called from trading logic.
#
# CRITICAL: All hooks are non-blocking — DB failures do not interrupt execution.

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from engine.storage.postgres_client import get_client
from engine.execution.cost_model import round_trip_cost

logger = logging.getLogger("storage_integration")


# ── ORDER HOOKS ───────────────────────────────────────────────────────

def on_order_placed(order_id: str, symbol: str, side: str,
                   qty: int, price: Optional[float] = None) -> bool:
    """
    Hook: Called immediately after order placement.

    Integration point:
        execution_engine.py :: execute_entry() / execute_exit()
        Right after broker.place_order() returns.

    Returns:
        True if persisted, False otherwise (non-blocking).
    """
    client = get_client()
    success = client.insert_order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        status="PLACED"
    )

    if not success:
        logger.warning(f"[STORAGE] Failed to persist order: {order_id}")

    return success


def on_order_complete(order_id: str, fill_price: float) -> bool:
    """
    Hook: Called when order status becomes COMPLETE.

    Integration point:
        execution_engine.py :: _poll_order()
        When broker confirms fill.

    Returns:
        True if persisted, False otherwise (non-blocking).
    """
    client = get_client()
    success = client.update_order_status(
        order_id=order_id,
        status="COMPLETE",
        price=fill_price
    )

    if not success:
        logger.warning(f"[STORAGE] Failed to update order: {order_id}")

    return success


def on_order_cancelled(order_id: str) -> bool:
    """
    Hook: Called when order is cancelled.

    Integration point:
        execution_engine.py :: _poll_order()
        When broker reports CANCELLED status.
    """
    client = get_client()
    return client.update_order_status(order_id=order_id, status="CANCELLED")


def on_order_rejected(order_id: str) -> bool:
    """
    Hook: Called when order is rejected by broker.

    Integration point:
        execution_engine.py :: _poll_order()
        When broker reports REJECTED status.
    """
    client = get_client()
    return client.update_order_status(order_id=order_id, status="REJECTED")


# ── POSITION HOOKS ────────────────────────────────────────────────────

def on_position_open(symbol: str, side: str, qty: int,
                    entry_price: float, entry_order_id: str,
                    ml_prob: Optional[float] = None,
                    regime: Optional[str] = None) -> Optional[int]:
    """
    Hook: Called when entry order becomes COMPLETE (position is live).

    Integration point:
        master_runner.py :: after finalize_entry() confirms entry order complete
        OR during recovery when a position is restored.

    Returns:
        Database position ID (used for subsequent updates) or None on failure.
    """
    client = get_client()
    pos_id = client.insert_position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        entry_order_id=entry_order_id,
        ml_prob=ml_prob,
        regime=regime
    )

    if pos_id is None:
        logger.error(f"[STORAGE] Failed to persist position: {symbol}")

    return pos_id


def on_position_update(position_id: int, **kwargs) -> bool:
    """
    Hook: Called when position state changes (SL order, max_pnl, etc).

    Integration point:
        master_runner.py :: after finalize_entry() stores SL order ID
        master_runner.py :: every cycle when max_pnl/min_pnl updates

    kwargs:
        sl_order_id: str
        max_pnl: float
        min_pnl: float
        ... any other position field
    """
    if not position_id:
        logger.warning("[STORAGE] on_position_update called with no position_id")
        return False

    client = get_client()
    return client.update_position(position_id, **kwargs)


def on_position_close(position_id: int, exit_price: float,
                     exit_reason: str, gross_pnl: float,
                     net_pnl: float, strategy: str,
                     exit_time: Optional[datetime] = None) -> bool:
    """
    Hook: Called when exit order becomes COMPLETE (position is closed).

    Integration point:
        master_runner.py :: after finalize_exit() confirms exit order complete

    This hook:
        1. Marks position as CLOSED
        2. Creates trade record with net PnL

    Returns:
        True if persisted, False otherwise (non-blocking).
    """
    if not position_id:
        logger.warning("[STORAGE] on_position_close called with no position_id")
        return False

    client = get_client()
    success = client.close_position(
        position_id=position_id,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        strategy=strategy,
        exit_time=exit_time
    )

    if not success:
        logger.error(f"[STORAGE] Failed to close position: {position_id}")

    return success


# ── SYSTEM STATE HOOKS ────────────────────────────────────────────────

def sync_session_state(ctx) -> bool:
    """
    Hook: Called every cycle to persist runtime state.

    Integration point:
        master_runner.py :: end of each cycle (replaces save_state())

    Persists:
        - session_date (prevents stale state reuse)
        - pnl (net realized PnL)
        - gross_pnl (gross realized PnL)
        - trades_today (count of completed trades)
        - scalp_pnl_today (scalp PnL)
        - scalp_trades_today (scalp trade count)
        - daily_profit_locked (risk gate)
    """
    client = get_client()
    from datetime import date

    # Session metadata
    client.set_state("session_date", date.today().isoformat())
    client.set_state("saved_at", datetime.now().isoformat())

    # PnL state (authoritative)
    client.set_state("pnl", float(getattr(ctx, "pnl", 0.0)))
    client.set_state("gross_pnl", float(getattr(ctx, "gross_pnl", 0.0)))
    client.set_state("trades_today", int(getattr(ctx, "trades_today", 0)))

    # Scalp state (if exists)
    scalp = getattr(ctx, "scalp_engine", None)
    if scalp:
        client.set_state("scalp_pnl_today", float(getattr(scalp, "pnl_today", 0.0)))
        client.set_state("scalp_trades_today", int(getattr(scalp, "trades_today", 0)))

    # Risk gates
    client.set_state("daily_profit_locked", bool(getattr(ctx, "daily_profit_locked", False)))

    logger.debug(f"[STORAGE] Session state synced: pnl={ctx.pnl:.2f}, trades={ctx.trades_today}")
    return True


def load_session_state(ctx) -> bool:
    """
    Hook: Called at startup to restore session state.

    Integration point:
        master_runner.py :: startup (replaces load_state())

    Returns:
        True if state was restored, False otherwise.
    """
    client = get_client()
    from datetime import date

    # Check if state is from today
    session_date = client.get_state("session_date")
    if session_date != date.today().isoformat():
        logger.info("[STORAGE] No valid session state from today")
        return False

    # Restore PnL state
    ctx.pnl = float(client.get_state("pnl", 0.0))
    ctx.gross_pnl = float(client.get_state("gross_pnl", 0.0))
    ctx.trades_today = int(client.get_state("trades_today", 0))

    # Restore scalp state (if exists)
    scalp = getattr(ctx, "scalp_engine", None)
    if scalp:
        scalp.pnl_today = float(client.get_state("scalp_pnl_today", 0.0))
        scalp.trades_today = int(client.get_state("scalp_trades_today", 0))

    # Restore risk gates
    ctx.daily_profit_locked = bool(client.get_state("daily_profit_locked", False))

    logger.info(f"[STORAGE] Session state restored: pnl={ctx.pnl:.2f}, trades={ctx.trades_today}")
    return True


def get_open_positions_from_db() -> list:
    """
    Hook: Called at startup to recover open positions.

    Integration point:
        master_runner.py :: startup recovery (replaces deserialize_position())

    Returns:
        List of open position dicts (compatible with runtime format).
    """
    client = get_client()
    db_positions = client.get_open_positions()

    # Convert DB format to runtime format
    positions = []
    for db_pos in db_positions:
        pos = {
            "_db_id": db_pos["id"],  # Store DB ID for future updates
            "symbol": db_pos["symbol"],
            "side": db_pos["side"],
            "qty": db_pos["qty"],
            "entry": db_pos["entry_price"],
            "entry_price": db_pos["entry_price"],
            "entry_order_id": db_pos["entry_order_id"],
            "sl_order_id": db_pos["sl_order_id"],
            "max_pnl": float(db_pos["max_pnl"] or 0),
            "min_pnl": float(db_pos["min_pnl"] or 0),
            "ml_prob": float(db_pos["ml_prob"]) if db_pos["ml_prob"] else None,
            "regime": db_pos["regime"],
            "entry_ts": db_pos["created_at"],
        }
        positions.append(pos)

    logger.info(f"[STORAGE] Recovered {len(positions)} open position(s) from DB")
    return positions


# ── ANALYTICS QUERIES ─────────────────────────────────────────────────

def get_today_summary() -> Dict[str, Any]:
    """
    Fetch today's trade summary from DB.

    Returns:
        Dict with trade_count, net_pnl, gross_pnl, winners, losers.
    """
    client = get_client()
    trades = client.get_today_trades()

    if not trades:
        return {
            "trade_count": 0,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "winners": 0,
            "losers": 0,
        }

    net_pnl = sum(float(t["net_pnl"]) for t in trades)
    gross_pnl = sum(float(t["gross_pnl"]) for t in trades)
    winners = sum(1 for t in trades if float(t["net_pnl"]) > 0)
    losers = sum(1 for t in trades if float(t["net_pnl"]) <= 0)

    return {
        "trade_count": len(trades),
        "net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "winners": winners,
        "losers": losers,
    }
