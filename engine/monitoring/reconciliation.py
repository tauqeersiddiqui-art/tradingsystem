# engine/monitoring/reconciliation.py
#
# Broker vs Database reconciliation — critical safety check.
#
# Detects position mismatches between broker state (actual holdings) and
# database state (system of record). Mismatches indicate data loss, orphaned
# positions, or execution failures.
#
# CRITICAL: This module ONLY detects and alerts. It does NOT auto-fix.
# Manual intervention required for all mismatches.
#
# Run schedule:
#   - At startup (before trading begins)
#   - Every 5 minutes during market hours

import logging
from datetime import datetime
from typing import List, Dict, Set, Tuple, Optional

logger = logging.getLogger("reconciliation")

# Alert thresholds
_MISMATCH_ALERT_SENT = {}  # Dedup key → timestamp


class ReconciliationResult:
    """Result of broker vs DB reconciliation check."""

    def __init__(self):
        self.timestamp = datetime.now()
        self.status = "OK"
        self.db_positions: List[Dict] = []
        self.broker_positions: List[Dict] = []
        self.matches: List[str] = []
        self.orphaned_in_broker: List[Dict] = []  # Broker has, DB missing
        self.orphaned_in_db: List[Dict] = []      # DB has, broker missing
        self.qty_mismatches: List[Dict] = []      # Symbol match, qty differs

    def has_critical_mismatch(self) -> bool:
        """Return True if any critical mismatch detected."""
        return (
            len(self.orphaned_in_broker) > 0 or
            len(self.orphaned_in_db) > 0 or
            len(self.qty_mismatches) > 0
        )

    def summary(self) -> str:
        """One-line summary for logging."""
        if not self.has_critical_mismatch():
            return f"Reconciliation OK: {len(self.matches)} positions match"

        parts = []
        if self.orphaned_in_broker:
            parts.append(f"{len(self.orphaned_in_broker)} orphaned in broker")
        if self.orphaned_in_db:
            parts.append(f"{len(self.orphaned_in_db)} orphaned in DB")
        if self.qty_mismatches:
            parts.append(f"{len(self.qty_mismatches)} qty mismatches")

        return f"CRITICAL MISMATCH: {', '.join(parts)}"

    def details(self) -> str:
        """Multi-line detailed report."""
        lines = [
            f"Reconciliation Report — {self.timestamp.strftime('%H:%M:%S')}",
            "=" * 70,
            f"Status: {self.status}",
            f"DB positions: {len(self.db_positions)}",
            f"Broker positions: {len(self.broker_positions)}",
            f"Matches: {len(self.matches)}",
            ""
        ]

        if self.orphaned_in_broker:
            lines.append("⚠ ORPHANED IN BROKER (broker has, DB missing):")
            for pos in self.orphaned_in_broker:
                lines.append(f"  - {pos['symbol']}: qty={pos['qty']}, avg={pos['avg_price']}")
            lines.append("")

        if self.orphaned_in_db:
            lines.append("⚠ ORPHANED IN DB (DB has, broker missing):")
            for pos in self.orphaned_in_db:
                lines.append(f"  - {pos['symbol']}: qty={pos['qty']}, entry={pos['entry_price']}")
            lines.append("")

        if self.qty_mismatches:
            lines.append("⚠ QUANTITY MISMATCHES:")
            for mismatch in self.qty_mismatches:
                lines.append(
                    f"  - {mismatch['symbol']}: "
                    f"DB={mismatch['db_qty']}, broker={mismatch['broker_qty']}"
                )
            lines.append("")

        if not self.has_critical_mismatch():
            lines.append("✓ All positions reconciled")

        return "\n".join(lines)


def reconcile_positions(ctx) -> ReconciliationResult:
    """
    Compare broker positions vs database positions.

    Args:
        ctx: TradingContext with broker and storage references

    Returns:
        ReconciliationResult with detected mismatches

    Alerts:
        Sends Telegram alert on critical mismatch (once per mismatch type per 5 min)
    """
    result = ReconciliationResult()

    try:
        # Fetch DB positions
        from engine.storage.integration import get_open_positions_from_db
        db_positions = get_open_positions_from_db()
        result.db_positions = db_positions

        # Fetch broker positions
        broker = ctx.broker
        if not broker:
            logger.warning("[RECON] Broker not available — skipping reconciliation")
            result.status = "SKIPPED"
            return result

        broker_positions_raw = broker.get_positions()
        # Filter to MIS positions only (day trades)
        broker_positions = [
            p for p in broker_positions_raw
            if p.get("product") == "MIS" and p.get("quantity", 0) != 0
        ]
        result.broker_positions = broker_positions

        # Build symbol sets
        db_map = {p["symbol"]: p for p in db_positions}
        broker_map = {p["tradingsymbol"]: p for p in broker_positions}

        db_symbols = set(db_map.keys())
        broker_symbols = set(broker_map.keys())

        # Find matches
        matches = db_symbols & broker_symbols
        result.matches = list(matches)

        # Check quantity mismatches for matched symbols
        for symbol in matches:
            db_qty = db_map[symbol]["qty"]
            broker_qty = broker_map[symbol]["quantity"]

            if db_qty != broker_qty:
                result.qty_mismatches.append({
                    "symbol": symbol,
                    "db_qty": db_qty,
                    "broker_qty": broker_qty
                })

        # Find orphaned positions
        orphaned_in_broker = broker_symbols - db_symbols
        for symbol in orphaned_in_broker:
            pos = broker_map[symbol]
            result.orphaned_in_broker.append({
                "symbol": symbol,
                "qty": pos["quantity"],
                "avg_price": pos.get("average_price", 0.0),
                "product": pos.get("product", "UNKNOWN")
            })

        orphaned_in_db = db_symbols - broker_symbols
        for symbol in orphaned_in_db:
            pos = db_map[symbol]
            result.orphaned_in_db.append({
                "symbol": symbol,
                "qty": pos["qty"],
                "entry_price": pos["entry_price"],
                "entry_order_id": pos.get("entry_order_id")
            })

        # Set status
        if result.has_critical_mismatch():
            result.status = "CRITICAL_MISMATCH"
            logger.error(f"[RECON] {result.summary()}")
            logger.error(f"\n{result.details()}")

            # Send alert (with deduplication)
            _send_mismatch_alert(ctx, result)
        else:
            result.status = "OK"
            logger.info(f"[RECON] {result.summary()}")

    except Exception as e:
        result.status = "ERROR"
        logger.error(f"[RECON] Reconciliation failed: {e}")

    return result


def _send_mismatch_alert(ctx, result: ReconciliationResult):
    """
    Send Telegram alert for critical mismatches.

    Deduplicates: Only sends once per mismatch type per 5 minutes.
    """
    try:
        from telegram.notifier import send_alert

        now = datetime.now()
        alert_sent = False

        # Alert for orphaned broker positions
        if result.orphaned_in_broker:
            key = "orphaned_broker"
            last_sent = _MISMATCH_ALERT_SENT.get(key)
            if not last_sent or (now - last_sent).total_seconds() > 300:
                symbols = ", ".join([p["symbol"] for p in result.orphaned_in_broker])
                msg = (
                    f"🚨 CRITICAL: BROKER POSITION MISMATCH\n\n"
                    f"Broker has position(s) not in database:\n"
                    f"{symbols}\n\n"
                    f"Action required: Manual investigation"
                )
                send_alert(msg)
                _MISMATCH_ALERT_SENT[key] = now
                alert_sent = True
                logger.warning(f"[RECON] Alert sent: orphaned broker positions")

        # Alert for orphaned DB positions
        if result.orphaned_in_db:
            key = "orphaned_db"
            last_sent = _MISMATCH_ALERT_SENT.get(key)
            if not last_sent or (now - last_sent).total_seconds() > 300:
                symbols = ", ".join([p["symbol"] for p in result.orphaned_in_db])
                msg = (
                    f"🚨 CRITICAL: DATABASE POSITION MISMATCH\n\n"
                    f"Database has position(s) not in broker:\n"
                    f"{symbols}\n\n"
                    f"Possible causes:\n"
                    f"- Position closed at broker but not in system\n"
                    f"- Order placement failed silently\n\n"
                    f"Action required: Manual investigation"
                )
                send_alert(msg)
                _MISMATCH_ALERT_SENT[key] = now
                alert_sent = True
                logger.warning(f"[RECON] Alert sent: orphaned DB positions")

        # Alert for quantity mismatches
        if result.qty_mismatches:
            key = "qty_mismatch"
            last_sent = _MISMATCH_ALERT_SENT.get(key)
            if not last_sent or (now - last_sent).total_seconds() > 300:
                details = "\n".join([
                    f"- {m['symbol']}: DB={m['db_qty']}, Broker={m['broker_qty']}"
                    for m in result.qty_mismatches
                ])
                msg = (
                    f"🚨 CRITICAL: QUANTITY MISMATCH\n\n"
                    f"Position quantities don't match:\n"
                    f"{details}\n\n"
                    f"Possible causes:\n"
                    f"- Partial fill not recorded\n"
                    f"- Manual broker modification\n\n"
                    f"Action required: Manual investigation"
                )
                send_alert(msg)
                _MISMATCH_ALERT_SENT[key] = now
                alert_sent = True
                logger.warning(f"[RECON] Alert sent: quantity mismatches")

        if not alert_sent:
            logger.debug("[RECON] Mismatch alerts already sent recently (deduped)")

    except Exception as e:
        logger.error(f"[RECON] Failed to send alert: {e}")


def reconcile_and_log(ctx) -> bool:
    """
    Run reconciliation and return True if OK.

    Helper for startup checks — returns False on critical mismatch.
    """
    result = reconcile_positions(ctx)

    if result.status == "ERROR":
        logger.error("[RECON] Reconciliation error — trading may be unsafe")
        return False

    if result.has_critical_mismatch():
        logger.error("[RECON] Critical mismatch detected — trading may be unsafe")
        logger.error(result.details())
        return False

    logger.info(f"[RECON] {result.summary()}")
    return True


# Scheduled reconciliation task
def start_reconciliation_monitor(ctx, interval_seconds: int = 300):
    """
    Start background reconciliation monitor (runs every 5 minutes).

    Args:
        ctx: TradingContext
        interval_seconds: Check interval (default 300s = 5 min)

    Returns:
        Thread handle (daemon thread, auto-stops on exit)
    """
    import threading
    import time

    def _monitor_loop():
        logger.info(f"[RECON] Monitor started (interval={interval_seconds}s)")
        while True:
            try:
                time.sleep(interval_seconds)
                reconcile_positions(ctx)
            except Exception as e:
                logger.error(f"[RECON] Monitor error: {e}")

    thread = threading.Thread(target=_monitor_loop, daemon=True, name="reconciliation")
    thread.start()
    return thread
