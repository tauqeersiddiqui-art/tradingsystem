# Parity Testing

<cite>
**Referenced Files in This Document**
- [research/backtest/engine/parity_test.py](file://research/backtest/engine/parity_test.py)
- [research/backtest/tests/test_parity.py](file://research/backtest/tests/test_parity.py)
- [research/backtest/engine/researchengine.py](file://research/backtest/engine/researchengine.py)
- [research/backtest/engine/research_engine.py](file://research/backtest/engine/research_engine.py)
- [engine/live_engine.py](file://engine/live_engine.py)
- [backtest/backtest_engine.py](file://backtest/backtest_engine.py)
- [engine/execution/cost_model.py](file://engine/execution/cost_model.py)
- [engine/execution/profit_manager.py](file://engine/execution/profit_manager.py)
- [engine/risk/risk_manager.py](file://engine/risk/risk_manager.py)
- [ml/predictor_champion.py](file://ml/predictor_champion.py)
- [engine/config/config.py](file://engine/config/config.py)
- [research/backtest/tests/golden_trades.py](file://research/backtest/tests/golden_trades.py)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)
10. [Appendices](#appendices)

## Introduction
This document explains the parity testing framework that ensures research and live environments produce identical results for trading decisions. It covers how the system compares outputs between the research engine and the live engine to maintain consistency across development and production, including signal generation, position sizing, and execution logic. It also documents techniques for synchronizing test data, handling time-based differences, managing external dependencies, and writing parity tests for strategies, indicators, and risk rules. Finally, it addresses common parity issues such as floating-point precision, timestamp handling, and market data variations, with guidance on debugging failures and maintaining regression tests.

## Project Structure
The parity testing framework is centered around a thin wrapper that delegates to the live engine’s public methods, ensuring no logic duplication. The research backtest engine mirrors live behavior by calling the same feature builders, predictors, risk managers, and profit managers. Tests validate entry signals, exit logic, sizing invariants, and cost calculations using deterministic mocks where necessary.

```mermaid
graph TB
subgraph "Research"
RE["ResearchBacktestEngine"]
PE["ParityTestWrapper"]
TST["test_parity.py"]
end
subgraph "Live"
LE["LiveEngine"]
PM["profit_manager"]
RM["risk_manager"]
CM["cost_model"]
PR["ChampionPredictor"]
end
RE --> LE
PE --> LE
TST --> RE
LE --> PM
LE --> RM
LE --> CM
LE --> PR
```

**Diagram sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)
- [research/backtest/engine/parity_test.py:30-57](file://research/backtest/engine/parity_test.py#L30-L57)
- [engine/live_engine.py:71-128](file://engine/live_engine.py#L71-L128)
- [engine/execution/profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)
- [engine/execution/cost_model.py:22-45](file://engine/execution/cost_model.py#L22-L45)
- [ml/predictor_champion.py:57-100](file://ml/predictor_champion.py#L57-L100)

**Section sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)
- [research/backtest/engine/parity_test.py:30-57](file://research/backtest/engine/parity_test.py#L30-L57)
- [engine/live_engine.py:71-128](file://engine/live_engine.py#L71-L128)

## Core Components
- ResearchBacktestEngine: A thin wrapper that delegates entry and exit decisions to LiveEngine, validates sizing invariants, and uses the same cost model and risk manager.
- ParityTestWrapper: Provides minimal wrappers around LiveEngine.check_entry and check_exit for parity runs.
- LiveEngine: Production decision engine implementing ORB tracking, feature building, ML prediction, and exit logic via profit_manager.
- BacktestSignalEngine: Institutional-grade backtesting engine mirroring LiveEngine step logic without broker/context dependencies.
- Cost Model: Authoritative source for round-trip costs and net PnL; used consistently across systems.
- Profit Manager: Centralized trailing stop ladder and exit triggers (target hit, drawdown, stop loss).
- Risk Manager: Computes entry stops and targets based on ATR and regime.
- ChampionPredictor: Loads models and returns probabilities for CE/PE directions.

Key responsibilities:
- Sizing invariants enforce whole-lot quantities aligned with Bank Nifty lot size.
- Feature building uses shared functions to ensure identical inputs to ML models.
- Exit logic delegates to profit_manager for consistent trailing and exits.
- Cost calculations are centralized to avoid discrepancies.

**Section sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)
- [research/backtest/engine/parity_test.py:30-57](file://research/backtest/engine/parity_test.py#L30-L57)
- [engine/live_engine.py:71-128](file://engine/live_engine.py#L71-L128)
- [backtest/backtest_engine.py:196-260](file://backtest/backtest_engine.py#L196-L260)
- [engine/execution/cost_model.py:22-45](file://engine/execution/cost_model.py#L22-L45)
- [engine/execution/profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)
- [ml/predictor_champion.py:57-100](file://ml/predictor_champion.py#L57-L100)

## Architecture Overview
The parity testing architecture ensures that research and live engines share the same decision pipeline. Research calls into LiveEngine for entry and exit decisions, while tests inject deterministic mocks for ML components to stabilize outcomes. Cost and risk modules are single sources of truth, preventing divergence.

```mermaid
sequenceDiagram
participant Test as "test_parity.py"
participant RBE as "ResearchBacktestEngine"
participant LE as "LiveEngine"
participant PM as "profit_manager"
participant RM as "risk_manager"
participant CM as "cost_model"
participant PR as "ChampionPredictor"
Test->>RBE : Initialize with Config and lots_per_trade
RBE->>LE : create context and instantiate LiveEngine
Test->>RBE : _check_entry_live(window_df, ts)
RBE->>LE : check_entry(df_window, ts)
LE->>PR : predict(features, direction)
LE->>RM : compute_entry_stops(price, atr, regime)
LE-->>RBE : signal dict or None
RBE-->>Test : signal or None
Test->>RBE : _check_exit_live(position, ltp, held_seconds)
RBE->>LE : check_exit(position, ltp, held_seconds)
LE->>PM : manage_position(entry, ltp, lot_size, stop_loss, max_pnl, ml_prob, target, side)
PM-->>LE : (new_stop, new_max_pnl, reason, scale_out_info)
LE-->>RBE : (should_exit, reason)
RBE-->>Test : (bool, str)
```

**Diagram sources**
- [research/backtest/engine/researchengine.py:107-121](file://research/backtest/engine/researchengine.py#L107-L121)
- [engine/live_engine.py:798-860](file://engine/live_engine.py#L798-L860)
- [engine/execution/profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)
- [ml/predictor_champion.py:151-204](file://ml/predictor_champion.py#L151-L204)

## Detailed Component Analysis

### ResearchBacktestEngine
- Purpose: Thin wrapper around LiveEngine to mirror live decisions without duplicating logic.
- Key behaviors:
  - Validates sizing invariants (qty > 0, multiple of lot size, multiple of 30).
  - Delegates ORB updates, feature building, entry checks, and exit checks to LiveEngine.
  - Uses shared cost_model and risk_manager for consistent PnL and stops.
  - Generates parity reports for sizing and cost calculations.

```mermaid
classDiagram
class ResearchBacktestEngine {
+config Config
+lot_size int
+lots_per_trade int
+qty int
+enable_ce bool
+enable_pe bool
+live_engine LiveEngine
+predictor ChampionPredictor
+learner IntradayMLLearner
+_create_context() Context
+_validate_sizing_invariants() void
+_update_orb(candle, ts) void
+_build_features(df_window, ts) dict
+_check_entry_live(df_window, ts) dict|None
+_check_exit_live(position, ltp, held_seconds) tuple
+run_parity_tests(df, start_date, end_date) dict
+get_sizing_parity_report() dict
+get_cost_parity_report() dict
}
class LiveEngine
class Config
class ChampionPredictor
class IntradayMLLearner
ResearchBacktestEngine --> LiveEngine : "delegates entry/exit"
ResearchBacktestEngine --> Config : "uses"
ResearchBacktestEngine --> ChampionPredictor : "initializes"
ResearchBacktestEngine --> IntradayMLLearner : "initializes"
```

**Diagram sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)

**Section sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)

### ParityTestWrapper
- Purpose: Minimal wrapper to call LiveEngine.check_entry and check_exit for parity runs.
- Behavior:
  - Instantiates LiveEngine with a minimal context.
  - Returns entry signals and exit decisions exactly as LiveEngine produces.
  - Includes helper functions to verify entry and exit invariants.

```mermaid
flowchart TD
Start([Start Parity Run]) --> Init["Initialize ParityTestWrapper"]
Init --> Loop["Iterate historical candles"]
Loop --> Entry["Call LiveEngine.check_entry"]
Entry --> Signal{"Signal?"}
Signal --> |Yes| Verify["Verify entry invariants"]
Signal --> |No| NextCandle["Next candle"]
Verify --> NextCandle
NextCandle --> End([End Parity Run])
```

**Diagram sources**
- [research/backtest/engine/parity_test.py:30-57](file://research/backtest/engine/parity_test.py#L30-L57)
- [research/backtest/engine/parity_test.py:118-162](file://research/backtest/engine/parity_test.py#L118-L162)

**Section sources**
- [research/backtest/engine/parity_test.py:30-57](file://research/backtest/engine/parity_test.py#L30-L57)
- [research/backtest/engine/parity_test.py:118-162](file://research/backtest/engine/parity_test.py#L118-L162)

### LiveEngine
- Purpose: Production decision engine implementing ORB tracking, feature building, ML prediction, and exit logic.
- Key behaviors:
  - Maintains ORB state and reconstructs if needed.
  - Builds features using shared function and validates completeness.
  - Applies session filters, day classification, and HTF alignment.
  - Delegates exit logic to profit_manager and enforces time-based exits.

```mermaid
sequenceDiagram
participant LE as "LiveEngine"
participant PR as "ChampionPredictor"
participant RM as "risk_manager"
participant PM as "profit_manager"
LE->>LE : update_orb(candle, ts)
LE->>LE : build_features(df_window, ts)
LE->>PR : predict(features, direction)
LE->>RM : compute_entry_stops(price, atr, regime)
LE-->>LE : signal dict or None
LE->>PM : manage_position(entry, ltp, lot_size, stop_loss, max_pnl, ml_prob, target, side)
PM-->>LE : (new_stop, new_max_pnl, reason, scale_out_info)
LE-->>LE : should_exit, reason
```

**Diagram sources**
- [engine/live_engine.py:190-217](file://engine/live_engine.py#L190-L217)
- [engine/live_engine.py:445-470](file://engine/live_engine.py#L445-L470)
- [engine/live_engine.py:798-860](file://engine/live_engine.py#L798-L860)
- [engine/execution/profit_manager.py:173-225](file://engine/execution/profit_manager.py#L173-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

**Section sources**
- [engine/live_engine.py:190-217](file://engine/live_engine.py#L190-L217)
- [engine/live_engine.py:445-470](file://engine/live_engine.py#L445-L470)
- [engine/live_engine.py:798-860](file://engine/live_engine.py#L798-L860)

### BacktestSignalEngine
- Purpose: Institutional-grade backtesting engine mirroring LiveEngine step logic without broker/context dependencies.
- Key behaviors:
  - Reuses feature builder, predictor, risk manager, and profit manager.
  - Tracks telemetry for raw signals, ML passes, blocks, and executions.
  - Implements session filters, day classification, and HTF alignment.

```mermaid
flowchart TD
Start([Backtest Step]) --> UpdateORB["Update ORB"]
UpdateORB --> ClassifyDay["Classify Day"]
ClassifyDay --> BuildFeatures["Build Features"]
BuildFeatures --> Predict["Predict CE/PE"]
Predict --> DirectionGate{"Direction Bias OK?"}
DirectionGate --> |No| Skip["Skip Candle"]
DirectionGate --> |Yes| CheckORB["Check ORB Breakout"]
CheckORB --> ComputeStops["Compute Stops"]
ComputeStops --> ExpectedPnL{"Expected PnL >= Min?"}
ExpectedPnL --> |No| Skip
ExpectedPnL --> |Yes| ReturnSignal["Return Signal"]
```

**Diagram sources**
- [backtest/backtest_engine.py:289-327](file://backtest/backtest_engine.py#L289-L327)
- [backtest/backtest_engine.py:431-758](file://backtest/backtest_engine.py#L431-L758)

**Section sources**
- [backtest/backtest_engine.py:289-327](file://backtest/backtest_engine.py#L289-L327)
- [backtest/backtest_engine.py:431-758](file://backtest/backtest_engine.py#L431-L758)

### Cost Model, Profit Manager, Risk Manager
- Cost Model: Centralized calculation of round-trip costs and net PnL; ensures consistency across systems.
- Profit Manager: Trailing stop ladder and exit triggers; enforces cost-aware locks and drawdown exits.
- Risk Manager: Computes entry stops and targets based on ATR and regime; caps worst-case losses.

```mermaid
classDiagram
class CostModel {
+lot_qty(config) int
+round_trip_cost(qty, config) float
+net_pnl(gross_pnl, qty, config) float
}
class ProfitManager {
+manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob, target, config, side) tuple
+ladder_stop(entry_price, qty, max_pnl, current_stop, config, side) tuple
}
class RiskManager {
+compute_entry_stops(entry_premium, atr, regime, delta, side) tuple
}
CostModel <.. ProfitManager : "uses"
RiskManager <.. ProfitManager : "uses"
```

**Diagram sources**
- [engine/execution/cost_model.py:22-45](file://engine/execution/cost_model.py#L22-L45)
- [engine/execution/profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

**Section sources**
- [engine/execution/cost_model.py:22-45](file://engine/execution/cost_model.py#L22-L45)
- [engine/execution/profit_manager.py:116-225](file://engine/execution/profit_manager.py#L116-L225)
- [engine/risk/risk_manager.py:20-60](file://engine/risk/risk_manager.py#L20-L60)

### ChampionPredictor
- Purpose: Loads ML models and returns probabilities for CE/PE directions.
- Key behaviors:
  - Validates feature presence and handles missing/invalid features gracefully.
  - Supports ensemble mode when both LGBM and CatBoost models exist.
  - Returns probabilities rounded to four decimals for consistency.

```mermaid
flowchart TD
Start([Predict]) --> LoadModel["Load CE/PE Model"]
LoadModel --> ValidateFeatures{"Features Valid?"}
ValidateFeatures --> |No| ReturnNone["Return None"]
ValidateFeatures --> |Yes| BuildInput["Build Input DataFrame"]
BuildInput --> PredictProb["Predict Probability"]
PredictProb --> Ensemble{"Ensemble Mode?"}
Ensemble --> |Yes| Average["Average LGBM + CatBoost"]
Ensemble --> |No| UseLGBM["Use LGBM Only"]
Average --> Round["Round to 4 Decimals"]
UseLGBM --> Round
Round --> ReturnProb["Return Probability"]
```

**Diagram sources**
- [ml/predictor_champion.py:57-100](file://ml/predictor_champion.py#L57-L100)
- [ml/predictor_champion.py:151-204](file://ml/predictor_champion.py#L151-L204)

**Section sources**
- [ml/predictor_champion.py:57-100](file://ml/predictor_champion.py#L57-L100)
- [ml/predictor_champion.py:151-204](file://ml/predictor_champion.py#L151-L204)

## Dependency Analysis
The parity testing framework relies on shared components to ensure consistency:
- ResearchBacktestEngine depends on LiveEngine for decision logic.
- LiveEngine depends on profit_manager, risk_manager, and cost_model for execution and risk.
- Tests use mocks for ML components to stabilize outcomes.
- Configuration drives behavior across all components.

```mermaid
graph TB
RBE["ResearchBacktestEngine"] --> LE["LiveEngine"]
LE --> PM["profit_manager"]
LE --> RM["risk_manager"]
LE --> CM["cost_model"]
LE --> PR["ChampionPredictor"]
TST["test_parity.py"] --> RBE
CFG["Config"] --> RBE
CFG --> LE
```

**Diagram sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)
- [engine/live_engine.py:71-128](file://engine/live_engine.py#L71-L128)
- [engine/config/config.py:4-48](file://engine/config/config.py#L4-L48)

**Section sources**
- [research/backtest/engine/researchengine.py:37-121](file://research/backtest/engine/researchengine.py#L37-L121)
- [engine/live_engine.py:71-128](file://engine/live_engine.py#L71-L128)
- [engine/config/config.py:4-48](file://engine/config/config.py#L4-L48)

## Performance Considerations
- Feature building is optimized to reuse shared functions and avoid redundant computations.
- ORB reconstruction minimizes API calls and handles edge cases gracefully.
- Mocking ML components in tests reduces runtime overhead and ensures deterministic outcomes.
- Telemetry in BacktestSignalEngine helps identify bottlenecks and filter inefficiencies.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common parity issues and resolutions:
- Floating-point precision: Use rounding functions consistently (e.g., round to two decimals for PnL) and compare with tolerance thresholds.
- Timestamp handling: Ensure timestamps are normalized to datetime objects and filtered by market hours.
- Market data variations: Use deterministic historical datasets and mock external dependencies (predictor, learner) for stable tests.
- Sizing mismatches: Validate lot sizes and quantities against configuration and enforce whole-lot invariants.
- Exit logic divergence: Delegate exit decisions to profit_manager and verify trailing stop behavior.

Debugging steps:
- Run parity tests with verbose logging to trace decision paths.
- Compare signal fields (side, qty, price, stop_loss, target, ml_prob) between research and live outputs.
- Inspect telemetry from BacktestSignalEngine to identify blocked or skipped signals.
- Validate cost calculations using cost_model and ensure net PnL matches expected values.

**Section sources**
- [research/backtest/tests/test_parity.py:130-180](file://research/backtest/tests/test_parity.py#L130-L180)
- [research/backtest/tests/test_parity.py:231-282](file://research/backtest/tests/test_parity.py#L231-L282)
- [research/backtest/tests/test_parity.py:288-447](file://research/backtest/tests/test_parity.py#L288-L447)
- [engine/execution/cost_model.py:22-45](file://engine/execution/cost_model.py#L22-L45)

## Conclusion
The parity testing framework ensures research and live environments produce identical results by delegating decision logic to the live engine and validating outputs through comprehensive tests. Shared components like cost_model, profit_manager, and risk_manager prevent divergence, while mocks stabilize ML-dependent tests. By following the guidelines in this document, developers can write robust parity tests for strategies, indicators, and risk rules, and effectively debug and resolve parity failures.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Writing Parity Tests for New Strategies
- Use ResearchBacktestEngine to delegate entry and exit decisions to LiveEngine.
- Inject deterministic mocks for ML components to ensure reproducible outcomes.
- Validate sizing invariants and cost calculations using cost_model.
- Test exit logic by simulating various scenarios (stop loss, trailing, time-based exits).

**Section sources**
- [research/backtest/engine/researchengine.py:107-121](file://research/backtest/engine/researchengine.py#L107-L121)
- [research/backtest/tests/test_parity.py:83-123](file://research/backtest/tests/test_parity.py#L83-L123)
- [research/backtest/tests/golden_trades.py:23-62](file://research/backtest/tests/golden_trades.py#L23-L62)

### Synchronizing Test Data
- Use historical datasets from data/historical directory.
- Normalize timestamps and filter by market hours.
- Ensure rolling windows are consistent between research and live runs.

**Section sources**
- [research/backtest/tests/test_parity.py:68-81](file://research/backtest/tests/test_parity.py#L68-L81)

### Handling External Dependencies
- Mock ML components (ChampionPredictor, IntradayMLLearner) to avoid import errors and stabilize tests.
- Use environment variables to configure behavior (e.g., WARMUP_MINUTES, MAX_HOLD_SECONDS).

**Section sources**
- [research/backtest/tests/test_parity.py:25-55](file://research/backtest/tests/test_parity.py#L25-L55)
- [engine/config/config.py:34-48](file://engine/config/config.py#L34-L48)