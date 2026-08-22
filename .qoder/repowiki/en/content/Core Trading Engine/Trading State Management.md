# Trading State Management

<cite>
**Referenced Files in This Document**
- [context.py](file://engine/core/context.py)
- [state_store.py](file://engine/core/state_store.py)
- [live_engine.py](file://engine/live_engine.py)
- [execution_engine.py](file://engine/execution/execution_engine.py)
- [profit_manager.py](file://engine/execution/profit_manager.py)
- [risk_manager.py](file://engine/risk/risk_manager.py)
- [master_runner.py](file://master_runner.py)
- [slippage.py](file://engine/analytics/slippage.py)
- [runtime_state.json](file://data/runtime_state.json)
</cite>

## Table of Contents
1. Introduction
2. Project Structure
3. Core Components
4. Architecture Overview
5. Detailed Component Analysis
6. Dependency Analysis
7. Performance Considerations
8. Troubleshooting Guide
9. Conclusion

## Introduction
This document explains the trading state management system that keeps consistent state across the trading engine components. It focuses on:
- The shared runtime Context object for configuration, market data, and component references
- Persistent state storage for positions, session counters, and recovery after failures or restarts
- Synchronization patterns between live engine, execution layer, and risk systems
- Data structures for positions, order status, and performance metrics
- Thread safety and concurrent access considerations
- Examples of state transitions during the trade lifecycle and error recovery strategies

## Project Structure
The state management system spans core runtime context, persistence, decision engine, execution, risk, and orchestration:
- Context provides a single place to access broker, executor, risk, ML learner, and live engine
- State store persists open positions, daily PnL, and trade counts with atomic writes
- Live engine drives signals, features, and exit decisions while updating position state
- Execution engine places orders, validates fills, and manages protective stops
- Risk manager computes entry stops and targets; profit manager handles trailing and exits
- Master runner orchestrates startup, recovery, reconciliation, and threading

```mermaid
graph TB
subgraph "Core"
Ctx["TradingContext"]
Store["StateStore"]
end
subgraph "Decision"
LE["LiveEngine"]
end
subgraph "Execution"
EE["ExecutionEngine"]
PM["ProfitManager"]
RM["RiskManager"]
end
subgraph "Orchestration"
MR["MasterRunner"]
end
subgraph "Persistence"
RS["runtime_state.json"]
end
MR --> Ctx
MR --> LE
MR --> EE
MR --> Store
LE --> Ctx
LE --> EE
LE --> PM
LE --> RM
EE --> Ctx
Store --> RS
```

**Diagram sources**
- [context.py:10-42](file://engine/core/context.py#L10-L42)
- [state_store.py:18-79](file://engine/core/state_store.py#L18-L79)
- [live_engine.py:71-184](file://engine/live_engine.py#L71-L184)
- [execution_engine.py:21-371](file://engine/execution/execution_engine.py#L21-L371)
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:15-60](file://engine/risk/risk_manager.py#L15-L60)
- [master_runner.py:725-747](file://master_runner.py#L725-L747)

**Section sources**
- [context.py:10-42](file://engine/core/context.py#L10-L42)
- [state_store.py:18-79](file://engine/core/state_store.py#L18-L79)
- [live_engine.py:71-184](file://engine/live_engine.py#L71-L184)
- [execution_engine.py:21-371](file://engine/execution/execution_engine.py#L21-L371)
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:15-60](file://engine/risk/risk_manager.py#L15-L60)
- [master_runner.py:725-747](file://master_runner.py#L725-L747)

## Core Components
- TradingContext: Central runtime container holding references to market data, ML learner, strategies, broker, executor, risk, options, config, and runtime counters (cycle_count, pnl, positions, trades_today). Provides readiness checks and heartbeat diagnostics.
- StateStore: Persists daily session state (PnL, trades_today, closed positions list) and open positions (including scalp positions) to a JSON file using atomic write pattern. Loads only same-day snapshots to avoid cross-session leakage.
- LiveEngine: Decision engine that builds features, tracks ORB, day classification, and coordinates entry/exit logic. Updates and consumes state via Context and calls into ProfitManager and RiskManager.
- ExecutionEngine: Places entries/exits, validates fills by polling broker order book, manages broker-side protective stop orders, and verifies flat positions post-exit.
- ProfitManager: Centralized trailing stop ladder and exit logic based on rupee-based MFE thresholds; returns updated stop levels and scale-out info.
- RiskManager: Computes tight entry stops and targets for long options (both CE and PE), capping worst-case loss per trade.
- MasterRunner: Orchestrates process startup, thread creation, context building, state restoration, broker reconciliation, and ongoing loop control.

**Section sources**
- [context.py:10-78](file://engine/core/context.py#L10-L78)
- [state_store.py:28-94](file://engine/core/state_store.py#L28-L94)
- [live_engine.py:71-184](file://engine/live_engine.py#L71-L184)
- [execution_engine.py:21-371](file://engine/execution/execution_engine.py#L21-L371)
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:15-60](file://engine/risk/risk_manager.py#L15-L60)
- [master_runner.py:725-747](file://master_runner.py#L725-L747)

## Architecture Overview
The system uses a central context to decouple modules and a persistent state store to survive restarts. The master runner initializes the context, restores same-day state, reconciles with the broker, and runs the live engine loop. The live engine reads/writes Context fields and delegates execution and risk to dedicated components.

```mermaid
sequenceDiagram
participant MR as "MasterRunner"
participant Ctx as "TradingContext"
participant Store as "StateStore"
participant LE as "LiveEngine"
participant EE as "ExecutionEngine"
participant PM as "ProfitManager"
participant RM as "RiskManager"
participant RS as "runtime_state.json"
MR->>Ctx : build_context()
MR->>Store : load_state()
Store-->>MR : snapshot (same-day only)
MR->>LE : start engine_loop(ctx)
loop every cycle
LE->>PM : manage_position(entry,ltp,stop,max_pnl,...)
PM-->>LE : new_stop, max_pnl, reason?, scale_out?
alt entry signal
LE->>EE : execute_entry(symbol, side, qty)
EE-->>LE : {order_id, price, qty}
LE->>RM : compute_entry_stops(entry, atr, regime)
RM-->>LE : stop_loss, target
LE->>Store : save_state(ctx, position)
else exit signal
LE->>EE : execute_exit(symbol, qty, side)
EE-->>LE : {order_id, price, qty}
LE->>Store : save_state(ctx, position=None)
end
end
```

**Diagram sources**
- [master_runner.py:725-747](file://master_runner.py#L725-L747)
- [state_store.py:40-79](file://engine/core/state_store.py#L40-L79)
- [live_engine.py:798-800](file://engine/live_engine.py#L798-L800)
- [execution_engine.py:94-154](file://engine/execution/execution_engine.py#L94-L154)
- [execution_engine.py:160-215](file://engine/execution/execution_engine.py#L160-L215)
- [profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

## Detailed Component Analysis

### TradingContext
- Purpose: Single source of truth for runtime references and counters used by all modules without direct cross-imports.
- Key fields: market, features, regime, sentiment, strategies, meta_ai, broker, executor, risk, state, options, scalp_engine, config, last_trade, cycle_count, pnl, positions, trades_today, exit analytics.
- Utilities: ready() validates critical dependencies; heartbeat() exposes diagnostic metrics.

```mermaid
classDiagram
class TradingContext {
+market
+features
+regime
+sentiment
+strategies
+meta_ai
+broker
+executor
+risk
+state
+options
+scalp_engine
+config
+last_trade
+cycle_count
+pnl
+positions
+trades_today
+exit_analytics
+exit_type_counts
+ready() bool
+heartbeat() dict
}
```

**Diagram sources**
- [context.py:10-78](file://engine/core/context.py#L10-L78)

**Section sources**
- [context.py:10-78](file://engine/core/context.py#L10-L78)

### StateStore
- Persistence strategy: Writes a JSON snapshot containing session_date, saved_at, pnl, trades_today, positions, open_position, and scalp_position. Uses atomic tmp file plus os.replace to prevent partial reads. Only loads snapshots from the current trading day to avoid cross-session leakage.
- Position serialization: Keeps only essential keys for open positions and safely serializes timestamps.
- Recovery: deserialize_position reconstructs datetime fields when present.

```mermaid
flowchart TD
Start([Save/Load]) --> Save{"save_state called?"}
Save --> |Yes| BuildSnap["Build snapshot<br/>session_date, pnl, trades_today,<br/>positions, open/scalp positions"]
BuildSnap --> WriteTmp["Write to temp file<br/>fsync before rename"]
WriteTmp --> AtomicSwap["os.replace(tmp, runtime_state.json)"]
AtomicSwap --> End([Done])
Save --> |No| Load{"load_state called?"}
Load --> |Yes| ReadFile["Read runtime_state.json"]
ReadFile --> CheckDay{"session_date == today?"}
CheckDay --> |Yes| ReturnSnap["Return snapshot"]
CheckDay --> |No| Ignore["Ignore stale snapshot"]
Ignore --> End
Load --> |No| End
```

**Diagram sources**
- [state_store.py:28-94](file://engine/core/state_store.py#L28-L94)
- [runtime_state.json:1-9](file://data/runtime_state.json#L1-L9)

**Section sources**
- [state_store.py:28-94](file://engine/core/state_store.py#L28-L94)
- [runtime_state.json:1-9](file://data/runtime_state.json#L1-L9)

### LiveEngine and State Synchronization
- Feature building and signals: Builds features from rolling candle windows and integrates day classification and ORB tracking.
- Exit coordination: Delegates to ProfitManager to compute trailing stops and determine exit reasons; updates Context fields (e.g., pnl, positions, trades_today) and triggers persistence.
- Entry coordination: On successful entry, sets up risk parameters via RiskManager and persists the new position.

```mermaid
sequenceDiagram
participant LE as "LiveEngine"
participant PM as "ProfitManager"
participant RM as "RiskManager"
participant EE as "ExecutionEngine"
participant Store as "StateStore"
participant Ctx as "TradingContext"
LE->>PM : manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob, target, config, side)
PM-->>LE : (new_stop, max_pnl, exit_reason, scale_out_info)
alt exit_reason is set
LE->>EE : execute_exit(symbol, qty, side)
EE-->>LE : {order_id, price, qty}
LE->>Store : save_state(ctx, position=None)
else no exit
LE->>RM : compute_entry_stops(entry_premium, atr, regime, delta, side)
RM-->>LE : (stop_loss, target, stop_pct)
opt entry signal
LE->>EE : execute_entry(symbol, side, qty)
EE-->>LE : {order_id, price, qty}
LE->>Store : save_state(ctx, position=new_position)
end
end
```

**Diagram sources**
- [live_engine.py:798-800](file://engine/live_engine.py#L798-L800)
- [profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)
- [execution_engine.py:94-154](file://engine/execution/execution_engine.py#L94-L154)
- [execution_engine.py:160-215](file://engine/execution/execution_engine.py#L160-L215)
- [state_store.py:40-79](file://engine/core/state_store.py#L40-L79)

**Section sources**
- [live_engine.py:798-800](file://engine/live_engine.py#L798-L800)
- [profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)
- [execution_engine.py:94-154](file://engine/execution/execution_engine.py#L94-L154)
- [execution_engine.py:160-215](file://engine/execution/execution_engine.py#L160-L215)
- [state_store.py:40-79](file://engine/core/state_store.py#L40-L79)

### ExecutionEngine and Order Status
- Entry/Exit placement: Places market orders for both CE and PE (buy to open, sell to close). Validates fill prices by polling broker order book until complete or fallback.
- Duplicate guard: Prevents overlapping active orders within a cycle.
- Protective stop: Places/modifies/cancels broker-side SL-M orders; supports recovery by finding open stop orders at startup.
- Verification: Confirms flat positions post-exit to avoid double exits.

```mermaid
flowchart TD
A["execute_entry/exit"] --> B{"Paper/Dry run?"}
B --> |Yes| D["Simulate order id and price"]
B --> |No| C["Place order via broker"]
C --> E["Poll order book for fill"]
E --> F{"Fill confirmed?"}
F --> |Yes| G["Record order_id, price, qty"]
F --> |No| H["Use fallback price or abort"]
D --> I["Return simulated result"]
G --> J["Update active order guard"]
H --> K["Log warning and return None"]
```

**Diagram sources**
- [execution_engine.py:94-154](file://engine/execution/execution_engine.py#L94-L154)
- [execution_engine.py:160-215](file://engine/execution/execution_engine.py#L160-L215)
- [execution_engine.py:235-328](file://engine/execution/execution_engine.py#L235-L328)
- [execution_engine.py:349-371](file://engine/execution/execution_engine.py#L349-L371)

**Section sources**
- [execution_engine.py:94-154](file://engine/execution/execution_engine.py#L94-L154)
- [execution_engine.py:160-215](file://engine/execution/execution_engine.py#L160-L215)
- [execution_engine.py:235-328](file://engine/execution/execution_engine.py#L235-L328)
- [execution_engine.py:349-371](file://engine/execution/execution_engine.py#L349-L371)

### RiskManager and ProfitManager
- RiskManager: Computes tight entry stops capped to limit worst-case loss per trade; derives target as multiple of stop distance. Designed for long options where premium rise profits both CE and PE.
- ProfitManager: Implements a cost-aware trailing ladder based on rupee MFE thresholds; returns updated stop level, stage label, locked rupees, and optional scale-out info. Enforces ratchet behavior (never loosens stop).

```mermaid
flowchart TD
Start(["manage_position"]) --> CalcPnL["Compute pnl = (ltp - entry) * qty"]
CalcPnL --> UpdateMax["max_pnl = max(max_pnl, pnl)"]
UpdateMax --> TargetCheck{"target hit?"}
TargetCheck --> |Yes| ExitTarget["reason = TARGET_HIT"]
TargetCheck --> |No| Ladder["ladder_stop(entry, qty, max_pnl, stop_loss, config, side)"]
Ladder --> Tighten{"new_stop > stop_loss?"}
Tighten --> |Yes| UpdateStop["stop_loss = new_stop"]
Tighten --> |No| KeepStop["keep stop_loss"]
UpdateStop --> Drawdown{"MFE >= threshold and drawdown?"}
KeepStop --> Drawdown
Drawdown --> |Yes| ExitDD["reason = Drawdown"]
Drawdown --> |No| HardStop{"ltp <= stop_loss?"}
ExitDD --> End(["return stop_loss, max_pnl, reason, scale_out"])
HardStop --> |Yes| ExitSL["reason = Stop Loss"]
HardStop --> |No| End
```

**Diagram sources**
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

**Section sources**
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

### MasterRunner Orchestration and Recovery
- Context building: Initializes broker, config, executor, ML learner, live engine, allocator, and restores same-day runtime state into Context fields.
- Restart recovery: Reloads runtime_state.json and reconciles against broker positions to ensure no orphaned positions; repairs protective stops if needed.
- Threading: Starts engine_loop in a daemon thread for continuous operation.

```mermaid
sequenceDiagram
participant MR as "MasterRunner"
participant Ctx as "TradingContext"
participant Store as "StateStore"
participant EE as "ExecutionEngine"
MR->>Ctx : build_context()
MR->>Store : load_state()
Store-->>MR : snapshot
MR->>Ctx : apply snapshot (pnl, positions, trades_today)
MR->>EE : find_open_stop_order(symbol)
EE-->>MR : open stop info or None
MR->>MR : reconcile broker vs saved state
MR->>MR : start engine_loop(ctx)
```

**Diagram sources**
- [master_runner.py:725-747](file://master_runner.py#L725-L747)
- [master_runner.py:915-965](file://master_runner.py#L915-L965)
- [execution_engine.py:311-328](file://engine/execution/execution_engine.py#L311-L328)
- [state_store.py:66-79](file://engine/core/state_store.py#L66-L79)

**Section sources**
- [master_runner.py:725-747](file://master_runner.py#L725-L747)
- [master_runner.py:915-965](file://master_runner.py#L915-L965)
- [execution_engine.py:311-328](file://engine/execution/execution_engine.py#L311-L328)
- [state_store.py:66-79](file://engine/core/state_store.py#L66-L79)

## Dependency Analysis
- Context couples all major subsystems without direct imports between them, enabling modular updates and clear ownership.
- StateStore depends only on OS and JSON; it is independent of trading logic but consumed by MasterRunner and LiveEngine.
- LiveEngine depends on ProfitManager and RiskManager for exit and stop calculations; it does not directly persist state except through Context and StateStore.
- ExecutionEngine interacts with the broker and is used by LiveEngine and MasterRunner for recovery.
- Analytics (Slippage) are observational and append CSV records protected by a lock.

```mermaid
graph LR
Ctx["TradingContext"] --> LE["LiveEngine"]
Ctx --> EE["ExecutionEngine"]
Ctx --> RM["RiskManager"]
LE --> PM["ProfitManager"]
LE --> RM
LE --> Store["StateStore"]
EE --> Broker["Broker API"]
MR["MasterRunner"] --> Ctx
MR --> Store
MR --> EE
Analytics["Slippage Analytics"] --> CSV["slippage_log.csv"]
```

**Diagram sources**
- [context.py:10-42](file://engine/core/context.py#L10-L42)
- [live_engine.py:71-184](file://engine/live_engine.py#L71-L184)
- [execution_engine.py:21-371](file://engine/execution/execution_engine.py#L21-L371)
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:15-60](file://engine/risk/risk_manager.py#L15-L60)
- [state_store.py:18-79](file://engine/core/state_store.py#L18-L79)
- [slippage.py:1-131](file://engine/analytics/slippage.py#L1-L131)

**Section sources**
- [context.py:10-42](file://engine/core/context.py#L10-L42)
- [live_engine.py:71-184](file://engine/live_engine.py#L71-L184)
- [execution_engine.py:21-371](file://engine/execution/execution_engine.py#L21-L371)
- [profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [risk_manager.py:15-60](file://engine/risk/risk_manager.py#L15-L60)
- [state_store.py:18-79](file://engine/core/state_store.py#L18-L79)
- [slippage.py:1-131](file://engine/analytics/slippage.py#L1-L131)

## Performance Considerations
- Atomic writes: StateStore uses temporary files and fsync followed by atomic replace to ensure durability and consistency under concurrent writers.
- Polling limits: ExecutionEngine caps fill polling attempts and intervals to balance responsiveness and broker load.
- Deduplication: LiveEngine deduplicates per-minute operations (learner updates, VWAP accumulation) to avoid redundant computations.
- Cost-aware trailing: ProfitManager avoids locking below round-trip costs and activates trailing only after meaningful profit, reducing churn and improving expectancy.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
- Stale snapshots: StateStore ignores previous-day snapshots to prevent PnL/trade count leakage across sessions.
- Fill validation: If broker poll fails to confirm fill, ExecutionEngine falls back to last known price or aborts entry/exit safely.
- Protective stop repair: MasterRunner locates open broker-side stop orders and repairs mismatches to ensure protection remains active.
- Slippage logging: Slippage module appends CSV records with thread-safe locking; use stats function to review recent slippage impact.

**Section sources**
- [state_store.py:66-79](file://engine/core/state_store.py#L66-L79)
- [execution_engine.py:52-88](file://engine/execution/execution_engine.py#L52-L88)
- [execution_engine.py:294-328](file://engine/execution/execution_engine.py#L294-L328)
- [master_runner.py:915-965](file://master_runner.py#L915-L965)
- [slippage.py:35-66](file://engine/analytics/slippage.py#L35-L66)
- [slippage.py:68-131](file://engine/analytics/slippage.py#L68-L131)

## Conclusion
The trading state management system centers around a shared Context and robust persistence via StateStore to maintain consistency across components and survive restarts. LiveEngine coordinates signals and exits using ProfitManager and RiskManager, while ExecutionEngine ensures reliable order placement and protective stop enforcement. MasterRunner orchestrates startup, recovery, and reconciliation to prevent orphaned positions. The design emphasizes safety (atomic writes, duplicate guards, strict stop policies), observability (heartbeat, slippage analytics), and resilience (recovery flows and broker-side protections).

[No sources needed since this section summarizes without analyzing specific files]