# engine/analytics/execution_audit.py
#
# Execution audit logger — records execution quality metrics per trade.
#
# Tracks:
#   - Slippage (expected vs actual fill price)
#   - SL trigger accuracy (expected vs actual)
#   - Execution latency (order placement to fill)
#   - Partial fill incidents
#
# Used for:
#   - Execution quality monitoring
#   - Broker performance analysis
#   - Cost model validation
#   - Alert on excessive slippage

import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger("execution_audit")

# Audit log file
_AUDIT_PATH = os.path.join("data", "execution_audit.jsonl")

# Slippage alert thresholds (points)
_SLIPPAGE_WARN_PTS = 2.0
_SLIPPAGE_CRITICAL_PTS = 5.0


@dataclass
class ExecutionAudit:
    """Single trade execution audit record."""

    # Trade identification
    trade_id: str
    symbol: str
    side: str
    qty: int

    # Entry execution
    expected_entry_price: Optional[float]  # Signal price
    actual_entry_price: float              # Fill price
    entry_slippage_pts: float              # Actual - expected (pts)
    entry_slippage_rs: float               # Slippage cost (Rs)
    entry_order_id: str
    entry_placed_at: str                   # ISO timestamp
    entry_filled_at: str                   # ISO timestamp
    entry_latency_ms: float                # Placement → fill (ms)

    # Exit execution
    expected_exit_price: Optional[float]   # Target/SL level
    actual_exit_price: float               # Fill price
    exit_slippage_pts: float               # Actual - expected (pts)
    exit_slippage_rs: float                # Slippage cost (Rs)
    exit_order_id: str
    exit_placed_at: str
    exit_filled_at: str
    exit_latency_ms: float

    # SL accuracy
    sl_trigger_level: Optional[float]      # Configured SL
    sl_actual_fill: Optional[float]        # Actual exit when SL triggered
    sl_gap_pts: Optional[float]            # Gap below trigger (slippage)

    # Fill status
    entry_partial: bool                    # True if entry was partial fill
    exit_partial: bool                     # True if exit was partial fill

    # Timestamps
    timestamp: str                         # Audit creation time


def log_execution_audit(
    trade_id: str,
    symbol: str,
    side: str,
    qty: int,
    # Entry details
    expected_entry_price: Optional[float],
    actual_entry_price: float,
    entry_order_id: str,
    entry_placed_at: datetime,
    entry_filled_at: datetime,
    # Exit details
    expected_exit_price: Optional[float],
    actual_exit_price: float,
    exit_order_id: str,
    exit_placed_at: datetime,
    exit_filled_at: datetime,
    # Fill status
    entry_partial: bool = False,
    exit_partial: bool = False,
    # SL details
    sl_trigger_level: Optional[float] = None,
    sl_actual_fill: Optional[float] = None,
) -> ExecutionAudit:
    """
    Record execution audit for a completed trade.

    Args:
        trade_id: Unique trade identifier
        symbol: Option symbol
        side: CE / PE
        qty: Position size (actual filled qty)
        expected_entry_price: Expected entry price (signal LTP)
        actual_entry_price: Actual fill price
        entry_order_id: Entry order ID
        entry_placed_at: Entry order placement time
        entry_filled_at: Entry order fill time
        entry_partial: True if entry was partial fill
        expected_exit_price: Expected exit (target/SL level)
        actual_exit_price: Actual exit fill
        exit_order_id: Exit order ID
        exit_placed_at: Exit order placement time
        exit_filled_at: Exit order fill time
        exit_partial: True if exit was partial fill
        sl_trigger_level: Virtual SL trigger level (if exit was SL)
        sl_actual_fill: Actual fill price when SL triggered

    Returns:
        ExecutionAudit record
    """
    # Calculate slippage
    entry_slippage_pts = 0.0
    if expected_entry_price:
        entry_slippage_pts = actual_entry_price - expected_entry_price

    exit_slippage_pts = 0.0
    if expected_exit_price:
        exit_slippage_pts = actual_exit_price - expected_exit_price

    entry_slippage_rs = abs(entry_slippage_pts * qty)
    exit_slippage_rs = abs(exit_slippage_pts * qty)

    # Calculate latency
    entry_latency_ms = (entry_filled_at - entry_placed_at).total_seconds() * 1000
    exit_latency_ms = (exit_filled_at - exit_placed_at).total_seconds() * 1000

    # Calculate SL gap (if applicable)
    sl_gap_pts = None
    if sl_trigger_level and sl_actual_fill:
        sl_gap_pts = sl_trigger_level - sl_actual_fill  # Gap below trigger

    # Create audit record
    audit = ExecutionAudit(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        qty=qty,
        expected_entry_price=expected_entry_price,
        actual_entry_price=actual_entry_price,
        entry_slippage_pts=round(entry_slippage_pts, 2),
        entry_slippage_rs=round(entry_slippage_rs, 2),
        entry_order_id=entry_order_id,
        entry_placed_at=entry_placed_at.isoformat(),
        entry_filled_at=entry_filled_at.isoformat(),
        entry_latency_ms=round(entry_latency_ms, 1),
        expected_exit_price=expected_exit_price,
        actual_exit_price=actual_exit_price,
        exit_slippage_pts=round(exit_slippage_pts, 2),
        exit_slippage_rs=round(exit_slippage_rs, 2),
        exit_order_id=exit_order_id,
        exit_placed_at=exit_placed_at.isoformat(),
        exit_filled_at=exit_filled_at.isoformat(),
        exit_latency_ms=round(exit_latency_ms, 1),
        sl_trigger_level=sl_trigger_level,
        sl_actual_fill=sl_actual_fill,
        sl_gap_pts=round(sl_gap_pts, 2) if sl_gap_pts else None,
        entry_partial=entry_partial,
        exit_partial=exit_partial,
        timestamp=datetime.now().isoformat()
    )

    # Write to JSONL file
    try:
        os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
        with open(_AUDIT_PATH, "a") as f:
            f.write(json.dumps(asdict(audit)) + "\n")
    except Exception as e:
        logger.error(f"[AUDIT] Failed to write audit log: {e}")

    # Log summary
    logger.info(
        f"[AUDIT] {symbol} {side}: "
        f"entry_slip={entry_slippage_pts:+.2f}pts, "
        f"exit_slip={exit_slippage_pts:+.2f}pts, "
        f"entry_lat={entry_latency_ms:.0f}ms, "
        f"exit_lat={exit_latency_ms:.0f}ms"
    )

    # Alert on excessive slippage
    total_slippage_pts = abs(entry_slippage_pts) + abs(exit_slippage_pts)

    if total_slippage_pts >= _SLIPPAGE_CRITICAL_PTS:
        logger.error(
            f"[AUDIT] CRITICAL SLIPPAGE: {total_slippage_pts:.2f}pts on {symbol} "
            f"(entry={entry_slippage_pts:+.2f}, exit={exit_slippage_pts:+.2f})"
        )
        _send_slippage_alert(audit, "CRITICAL")
    elif total_slippage_pts >= _SLIPPAGE_WARN_PTS:
        logger.warning(
            f"[AUDIT] High slippage: {total_slippage_pts:.2f}pts on {symbol}"
        )

    # Alert on SL gap (when actual fill is far below trigger)
    if sl_gap_pts and sl_gap_pts > 3.0:
        logger.warning(
            f"[AUDIT] SL SLIPPAGE: trigger={sl_trigger_level:.2f}, "
            f"fill={sl_actual_fill:.2f}, gap={sl_gap_pts:.2f}pts"
        )

    return audit


def _send_slippage_alert(audit: ExecutionAudit, severity: str):
    """Send Telegram alert for excessive slippage."""
    try:
        from telegram.notifier import send_alert

        total_slip = abs(audit.entry_slippage_pts) + abs(audit.exit_slippage_pts)
        total_cost = audit.entry_slippage_rs + audit.exit_slippage_rs

        emoji = "⚠️" if severity == "WARN" else "🚨"

        msg = (
            f"{emoji} {severity}: EXECUTION SLIPPAGE\n\n"
            f"Trade: {audit.symbol} {audit.side}\n"
            f"Total slippage: {total_slip:.2f}pts (Rs{total_cost:.0f})\n\n"
            f"Entry: {audit.entry_slippage_pts:+.2f}pts\n"
            f"Exit: {audit.exit_slippage_pts:+.2f}pts\n\n"
            f"Review execution quality"
        )

        send_alert(msg)
    except Exception as e:
        logger.error(f"[AUDIT] Failed to send slippage alert: {e}")


def get_today_audit_summary() -> Dict[str, Any]:
    """
    Return execution quality summary for today.

    Returns:
        Dict with avg slippage, latency, partial fill count, etc.
    """
    try:
        if not os.path.exists(_AUDIT_PATH):
            return {"status": "no_data"}

        today = datetime.now().date()
        audits = []

        with open(_AUDIT_PATH, "r") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    record_date = datetime.fromisoformat(record["timestamp"]).date()
                    if record_date == today:
                        audits.append(record)
                except Exception:
                    continue

        if not audits:
            return {"status": "no_data_today"}

        # Calculate averages
        entry_slips = [abs(a["entry_slippage_pts"]) for a in audits]
        exit_slips = [abs(a["exit_slippage_pts"]) for a in audits]
        entry_lats = [a["entry_latency_ms"] for a in audits]
        exit_lats = [a["exit_latency_ms"] for a in audits]

        partial_entries = sum(1 for a in audits if a["entry_partial"])
        partial_exits = sum(1 for a in audits if a["exit_partial"])

        total_slip_cost = sum(
            a["entry_slippage_rs"] + a["exit_slippage_rs"] for a in audits
        )

        return {
            "status": "ok",
            "trade_count": len(audits),
            "avg_entry_slippage_pts": round(sum(entry_slips) / len(entry_slips), 2),
            "avg_exit_slippage_pts": round(sum(exit_slips) / len(exit_slips), 2),
            "avg_entry_latency_ms": round(sum(entry_lats) / len(entry_lats), 1),
            "avg_exit_latency_ms": round(sum(exit_lats) / len(exit_lats), 1),
            "partial_fill_count": partial_entries + partial_exits,
            "total_slippage_cost_rs": round(total_slip_cost, 2),
        }

    except Exception as e:
        logger.error(f"[AUDIT] Failed to generate summary: {e}")
        return {"status": "error", "error": str(e)}


def validate_cost_model(actual_costs: Dict[int, float]) -> Dict[str, Any]:
    """
    Validate cost model against actual measured slippage.

    Args:
        actual_costs: Dict mapping qty → actual total cost (Rs)

    Returns:
        Validation report with cost model accuracy
    """
    from engine.execution.cost_model import round_trip_cost

    results = []

    for qty, actual_cost in actual_costs.items():
        expected_cost = round_trip_cost(qty)
        diff = actual_cost - expected_cost
        diff_pct = (diff / expected_cost) * 100 if expected_cost > 0 else 0

        results.append({
            "qty": qty,
            "expected_cost": round(expected_cost, 2),
            "actual_cost": round(actual_cost, 2),
            "diff": round(diff, 2),
            "diff_pct": round(diff_pct, 1)
        })

    # Flag if model is systematically off
    avg_diff_pct = sum(r["diff_pct"] for r in results) / len(results)

    status = "OK"
    if abs(avg_diff_pct) > 20:
        status = "NEEDS_CALIBRATION"
        logger.warning(f"[AUDIT] Cost model off by {avg_diff_pct:.1f}% — consider recalibration")

    return {
        "status": status,
        "avg_diff_pct": round(avg_diff_pct, 1),
        "results": results
    }
