# PostgreSQL Migration Guide

Complete migration from `runtime_state.json` to PostgreSQL persistence.

---

## Prerequisites

1. **PostgreSQL 12+** installed and running
2. **psycopg2** Python package

```bash
pip install psycopg2-binary
```

---

## Step 1: Database Setup

### 1.1 Create Database

```sql
CREATE DATABASE trading_system;
```

### 1.2 Create User

```sql
CREATE USER trading_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE trading_system TO trading_user;
```

### 1.3 Initialize Schema

```bash
psql -U trading_user -d trading_system -f engine/storage/schema.sql
```

Verify tables were created:

```sql
\dt  -- List tables
```

Expected output:
- `orders`
- `positions`
- `trades`
- `system_state`

---

## Step 2: Environment Configuration

Create or update `.env`:

```bash
# PostgreSQL Configuration
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=trading_system
POSTGRES_USER=trading_user
POSTGRES_PASSWORD=your_secure_password

# Connection Pool (optional, defaults shown)
POSTGRES_MIN_CONN=2
POSTGRES_MAX_CONN=10
```

**Security note:** Never commit `.env` to git. Use `.env.example` for templates.

---

## Step 3: Code Integration

### 3.1 Initialize at Startup

In `master_runner.py`, **after** importing modules, add:

```python
# Add after existing imports
from engine.storage import postgres_client
from engine.storage.integration import (
    load_session_state,
    sync_session_state,
    get_open_positions_from_db,
    on_order_placed,
    on_order_complete,
    on_position_open,
    on_position_update,
    on_position_close,
)

# Initialize PostgreSQL (before market opens)
if not postgres_client.initialize():
    logger.error("[CRITICAL] PostgreSQL connection failed — exiting")
    sys.exit(1)

logger.info("[POSTGRES] Connected and ready")
```

### 3.2 Replace `load_state()` Call

**OLD:**
```python
snap = load_state()  # state_store.load_state()
if snap:
    ctx.pnl = snap.get("pnl", 0.0)
    ctx.trades_today = snap.get("trades_today", 0)
    # ...
```

**NEW:**
```python
# Restore session state from DB
load_session_state(ctx)

# Recover open positions
db_positions = get_open_positions_from_db()
if db_positions:
    logger.info(f"[RECOVERY] Found {len(db_positions)} open position(s)")
    position = db_positions[0]  # System only holds 1 position at a time
    # ... rest of recovery logic
```

### 3.3 Hook Order Placement

In `execution_engine.py`, inside `execute_entry()` and `execute_exit()`:

**After `broker.place_order()` returns:**

```python
order_id = broker.place_order(
    tradingsymbol=symbol,
    transaction_type=transaction_type,
    quantity=qty,
    order_type="MARKET",
    product="MIS",
)

# NEW: Persist order
from engine.storage.integration import on_order_placed
on_order_placed(
    order_id=order_id,
    symbol=symbol,
    side=transaction_type,
    qty=qty,
    price=None  # MARKET order, price unknown at placement
)
```

### 3.4 Hook Order Completion

In `execution_engine.py`, inside `_poll_order()`:

**When order status becomes COMPLETE:**

```python
if status == ST_COMPLETE:
    fill_price = state.get("average_price", 0.0)
    
    # NEW: Update order in DB
    from engine.storage.integration import on_order_complete
    on_order_complete(order_id, fill_price)
    
    # ... rest of existing logic
```

**When order is cancelled or rejected:**

```python
if status == ST_CANCELLED:
    from engine.storage.integration import on_order_cancelled
    on_order_cancelled(order_id)

if status == ST_REJECTED:
    from engine.storage.integration import on_order_rejected
    on_order_rejected(order_id)
```

### 3.5 Hook Position Open

In `master_runner.py`, **after `finalize_entry()` confirms entry COMPLETE:**

```python
# After entry order confirmed
if entry_confirmed:
    position = {
        "symbol": option_symbol,
        "side": side,
        "qty": qty,
        "entry": fill_price,
        "entry_order_id": entry_order_id,
        "ml_prob": ml_prob,
        "regime": regime,
        # ...
    }
    
    # NEW: Persist position to DB
    pos_id = on_position_open(
        symbol=option_symbol,
        side=side,
        qty=qty,
        entry_price=fill_price,
        entry_order_id=entry_order_id,
        ml_prob=ml_prob,
        regime=regime
    )
    
    if pos_id:
        position["_db_id"] = pos_id  # Store for future updates
    
    ctx.positions.append(position)
```

### 3.6 Hook Position Updates

**After SL order is placed:**

```python
sl_order_id = executor.place_sl_order(...)

# NEW: Update position with SL order ID
if pos_id := position.get("_db_id"):
    on_position_update(pos_id, sl_order_id=sl_order_id)
```

**Every cycle when max_pnl updates:**

```python
pnl = (ltp - entry_price) * qty
position["max_pnl"] = max(position.get("max_pnl", 0), pnl)

# NEW: Persist max_pnl
if pos_id := position.get("_db_id"):
    on_position_update(pos_id, max_pnl=position["max_pnl"])
```

### 3.7 Hook Position Close

In `master_runner.py`, **after `finalize_exit()` confirms exit COMPLETE:**

```python
# After exit confirmed
exit_price = exit_order_state["average_price"]
gross_pnl = (exit_price - entry_price) * qty
net_pnl = gross_pnl - cost_model.round_trip_cost(qty)

# Update context
ctx.pnl += net_pnl
ctx.gross_pnl += gross_pnl
ctx.trades_today += 1

# NEW: Close position in DB (creates trade record)
if pos_id := position.get("_db_id"):
    on_position_close(
        position_id=pos_id,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        strategy=strategy,
        exit_time=datetime.now()
    )
```

### 3.8 Replace `save_state()` Call

**At end of each cycle:**

**OLD:**
```python
save_state(
    ctx,
    position=position,
    scalp_position=scalp_position,
    # ...
)
```

**NEW:**
```python
# Sync session state to DB
sync_session_state(ctx)
```

---

## Step 4: Verification

### 4.1 Run Test Suite

```bash
python data/_phase23_verify.py
```

Expected output:
```
===== PHASE 23 VERIFICATION: PostgreSQL Persistence Layer =====
✓ PASSED: Order recorded exactly once
✓ PASSED: Position persists across restart
✓ PASSED: Trade PnL stored correctly
✓ PASSED: No duplicate trades
✓ PASSED: System state recovery
✓ PASSED: Analytics queries

RESULTS: 6 passed, 0 failed
```

### 4.2 Manual Verification

**Check database contents:**

```sql
-- View today's trades
SELECT * FROM trades WHERE DATE(entry_time) = CURRENT_DATE;

-- View open positions
SELECT * FROM positions WHERE status = 'OPEN';

-- View session state
SELECT * FROM system_state;
```

### 4.3 Restart Test

1. Start system with live data (paper mode recommended)
2. Place 1 trade
3. **Stop system while position is OPEN**
4. Restart system
5. Verify position is recovered and continues management

Check logs for:
```
[POSTGRES] Connected and ready
[STORAGE] Recovered 1 open position(s) from DB
[RECOVERY] Found 1 open position(s)
```

---

## Step 5: Rollback Plan

If issues arise, revert to `runtime_state.json`:

1. **Disable PostgreSQL initialization** (comment out in `master_runner.py`)
2. **Remove integration hooks** (order/position/state calls)
3. **Restore old `save_state()` / `load_state()` calls**
4. **Restart system**

The JSON file will continue working as before. No data loss.

---

## Step 6: Monitoring

### Health Check

Add to dashboard:

```python
from engine.storage.postgres_client import get_client

health = get_client().health_check()
print(f"DB Status: {health['status']}")
print(f"Open Positions: {health['stats']['open_positions']}")
print(f"Today's Trades: {health['stats']['today_trades']}")
```

### Logs to Watch

```
[POSTGRES] Connected and ready           ← Startup success
[STORAGE] Order inserted: ORDER_123      ← Order persisted
[STORAGE] Position opened: id=42         ← Position created
[STORAGE] Position closed: id=42         ← Trade recorded
[STORAGE] Failed to persist order        ← DB write failure (non-blocking)
```

---

## Performance Notes

1. **Connection pool** (2–10 connections) handles concurrent writes
2. **All writes are async** — trading never blocks on DB
3. **Indexes on hot paths** (open_positions, today_trades) keep queries fast
4. **JSONB for system_state** allows flexible schema evolution

---

## Migration Checklist

- [ ] PostgreSQL installed and running
- [ ] Database and user created
- [ ] Schema initialized (`schema.sql` loaded)
- [ ] `.env` configured with DB credentials
- [ ] `postgres_client.initialize()` added at startup
- [ ] `load_session_state()` replaces `load_state()`
- [ ] `sync_session_state()` replaces `save_state()`
- [ ] Order hooks added (`on_order_placed`, `on_order_complete`)
- [ ] Position hooks added (`on_position_open`, `on_position_close`)
- [ ] Test suite passes (`_phase23_verify.py`)
- [ ] Manual restart test confirms recovery
- [ ] Health check added to dashboard
- [ ] Old `runtime_state.json` backed up

---

## Troubleshooting

### Connection Failed

```
[POSTGRES] Connection failed: FATAL: password authentication failed
```

**Fix:** Verify credentials in `.env` match PostgreSQL user/password.

### Schema Not Found

```
[POSTGRES] relation "orders" does not exist
```

**Fix:** Run `schema.sql` to create tables.

### Stale State Restored

```
[STORAGE] No valid session state from today
```

**Fix:** This is expected behavior. System starts fresh if previous session was from a different day.

### Duplicate Trades

```
⚠ WARNING: Duplicate trades detected
```

**Fix:** Add unique constraint to prevent duplicates:

```sql
ALTER TABLE trades ADD CONSTRAINT unique_position_trade
UNIQUE (position_id);
```

---

## Next Steps

1. **Monitor for 1 week** — ensure no data loss or corruption
2. **Deprecate `runtime_state.json`** — remove old code after DB proven stable
3. **Add analytics queries** — leverage SQL for backtests, reports
4. **Consider async I/O** — migrate from `psycopg2` to `asyncpg` if bottlenecks appear

---

**Migration complete. PostgreSQL is now the system of record.**
