# engine/storage/postgres_client.py
#
# PostgreSQL persistence layer — system of record for all trading state.
# Replaces runtime_state.json with durable, queryable storage.
#
# FAIL-SAFE: DB write failures DO NOT block trading. Errors are logged and
# execution continues. Reconnection uses exponential backoff.

import os
import json
import logging
import time
from datetime import datetime, date
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool, sql, extras
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

logger = logging.getLogger("postgres_client")

# ── Environment-driven config ─────────────────────────────────────────
_DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
_DB_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
_DB_NAME = os.getenv("POSTGRES_DB", "trading_system")
_DB_USER = os.getenv("POSTGRES_USER", "trading_user")
_DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

# Connection pool sizing
_MIN_CONN = int(os.getenv("POSTGRES_MIN_CONN", "2"))
_MAX_CONN = int(os.getenv("POSTGRES_MAX_CONN", "10"))

# Retry policy
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class PostgresClient:
    """
    Async-safe PostgreSQL client with connection pooling and automatic retry.

    All write methods are non-blocking to trading execution: failures are
    logged but do not raise exceptions to the caller.
    """

    def __init__(self):
        self._pool: Optional[pool.ThreadedConnectionPool] = None
        self._initialized = False
        self._last_error: Optional[str] = None

    def connect(self) -> bool:
        """
        Initialize connection pool and ensure schema exists.

        Returns:
            True if connected, False otherwise.
        """
        if self._initialized:
            return True

        try:
            logger.info(f"[POSTGRES] Connecting to {_DB_USER}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}")

            # Create pool
            self._pool = pool.ThreadedConnectionPool(
                _MIN_CONN,
                _MAX_CONN,
                host=_DB_HOST,
                port=_DB_PORT,
                database=_DB_NAME,
                user=_DB_USER,
                password=_DB_PASS,
                connect_timeout=5,
            )

            # Verify connection and initialize schema
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()

            self._initialize_schema()
            self._initialized = True
            self._last_error = None
            logger.info("[POSTGRES] Connected and schema initialized")
            return True

        except Exception as e:
            self._last_error = str(e)
            logger.error(f"[POSTGRES] Connection failed: {e}")
            return False

    @contextmanager
    def _get_conn(self):
        """Context manager for connection pooling."""
        if not self._pool:
            raise RuntimeError("Connection pool not initialized")

        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _initialize_schema(self):
        """Create tables if they don't exist."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Orders table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id SERIAL PRIMARY KEY,
                        order_id VARCHAR(100) UNIQUE NOT NULL,
                        symbol VARCHAR(50) NOT NULL,
                        side VARCHAR(10) NOT NULL,
                        qty INTEGER NOT NULL,
                        price DECIMAL(10, 2),
                        status VARCHAR(20) NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # Index on order_id for fast lookups
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_orders_order_id
                    ON orders(order_id)
                """)

                # Index on status for filtering
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_orders_status
                    ON orders(status)
                """)

                # Positions table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(50) NOT NULL,
                        side VARCHAR(10) NOT NULL,
                        qty INTEGER NOT NULL,
                        entry_price DECIMAL(10, 2) NOT NULL,
                        entry_order_id VARCHAR(100),
                        sl_order_id VARCHAR(100),
                        status VARCHAR(20) NOT NULL,
                        ml_prob DECIMAL(5, 4),
                        regime VARCHAR(50),
                        max_pnl DECIMAL(10, 2) DEFAULT 0,
                        min_pnl DECIMAL(10, 2) DEFAULT 0,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW(),
                        FOREIGN KEY (entry_order_id) REFERENCES orders(order_id)
                    )
                """)

                # Index on status for active position queries
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status)
                """)

                # Trades table (closed positions)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(50) NOT NULL,
                        side VARCHAR(10) NOT NULL,
                        entry_price DECIMAL(10, 2) NOT NULL,
                        exit_price DECIMAL(10, 2) NOT NULL,
                        qty INTEGER NOT NULL,
                        gross_pnl DECIMAL(10, 2) NOT NULL,
                        net_pnl DECIMAL(10, 2) NOT NULL,
                        strategy VARCHAR(50),
                        ml_prob DECIMAL(5, 4),
                        regime VARCHAR(50),
                        exit_reason VARCHAR(100),
                        entry_time TIMESTAMP NOT NULL,
                        exit_time TIMESTAMP NOT NULL,
                        position_id INTEGER,
                        FOREIGN KEY (position_id) REFERENCES positions(id)
                    )
                """)

                # Index on entry_time for date-range queries
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_trades_entry_time
                    ON trades(entry_time)
                """)

                # System state table (replaces runtime_state.json)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS system_state (
                        id SERIAL PRIMARY KEY,
                        key VARCHAR(100) UNIQUE NOT NULL,
                        value JSONB NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # Index on key for fast lookups
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_system_state_key
                    ON system_state(key)
                """)

                logger.info("[POSTGRES] Schema initialized")

    def _retry_on_failure(self, func, *args, **kwargs):
        """
        Execute func with exponential backoff retry.

        Returns:
            Result of func, or None if all retries exhausted.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(f"[POSTGRES] Retry {attempt+1}/{_MAX_RETRIES} after {wait}s: {e}")
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(wait)
                else:
                    logger.error(f"[POSTGRES] All retries exhausted: {e}")
                    self._last_error = str(e)
                    return None

    # ── ORDERS ────────────────────────────────────────────────────────

    def insert_order(self, order_id: str, symbol: str, side: str,
                     qty: int, price: Optional[float], status: str) -> bool:
        """
        Record a new order placement.

        Non-blocking: returns False on failure but does not raise.
        """
        def _insert():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO orders (order_id, symbol, side, qty, price, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (order_id) DO NOTHING
                    """, (order_id, symbol, side, qty, price, status))
            logger.info(f"[POSTGRES] Order inserted: {order_id} {symbol} {side} {qty} @ {price}")
            return True

        try:
            return self._retry_on_failure(_insert) is not None
        except Exception as e:
            logger.error(f"[POSTGRES] insert_order failed: {e}")
            return False

    def update_order_status(self, order_id: str, status: str,
                           price: Optional[float] = None) -> bool:
        """
        Update order status (COMPLETE / CANCELLED / REJECTED).

        Non-blocking: returns False on failure but does not raise.
        """
        def _update():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    if price is not None:
                        cur.execute("""
                            UPDATE orders
                            SET status = %s, price = %s, updated_at = NOW()
                            WHERE order_id = %s
                        """, (status, price, order_id))
                    else:
                        cur.execute("""
                            UPDATE orders
                            SET status = %s, updated_at = NOW()
                            WHERE order_id = %s
                        """, (status, order_id))
            logger.info(f"[POSTGRES] Order updated: {order_id} -> {status}")
            return True

        try:
            return self._retry_on_failure(_update) is not None
        except Exception as e:
            logger.error(f"[POSTGRES] update_order_status failed: {e}")
            return False

    # ── POSITIONS ─────────────────────────────────────────────────────

    def insert_position(self, symbol: str, side: str, qty: int,
                       entry_price: float, entry_order_id: str,
                       ml_prob: Optional[float] = None,
                       regime: Optional[str] = None) -> Optional[int]:
        """
        Record a new open position.

        Returns:
            Position ID (database PK) or None on failure.
        """
        def _insert():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO positions
                        (symbol, side, qty, entry_price, entry_order_id,
                         ml_prob, regime, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN')
                        RETURNING id
                    """, (symbol, side, qty, entry_price, entry_order_id,
                          ml_prob, regime))
                    pos_id = cur.fetchone()[0]
            logger.info(f"[POSTGRES] Position opened: id={pos_id} {symbol} {side} {qty}")
            return pos_id

        try:
            return self._retry_on_failure(_insert)
        except Exception as e:
            logger.error(f"[POSTGRES] insert_position failed: {e}")
            return None

    def update_position(self, position_id: int, **kwargs) -> bool:
        """
        Update position fields (sl_order_id, max_pnl, min_pnl, etc).

        kwargs: Any position field to update.
        """
        if not kwargs:
            return True

        def _update():
            fields = ", ".join([f"{k} = %s" for k in kwargs.keys()])
            values = list(kwargs.values()) + [position_id]

            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE positions
                        SET {fields}, updated_at = NOW()
                        WHERE id = %s
                    """, values)
            logger.debug(f"[POSTGRES] Position {position_id} updated: {kwargs}")
            return True

        try:
            return self._retry_on_failure(_update) is not None
        except Exception as e:
            logger.error(f"[POSTGRES] update_position failed: {e}")
            return False

    def close_position(self, position_id: int, exit_price: float,
                      exit_reason: str, gross_pnl: float,
                      net_pnl: float, strategy: str,
                      exit_time: Optional[datetime] = None) -> bool:
        """
        Close a position and create corresponding trade record.

        Atomic: both position update and trade insert happen in same transaction.
        """
        if exit_time is None:
            exit_time = datetime.now()

        def _close():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # Update position status
                    cur.execute("""
                        UPDATE positions
                        SET status = 'CLOSED', updated_at = NOW()
                        WHERE id = %s
                        RETURNING symbol, side, qty, entry_price,
                                  ml_prob, regime, created_at
                    """, (position_id,))

                    row = cur.fetchone()
                    if not row:
                        logger.warning(f"[POSTGRES] Position {position_id} not found")
                        return False

                    symbol, side, qty, entry_price, ml_prob, regime, entry_time = row

                    # Insert trade record
                    cur.execute("""
                        INSERT INTO trades
                        (symbol, side, entry_price, exit_price, qty,
                         gross_pnl, net_pnl, strategy, ml_prob, regime,
                         exit_reason, entry_time, exit_time, position_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (symbol, side, entry_price, exit_price, qty,
                          gross_pnl, net_pnl, strategy, ml_prob, regime,
                          exit_reason, entry_time, exit_time, position_id))

            logger.info(f"[POSTGRES] Position closed: id={position_id} net_pnl={net_pnl:.2f}")
            return True

        try:
            return self._retry_on_failure(_close) is not None
        except Exception as e:
            logger.error(f"[POSTGRES] close_position failed: {e}")
            return False

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieve all open positions.

        Returns:
            List of position dicts.
        """
        def _fetch():
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM positions
                        WHERE status = 'OPEN'
                        ORDER BY created_at DESC
                    """)
                    return [dict(row) for row in cur.fetchall()]

        try:
            result = self._retry_on_failure(_fetch)
            return result if result is not None else []
        except Exception as e:
            logger.error(f"[POSTGRES] get_open_positions failed: {e}")
            return []

    # ── SYSTEM STATE ──────────────────────────────────────────────────

    def set_state(self, key: str, value: Any) -> bool:
        """
        Persist a system state key-value pair (replaces runtime_state.json).

        Value is stored as JSONB.
        """
        def _set():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO system_state (key, value, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key)
                        DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """, (key, json.dumps(value)))
            return True

        try:
            return self._retry_on_failure(_set) is not None
        except Exception as e:
            logger.error(f"[POSTGRES] set_state failed: {e}")
            return False

    def get_state(self, key: str, default=None) -> Any:
        """
        Retrieve a system state value by key.

        Returns:
            Parsed value or default if not found.
        """
        def _get():
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT value FROM system_state
                        WHERE key = %s
                    """, (key,))
                    row = cur.fetchone()
                    if row:
                        return json.loads(row[0])
                    return default

        try:
            result = self._retry_on_failure(_get)
            return result if result is not None else default
        except Exception as e:
            logger.error(f"[POSTGRES] get_state failed: {e}")
            return default

    def get_today_trades(self) -> List[Dict[str, Any]]:
        """
        Retrieve all trades from today.

        Returns:
            List of trade dicts.
        """
        def _fetch():
            today = date.today()
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM trades
                        WHERE DATE(entry_time) = %s
                        ORDER BY entry_time ASC
                    """, (today,))
                    return [dict(row) for row in cur.fetchall()]

        try:
            result = self._retry_on_failure(_fetch)
            return result if result is not None else []
        except Exception as e:
            logger.error(f"[POSTGRES] get_today_trades failed: {e}")
            return []

    def health_check(self) -> Dict[str, Any]:
        """
        Return connection health status.

        Returns:
            Dict with status, error, and basic stats.
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM orders")
                    order_count = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
                    open_positions = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM trades WHERE DATE(entry_time) = %s",
                               (date.today(),))
                    today_trades = cur.fetchone()[0]

            return {
                "status": "healthy",
                "connected": True,
                "error": None,
                "stats": {
                    "total_orders": order_count,
                    "open_positions": open_positions,
                    "today_trades": today_trades,
                }
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "connected": False,
                "error": str(e),
                "last_error": self._last_error,
            }

    def close(self):
        """Close connection pool."""
        if self._pool:
            self._pool.closeall()
            self._initialized = False
            logger.info("[POSTGRES] Connection pool closed")


# ── Singleton instance ────────────────────────────────────────────────
_client: Optional[PostgresClient] = None


def get_client() -> PostgresClient:
    """Get or create the singleton PostgresClient instance."""
    global _client
    if _client is None:
        _client = PostgresClient()
    return _client


def initialize() -> bool:
    """
    Initialize the PostgreSQL client (call once at startup).

    Returns:
        True if successful, False otherwise.
    """
    client = get_client()
    return client.connect()
