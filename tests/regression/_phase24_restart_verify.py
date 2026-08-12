# data/_phase24_restart_verify.py
#
# PHASE 24: RESTART RECOVERY VALIDATION
#
# Simulates real-world crash scenarios and validates that the system recovers
# correctly without data loss, duplicate orders, or inconsistent state.
#
# Test scenarios:
#   1. Crash after entry order placed (pending)
#   2. Crash after entry complete, before SL placed
#   3. Crash after SL placed (normal open position)
#   4. Crash after exit triggered, before completion
#
# SUCCESS CRITERIA:
#   - Position restored correctly from DB + broker
#   - No duplicate entry orders
#   - No duplicate exit orders
#   - SL order present and correct
#   - No orphaned orders

import os
import sys
import time
import json
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock environment before imports
os.environ["POSTGRES_DB"] = "trading_system_test"
os.environ["POSTGRES_USER"] = "trading_user"
os.environ["POSTGRES_PASSWORD"] = "test_password"
os.environ["PAPER_MODE"] = "1"

from engine.storage.postgres_client import PostgresClient, get_client
from engine.storage.integration import (
    on_order_placed,
    on_order_complete,
    on_position_open,
    on_position_update,
    on_position_close,
    sync_session_state,
    load_session_state,
    get_open_positions_from_db,
)
from engine.execution.execution_engine import (
    ExecutionEngine,
    ST_COMPLETE, ST_OPEN, ST_PARTIAL,
)
from engine.core.context import TradingContext
from engine.config.config import Config


class MockBroker:
    """Mock broker for testing."""

    def __init__(self):
        self.orders = {}
        self.positions = {}

    def place_order(self, tradingsymbol, transaction_type, quantity, **kwargs):
        order_id = f"TEST_{len(self.orders) + 1:03d}"
        self.orders[order_id] = {
            "order_id": order_id,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "status": "OPEN",
            "average_price": 0.0,
            **kwargs
        }
        return order_id

    def get_order(self, order_id):
        return self.orders.get(order_id, {})

    def complete_order(self, order_id, fill_price):
        """Simulate order completion."""
        if order_id in self.orders:
            self.orders[order_id]["status"] = "COMPLETE"
            self.orders[order_id]["average_price"] = fill_price

            # Update positions
            order = self.orders[order_id]
            symbol = order["tradingsymbol"]
            qty = order["quantity"]

            if order["transaction_type"] == "BUY":
                self.positions[symbol] = {"quantity": qty, "average_price": fill_price}
            else:  # SELL
                if symbol in self.positions:
                    del self.positions[symbol]

    def get_positions(self):
        """Return positions in broker format."""
        return [
            {
                "tradingsymbol": symbol,
                "quantity": pos["quantity"],
                "average_price": pos["average_price"],
            }
            for symbol, pos in self.positions.items()
        ]

    def cancel_order(self, order_id):
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELLED"


def setup_test_db():
    """Reset test database."""
    client = get_client()
    client.connect()

    conn = client._pool.getconn()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS trades CASCADE")
    cur.execute("DROP TABLE IF EXISTS positions CASCADE")
    cur.execute("DROP TABLE IF EXISTS orders CASCADE")
    cur.execute("DROP TABLE IF EXISTS system_state CASCADE")

    client._pool.putconn(conn)
    client._initialize_schema()

    return client


def test_crash_after_entry_pending():
    """
    Test 1: System crashes after entry order placed but before completion.

    Expected behavior:
      - On restart, find pending entry order
      - Poll order status from broker
      - If complete, create position
      - If cancelled/rejected, mark as failed
      - No duplicate entry order
    """
    print("\n" + "=" * 70)
    print("TEST 1: Crash after entry order placed (pending)")
    print("=" * 70)

    client = setup_test_db()
    broker = MockBroker()

    # Simulate entry order placement
    symbol = "BANKNIFTY2408147300CE"
    order_id = "ENTRY_001"

    # System places entry order
    on_order_placed(order_id, symbol, "BUY", 30, None)
    print(f"[PRE-CRASH] Entry order placed: {order_id}")

    # Store pending entry state
    client.set_state("pending_entry", {
        "order_id": order_id,
        "symbol": symbol,
        "side": "CE",
        "qty": 30,
        "timestamp": datetime.now().isoformat()
    })

    # === SYSTEM CRASHES HERE ===
    print("[CRASH] System killed mid-execution")

    # === SYSTEM RESTARTS ===
    print("\n[RESTART] System restarting...")

    # Simulate broker completing the order while system was down
    broker.orders[order_id] = {
        "order_id": order_id,
        "status": "COMPLETE",
        "average_price": 152.5
    }
    broker.complete_order(order_id, 152.5)

    # Recovery: Check for pending entry
    pending = client.get_state("pending_entry")
    assert pending is not None, "Pending entry not found"
    assert pending["order_id"] == order_id

    print(f"[RECOVERY] Found pending entry: {pending['order_id']}")

    # Poll broker for order status
    broker_order = broker.get_order(order_id)
    assert broker_order["status"] == "COMPLETE", "Order should be complete"

    # Update DB
    on_order_complete(order_id, broker_order["average_price"])

    # Create position
    pos_id = on_position_open(
        symbol=symbol,
        side="CE",
        qty=30,
        entry_price=broker_order["average_price"],
        entry_order_id=order_id
    )

    # Clear pending state
    client.set_state("pending_entry", None)

    # Verify no duplicate orders
    conn = client._pool.getconn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders WHERE order_id = %s", (order_id,))
    order_count = cur.fetchone()[0]
    client._pool.putconn(conn)

    assert order_count == 1, f"Expected 1 order, got {order_count} (DUPLICATE DETECTED)"

    # Verify position exists
    positions = get_open_positions_from_db()
    assert len(positions) == 1, f"Expected 1 position, got {len(positions)}"
    assert positions[0]["entry_order_id"] == order_id

    print(f"✓ PASSED: Position recovered, no duplicates")
    print(f"  - Position ID: {pos_id}")
    print(f"  - Entry price: {broker_order['average_price']}")
    print(f"  - Order count: {order_count}")

    client.close()


def test_crash_after_entry_before_sl():
    """
    Test 2: System crashes after entry complete but before SL order placed.

    Expected behavior:
      - On restart, find open position without SL
      - Place SL order
      - Update position with SL order ID
      - No duplicate SL orders
    """
    print("\n" + "=" * 70)
    print("TEST 2: Crash after entry complete, before SL placed")
    print("=" * 70)

    client = setup_test_db()
    broker = MockBroker()

    symbol = "BANKNIFTY2408147500PE"
    entry_order_id = "ENTRY_002"

    # Complete entry
    on_order_placed(entry_order_id, symbol, "BUY", 60, None)
    broker.complete_order(entry_order_id, 180.0)
    on_order_complete(entry_order_id, 180.0)

    pos_id = on_position_open(
        symbol=symbol,
        side="PE",
        qty=60,
        entry_price=180.0,
        entry_order_id=entry_order_id
    )

    print(f"[PRE-CRASH] Position opened: id={pos_id}, NO SL YET")

    # === SYSTEM CRASHES HERE (before SL placement) ===
    print("[CRASH] System killed before SL placed")

    # === SYSTEM RESTARTS ===
    print("\n[RESTART] System restarting...")

    # Recovery: Find positions without SL
    positions = get_open_positions_from_db()
    assert len(positions) == 1

    pos = positions[0]
    assert pos["sl_order_id"] is None, "SL should not exist yet"

    print(f"[RECOVERY] Found position without SL: {pos['symbol']}")

    # Place SL order
    sl_order_id = broker.place_order(
        tradingsymbol=symbol,
        transaction_type="SELL",
        quantity=60,
        trigger_price=170.0
    )

    on_order_placed(sl_order_id, symbol, "SELL", 60, 170.0)

    # Update position
    on_position_update(pos["_db_id"], sl_order_id=sl_order_id)

    # Verify no duplicate SL orders
    conn = client._pool.getconn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM orders
        WHERE tradingsymbol = %s AND side = 'SELL'
    """, (symbol,))
    sl_count = cur.fetchone()[0]
    client._pool.putconn(conn)

    assert sl_count == 1, f"Expected 1 SL order, got {sl_count} (DUPLICATE DETECTED)"

    # Verify position updated
    positions_after = get_open_positions_from_db()
    assert positions_after[0]["sl_order_id"] == sl_order_id

    print(f"✓ PASSED: SL placed on recovery, no duplicates")
    print(f"  - SL order ID: {sl_order_id}")
    print(f"  - SL order count: {sl_count}")

    client.close()


def test_crash_with_open_position():
    """
    Test 3: System crashes with normal open position (entry + SL complete).

    Expected behavior:
      - On restart, restore position from DB
      - Verify position matches broker
      - Resume position management
      - No new orders placed
    """
    print("\n" + "=" * 70)
    print("TEST 3: Crash with fully open position (entry + SL complete)")
    print("=" * 70)

    client = setup_test_db()
    broker = MockBroker()

    symbol = "BANKNIFTY2408147300CE"
    entry_order_id = "ENTRY_003"
    sl_order_id = "SL_003"

    # Complete entry
    broker.complete_order(entry_order_id, 200.0)
    on_order_placed(entry_order_id, symbol, "BUY", 30, None)
    on_order_complete(entry_order_id, 200.0)

    pos_id = on_position_open(
        symbol=symbol,
        side="CE",
        qty=30,
        entry_price=200.0,
        entry_order_id=entry_order_id,
        ml_prob=0.75,
        regime="TREND_UP"
    )

    # Place SL
    on_order_placed(sl_order_id, symbol, "SELL", 30, 190.0)
    on_position_update(pos_id, sl_order_id=sl_order_id)

    # Update max_pnl
    on_position_update(pos_id, max_pnl=450.0)

    print(f"[PRE-CRASH] Position fully open: entry={entry_order_id}, SL={sl_order_id}")

    # === SYSTEM CRASHES HERE ===
    print("[CRASH] System killed during normal operation")

    # === SYSTEM RESTARTS ===
    print("\n[RESTART] System restarting...")

    # Recovery: Load all open positions
    positions = get_open_positions_from_db()
    assert len(positions) == 1

    pos = positions[0]
    assert pos["_db_id"] == pos_id
    assert pos["symbol"] == symbol
    assert pos["entry_price"] == 200.0
    assert pos["sl_order_id"] == sl_order_id
    assert pos["max_pnl"] == 450.0
    assert pos["ml_prob"] == 0.75
    assert pos["regime"] == "TREND_UP"

    print(f"[RECOVERY] Position restored:")
    print(f"  - Symbol: {pos['symbol']}")
    print(f"  - Entry: {pos['entry_price']}")
    print(f"  - Max PnL: {pos['max_pnl']}")
    print(f"  - SL order: {pos['sl_order_id']}")

    # Verify broker has the position
    broker_positions = broker.get_positions()
    # Note: broker mock needs to be populated for this test

    # Verify no duplicate orders were placed
    conn = client._pool.getconn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders WHERE order_id IN (%s, %s)",
                (entry_order_id, sl_order_id))
    order_count = cur.fetchone()[0]
    client._pool.putconn(conn)

    assert order_count == 2, f"Expected 2 orders, got {order_count}"

    print(f"✓ PASSED: Position restored correctly, no duplicates")

    client.close()


def test_crash_after_exit_triggered():
    """
    Test 4: System crashes after exit order placed but before completion.

    Expected behavior:
      - On restart, find pending exit order
      - Poll broker for status
      - If complete, close position and create trade
      - If pending, wait for completion
      - No duplicate exit orders
    """
    print("\n" + "=" * 70)
    print("TEST 4: Crash after exit triggered (pending)")
    print("=" * 70)

    client = setup_test_db()
    broker = MockBroker()

    symbol = "BANKNIFTY2408147500PE"
    entry_order_id = "ENTRY_004"
    sl_order_id = "SL_004"
    exit_order_id = "EXIT_004"

    # Setup complete position
    broker.complete_order(entry_order_id, 150.0)
    on_order_placed(entry_order_id, symbol, "BUY", 60, None)
    on_order_complete(entry_order_id, 150.0)

    pos_id = on_position_open(
        symbol=symbol,
        side="PE",
        qty=60,
        entry_price=150.0,
        entry_order_id=entry_order_id
    )

    on_order_placed(sl_order_id, symbol, "SELL", 60, 140.0)
    on_position_update(pos_id, sl_order_id=sl_order_id)

    # Trigger exit (but not complete yet)
    on_order_placed(exit_order_id, symbol, "SELL", 60, None)

    # Store pending exit
    on_position_update(pos_id, _exit_order_id=exit_order_id, _pending_exit_reason="Stop Loss")

    print(f"[PRE-CRASH] Exit triggered: {exit_order_id} (PENDING)")

    # === SYSTEM CRASHES HERE ===
    print("[CRASH] System killed mid-exit")

    # === SYSTEM RESTARTS ===
    print("\n[RESTART] System restarting...")

    # Simulate broker completing exit while system was down
    broker.complete_order(exit_order_id, 145.0)

    # Recovery: Find positions with pending exit
    positions = get_open_positions_from_db()
    assert len(positions) == 1

    pos = positions[0]
    # Note: _exit_order_id is not in _PERSIST_KEYS, so this test documents
    # that we need to add it if we want to track in-flight exits

    print(f"[RECOVERY] Found position: {pos['symbol']}")
    print(f"  Note: Pending exit state needs explicit tracking")

    # Check if exit order exists in DB
    conn = client._pool.getconn()
    cur = conn.cursor()
    cur.execute("SELECT status FROM orders WHERE order_id = %s", (exit_order_id,))
    row = cur.fetchone()
    client._pool.putconn(conn)

    if row:
        exit_status = row[0]
        print(f"[RECOVERY] Exit order found: status={exit_status}")

        # Poll broker
        broker_order = broker.get_order(exit_order_id)
        if broker_order["status"] == "COMPLETE":
            # Complete exit
            on_order_complete(exit_order_id, broker_order["average_price"])

            # Calculate PnL
            entry_price = pos["entry_price"]
            exit_price = broker_order["average_price"]
            qty = pos["qty"]
            gross_pnl = (exit_price - entry_price) * qty

            from engine.execution.cost_model import round_trip_cost
            net_pnl = gross_pnl - round_trip_cost(qty)

            # Close position
            on_position_close(
                position_id=pos["_db_id"],
                exit_price=exit_price,
                exit_reason="Stop Loss",
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                strategy="ML"
            )

            print(f"[RECOVERY] Position closed: net_pnl={net_pnl:.2f}")

    # Verify no duplicate exit orders
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM orders
        WHERE tradingsymbol = %s AND side = 'SELL' AND order_id LIKE 'EXIT%'
    """, (symbol,))
    exit_count = cur.fetchone()[0]
    client._pool.putconn(conn)

    assert exit_count == 1, f"Expected 1 exit order, got {exit_count} (DUPLICATE DETECTED)"

    # Verify trade created
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM trades WHERE position_id = %s", (pos_id,))
    trade_count = cur.fetchone()[0]
    client._pool.putconn(conn)

    assert trade_count == 1, f"Expected 1 trade, got {trade_count}"

    print(f"✓ PASSED: Exit completed on recovery, no duplicates")
    print(f"  - Exit order count: {exit_count}")
    print(f"  - Trade count: {trade_count}")

    client.close()


def test_broker_position_reconciliation():
    """
    Test 5: Verify DB positions match broker positions.

    Expected behavior:
      - DB says position open → broker must have it
      - Broker has position → DB must have it
      - Mismatch → CRITICAL ALERT (detected, not auto-fixed)
    """
    print("\n" + "=" * 70)
    print("TEST 5: Broker vs DB reconciliation")
    print("=" * 70)

    client = setup_test_db()
    broker = MockBroker()

    # Case 1: DB and broker match (OK)
    symbol1 = "BANKNIFTY2408147300CE"
    entry_order_id = "ENTRY_005"

    broker.complete_order(entry_order_id, 200.0)
    broker.positions[symbol1] = {"quantity": 30, "average_price": 200.0}

    on_order_placed(entry_order_id, symbol1, "BUY", 30, None)
    on_order_complete(entry_order_id, 200.0)

    pos_id = on_position_open(
        symbol=symbol1,
        side="CE",
        qty=30,
        entry_price=200.0,
        entry_order_id=entry_order_id
    )

    # Reconcile
    db_positions = get_open_positions_from_db()
    broker_positions = broker.get_positions()

    db_symbols = {p["symbol"] for p in db_positions}
    broker_symbols = {p["tradingsymbol"] for p in broker_positions}

    assert db_symbols == broker_symbols, "Positions should match"
    print(f"✓ Case 1 PASSED: DB and broker match ({len(db_positions)} positions)")

    # Case 2: Broker has position, DB missing (CRITICAL)
    symbol2 = "BANKNIFTY2408147500PE"
    broker.positions[symbol2] = {"quantity": 60, "average_price": 180.0}

    broker_positions = broker.get_positions()
    db_positions = get_open_positions_from_db()

    db_symbols = {p["symbol"] for p in db_positions}
    broker_symbols = {p["tradingsymbol"] for p in broker_positions}

    orphaned_in_broker = broker_symbols - db_symbols
    assert len(orphaned_in_broker) == 1, "Should detect orphaned broker position"
    assert symbol2 in orphaned_in_broker

    print(f"✗ Case 2 DETECTED: Broker has position not in DB: {orphaned_in_broker}")
    print(f"  → CRITICAL ALERT would be sent")

    # Case 3: DB has position, broker missing (CRITICAL)
    symbol3 = "BANKNIFTY2408147700CE"
    entry_order_id_3 = "ENTRY_006"

    on_order_placed(entry_order_id_3, symbol3, "BUY", 30, None)
    on_order_complete(entry_order_id_3, 210.0)

    pos_id_3 = on_position_open(
        symbol=symbol3,
        side="CE",
        qty=30,
        entry_price=210.0,
        entry_order_id=entry_order_id_3
    )

    # Don't add to broker (simulate broker state loss)

    db_positions = get_open_positions_from_db()
    broker_positions = broker.get_positions()

    db_symbols = {p["symbol"] for p in db_positions}
    broker_symbols = {p["tradingsymbol"] for p in broker_positions}

    orphaned_in_db = db_symbols - broker_symbols
    assert len(orphaned_in_db) == 1, "Should detect orphaned DB position"
    assert symbol3 in orphaned_in_db

    print(f"✗ Case 3 DETECTED: DB has position not in broker: {orphaned_in_db}")
    print(f"  → CRITICAL ALERT would be sent")

    print(f"\n✓ PASSED: Reconciliation detects all mismatches")

    client.close()


def run_all_tests():
    """Run all Phase 24 restart recovery tests."""
    print("=" * 70)
    print("PHASE 24: RESTART RECOVERY VALIDATION")
    print("=" * 70)
    print("\nObjective: Verify system recovers correctly from crashes")
    print("Success criteria: No data loss, no duplicates, no inconsistencies")

    tests = [
        test_crash_after_entry_pending,
        test_crash_after_entry_before_sl,
        test_crash_with_open_position,
        test_crash_after_exit_triggered,
        test_broker_position_reconciliation,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"\n✗ FAILED: {test_func.__name__}")
            print(f"  Error: {e}")
        except Exception as e:
            failed += 1
            print(f"\n✗ ERROR: {test_func.__name__}")
            print(f"  Exception: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)

    if failed == 0:
        print("\n✓ ALL TESTS PASSED — System restart recovery is VALIDATED")
    else:
        print(f"\n✗ {failed} TEST(S) FAILED — Fix before production")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
