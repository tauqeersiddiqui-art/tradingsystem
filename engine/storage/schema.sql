-- engine/storage/schema.sql
--
-- PostgreSQL schema for trading system persistence layer.
-- System of record for all orders, positions, trades, and runtime state.
--
-- Setup instructions:
-- 1. Create database: CREATE DATABASE trading_system;
-- 2. Create user: CREATE USER trading_user WITH PASSWORD 'your_password';
-- 3. Grant privileges: GRANT ALL PRIVILEGES ON DATABASE trading_system TO trading_user;
-- 4. Run this file: psql -U trading_user -d trading_system -f schema.sql

-- ── ORDERS TABLE ──────────────────────────────────────────────────────
-- Records every order placed with the broker.
-- Immutable history — no deletes, only status updates.

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) UNIQUE NOT NULL,  -- Broker order ID
    symbol VARCHAR(50) NOT NULL,             -- e.g., BANKNIFTY2410547300CE
    side VARCHAR(10) NOT NULL,               -- BUY / SELL
    qty INTEGER NOT NULL,                    -- Position size (e.g., 30, 60)
    price DECIMAL(10, 2),                    -- Fill price (NULL if not filled)
    status VARCHAR(20) NOT NULL,             -- PLACED / COMPLETE / CANCELLED / REJECTED
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Index for fast order lookups by broker ID
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);

-- Index for filtering by status
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

COMMENT ON TABLE orders IS 'Immutable order history — all broker interactions';
COMMENT ON COLUMN orders.order_id IS 'Broker-assigned order ID (unique)';
COMMENT ON COLUMN orders.status IS 'PLACED: submitted | COMPLETE: filled | CANCELLED: user/broker cancel | REJECTED: broker reject';

-- ── POSITIONS TABLE ───────────────────────────────────────────────────
-- Active and closed positions.
-- A position is created on entry order COMPLETE and closed on exit order COMPLETE.

CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,               -- CE / PE
    qty INTEGER NOT NULL,
    entry_price DECIMAL(10, 2) NOT NULL,     -- Actual fill price
    entry_order_id VARCHAR(100),             -- FK to orders.order_id
    sl_order_id VARCHAR(100),                -- SL order ID (if placed)
    status VARCHAR(20) NOT NULL,             -- OPEN / CLOSED
    ml_prob DECIMAL(5, 4),                   -- ML probability at entry (0.0–1.0)
    regime VARCHAR(50),                      -- Market regime at entry
    max_pnl DECIMAL(10, 2) DEFAULT 0,        -- Peak profit seen (Rs, gross)
    min_pnl DECIMAL(10, 2) DEFAULT 0,        -- Worst drawdown seen (Rs, gross)
    created_at TIMESTAMP DEFAULT NOW(),      -- Position open time
    updated_at TIMESTAMP DEFAULT NOW(),      -- Last update time
    FOREIGN KEY (entry_order_id) REFERENCES orders(order_id)
);

-- Index for active position queries (hot path)
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- Index for symbol lookups
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_positions_created_at ON positions(created_at);

COMMENT ON TABLE positions IS 'Position lifecycle — OPEN when live, CLOSED on exit';
COMMENT ON COLUMN positions.max_pnl IS 'Peak gross PnL in Rs (used for MFE analytics)';
COMMENT ON COLUMN positions.min_pnl IS 'Worst gross PnL in Rs (used for MAE analytics)';

-- ── TRADES TABLE ──────────────────────────────────────────────────────
-- Completed trades (closed positions).
-- Created atomically when a position is closed.
-- This is the authoritative record for realized PnL.

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,
    entry_price DECIMAL(10, 2) NOT NULL,
    exit_price DECIMAL(10, 2) NOT NULL,
    qty INTEGER NOT NULL,
    gross_pnl DECIMAL(10, 2) NOT NULL,       -- PnL before costs
    net_pnl DECIMAL(10, 2) NOT NULL,         -- PnL after costs (authoritative)
    strategy VARCHAR(50),                     -- ORB / ML / HYBRID / SCALP
    ml_prob DECIMAL(5, 4),
    regime VARCHAR(50),
    exit_reason VARCHAR(100),                 -- Stop Loss / TARGET_HIT / MANUAL / etc
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP NOT NULL,
    position_id INTEGER,                      -- FK to positions.id
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

-- Index for date-range queries (daily reports, backtests)
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);

-- Index for PnL filtering (winners/losers)
CREATE INDEX IF NOT EXISTS idx_trades_net_pnl ON trades(net_pnl);

-- Index for strategy breakdown
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

COMMENT ON TABLE trades IS 'Closed position records — realized PnL truth';
COMMENT ON COLUMN trades.gross_pnl IS 'PnL before costs (for MFE comparison)';
COMMENT ON COLUMN trades.net_pnl IS 'PnL after costs — authoritative realized profit';

-- ── SYSTEM STATE TABLE ────────────────────────────────────────────────
-- Replaces runtime_state.json with queryable JSONB storage.
-- Stores:
--   - session_date: current trading day (prevents stale state reuse)
--   - pnl: cumulative net realized PnL for the day
--   - gross_pnl: cumulative gross realized PnL
--   - trades_today: count of completed trades
--   - daily_profit_locked: risk gate (stops trading when true)
--   - pending_entry: in-flight entry order details
--   - etc.

CREATE TABLE IF NOT EXISTS system_state (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,        -- e.g., "pnl", "session_date"
    value JSONB NOT NULL,                    -- JSON value (any type)
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Index for fast key lookups (hot path)
CREATE INDEX IF NOT EXISTS idx_system_state_key ON system_state(key);

COMMENT ON TABLE system_state IS 'Runtime state persistence — replaces runtime_state.json';
COMMENT ON COLUMN system_state.value IS 'JSONB — supports any JSON type (number, string, object, array)';

-- ── HELPER VIEWS ──────────────────────────────────────────────────────

-- Today's trades (hot query for dashboard)
CREATE OR REPLACE VIEW today_trades AS
SELECT * FROM trades
WHERE DATE(entry_time) = CURRENT_DATE
ORDER BY entry_time ASC;

-- Open positions (hot query for position management)
CREATE OR REPLACE VIEW open_positions AS
SELECT * FROM positions
WHERE status = 'OPEN'
ORDER BY created_at DESC;

-- Daily PnL summary
CREATE OR REPLACE VIEW daily_pnl AS
SELECT
    DATE(entry_time) AS trade_date,
    COUNT(*) AS trade_count,
    SUM(gross_pnl) AS total_gross_pnl,
    SUM(net_pnl) AS total_net_pnl,
    AVG(net_pnl) AS avg_net_pnl,
    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS winners,
    SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) AS losers
FROM trades
GROUP BY DATE(entry_time)
ORDER BY trade_date DESC;

COMMENT ON VIEW daily_pnl IS 'Daily PnL summary — win rate, avg profit, total net';

-- ── PERFORMANCE NOTES ─────────────────────────────────────────────────
-- 1. All hot-path queries (open_positions, today_trades) are indexed.
-- 2. Batch inserts should use executemany() for multiple orders.
-- 3. Connection pooling prevents bottlenecks (2–10 conns default).
-- 4. No triggers or cascades — explicit control for fail-safe behavior.
