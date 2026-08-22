# Entry Quality Filter

<cite>
**Referenced Files in This Document**
- [filters.py](file://engine/execution/filters.py)
- [live_engine.py](file://engine/live_engine.py)
- [master_runner.py](file://master_runner.py)
- [backtest_entry_quality.py](file://scripts/backtest_entry_quality.py)
- [phase55_filter.py](file://engine/intelligence/phase55_filter.py)
- [test_entry_confirmation.py](file://tests/test_entry_confirmation.py)
</cite>

## Update Summary
**Changes Made**
- Updated Entry Quality Filter to reflect symmetric coordinate handling for both CE and PE instruments
- Enhanced documentation of the seven-stage rejection pipeline with consistent behavior across option types
- Added comprehensive coverage of module-level rejection counters (_REJECTION_COUNTS, _QUALITY_EVALS) for aggregate statistics
- Updated threshold configuration section to document the mirrored coordinate system where CE uses raw values and PE uses inverted values (1.0 - raw)
- Enhanced quality scoring mechanism documentation with symmetric coordinate handling

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

## Introduction
This document explains the Entry Quality Filter system that prevents low-quality or mistimed entries from being executed. It combines:
- A rejection-first entry timing and quality filter based on completed 1-minute candles and momentum velocity with **symmetric coordinate handling for CE and PE options**.
- An additional Phase 5.5 decision filter for CE/PE confidence thresholds and regime awareness.
- Live confirmation gates (structure, pullback, momentum, HTF alignment, trap filters) used by the master runner.
- A backtest replay script to measure baseline vs filtered performance and collect rejection statistics.

The goal is to reduce whipsaw entries, avoid chasing extended moves, and ensure trades have a reasonable chance of covering costs and moving favorably.

## Project Structure
Entry quality filtering spans several modules:
- Execution-time quality gate: engine/execution/filters.py
- Live engine integration points: engine/live_engine.py
- Master-level confirmation gates: master_runner.py
- Backtest replay and stats: scripts/backtest_entry_quality.py
- Phase 5.5 ML-aware filter: engine/intelligence/phase55_filter.py
- Unit tests for live confirmation gates: tests/test_entry_confirmation.py

```mermaid
graph TB
LE["LiveEngine<br/>check_entry / _check_entry_predict_first"] --> EQ["compute_entry_quality<br/>(filters.py)"]
MR["should_confirm_entry<br/>(master_runner.py)"] --> Gates["Structure / Pullback / Momentum / HTF / Trap"]
BE["Backtest Replay<br/>(backtest_entry_quality.py)"] --> EQ
P55["Phase55 evaluate_phase55_filter<br/>(phase55_filter.py)"] --> LE
```

**Diagram sources**
- [live_engine.py:1090-1111](file://engine/live_engine.py#L1090-L1111)
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [backtest_entry_quality.py:118-125](file://scripts/backtest_entry_quality.py#L118-L125)
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)

**Section sources**
- [filters.py:1-264](file://engine/execution/filters.py#L1-L264)
- [live_engine.py:1090-1111](file://engine/live_engine.py#L1090-L1111)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [backtest_entry_quality.py:1-207](file://scripts/backtest_entry_quality.py#L1-L207)
- [phase55_filter.py:1-200](file://engine/intelligence/phase55_filter.py#L1-L200)
- [test_entry_confirmation.py:1-389](file://tests/test_entry_confirmation.py#L1-L389)

## Core Components
- compute_entry_quality: Rejection-first filter using OHLC geometry, swing move, breakout age, candle wick, momentum velocity, composite score, and cost coverage with **symmetric coordinate handling for CE and PE options**. Returns accepted/rejected with metrics and reason.
- should_confirm_entry: Live confirmation gates applied after an ML signal fires but before execution: structure continuation, pullback band, momentum push, HTF alignment, and trap detection.
- evaluate_phase55_filter: Optional ML-aware filter that can block CE/PE entries based on side-specific confidence thresholds and regime conditions.
- Backtest replay: Generates ORB/momentum candidates, runs compute_entry_quality per bar, simulates exits, and compares baseline vs filtered results plus rejection breakdown.

**Section sources**
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)
- [backtest_entry_quality.py:86-155](file://scripts/backtest_entry_quality.py#L86-L155)

## Architecture Overview
The entry pipeline integrates multiple layers of quality control:

```mermaid
sequenceDiagram
participant LE as "LiveEngine"
participant EQ as "compute_entry_quality"
participant MR as "should_confirm_entry"
participant P55 as "evaluate_phase55_filter"
participant BE as "Backtest Replay"
Note over LE,P55 : Live path
LE->>LE : Detect signal (predict-first or legacy)
LE->>EQ : Evaluate entry timing/quality
EQ-->>LE : accepted? (metrics + reason)
alt rejected
LE->>LE : Count block reason, skip trade
else accepted
LE->>MR : Apply live confirmation gates
MR-->>LE : confirmed? (reason)
alt not confirmed
LE->>LE : Block entry
else confirmed
LE->>P55 : Optional Phase 5.5 check
P55-->>LE : allow_trade? (applied_filters)
alt blocked
LE->>LE : Skip entry
else allowed
LE->>LE : Finalize risk/PnL guard and execute
end
end
end
Note over BE,EQ : Backtest path
BE->>BE : Generate ORB/MOM candidates
BE->>EQ : Run compute_entry_quality per candidate bar
EQ-->>BE : accepted?
BE->>BE : Simulate exit and record PnL
BE-->>BE : Compare baseline vs filtered + rejections
```

**Diagram sources**
- [live_engine.py:1090-1111](file://engine/live_engine.py#L1090-L1111)
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)
- [backtest_entry_quality.py:86-155](file://scripts/backtest_entry_quality.py#L86-L155)

## Detailed Component Analysis

### Entry Timing and Quality Filter (compute_entry_quality)
- Purpose: Reject entries that are late, already moved too far, poorly timed, or unlikely to cover costs.
- Inputs: Completed 1m OHLC window, side (CE/PE), last price, timestamp, optional ORB state, round-trip cost.
- Key checks (in order):
  - MOVE_ALREADY_DONE: Move off recent swing exceeds threshold.
  - LATE_ENTRY: Breakout older than configured seconds.
  - BUYING_AT_TOP: Candle closed at adverse extreme with **symmetric coordinate handling**.
  - REJECTION_CANDLE: Adverse wick dominates range.
  - MOMENTUM_DYING: Momentum velocity falling while price still extends.
  - LOW_QUALITY: Composite score below minimum with **symmetric coordinate scoring**.
  - NOT_PROFITABLE: Expected premium move cannot cover round-trip cost.
- Outputs: accepted flag, reason if rejected, metrics including move_pct, wick_ratio, close_position, breakout_age_s, momentum_velocity_now/prev, score.

**Updated Symmetric Coordinate Handling:**
- **Mirrored Coordinates System**: CE uses raw values while PE uses inverted values (1.0 - raw) to maintain consistent threshold logic
- **CLOSE_POS_MAX**: Standard threshold for buying-at-top detection across all options in mirrored coordinates
- **CLOSE_POS_GOOD**: Threshold for quality scoring bonus in mirrored coordinates
- **Adverse Wick Calculation**: Direction-specific wick calculation that works consistently across option types

```mermaid
flowchart TD
Start(["Start compute_entry_quality"]) --> DataCheck{"Valid data?"}
DataCheck -- No --> AcceptOpen["Return accepted=True (fail-open)"]
DataCheck -- Yes --> Compute["Compute swing move, breakout age, candle geometry, momentum velocity"]
Compute --> Mirror["Apply symmetric coordinate mirroring:<br/>CE: raw values | PE: 1.0 - raw"]
Mirror --> Rule1{"MOVE_ALREADY_DONE?"}
Rule1 -- Yes --> Reject1["Reject: MOVE_ALREADY_DONE"]
Rule1 -- No --> Rule2{"LATE_ENTRY?"}
Rule2 -- Yes --> Reject2["Reject: LATE_ENTRY"]
Rule2 -- No --> Rule3{"BUYING_AT_TOP?"}
Rule3 -- Yes --> Reject3["Reject: BUYING_AT_TOP"]
Rule3 -- No --> Rule4{"REJECTION_CANDLE?"}
Rule4 -- Yes --> Reject4["Reject: REJECTION_CANDLE"]
Rule4 -- No --> Rule5{"MOMENTUM_DYING?"}
Rule5 -- Yes --> Reject5["Reject: MOMENTUM_DYING"]
Rule5 -- No --> Score["Compute composite score with symmetric coordinates"]
Score --> Rule6{"LOW_QUALITY?"}
Rule6 -- Yes --> Reject6["Reject: LOW_QUALITY"]
Rule6 -- No --> Profitable{"NOT_PROFITABLE?"}
Profitable -- Yes --> Reject7["Reject: NOT_PROFITABLE"]
Profitable -- No --> Accept["Return accepted=True with metrics"]
```

**Diagram sources**
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)

**Section sources**
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)

### Module-Level Rejection Counters
- Purpose: Track aggregate statistics for backtest reporting and EOD analysis
- Components:
  - `_REJECTION_COUNTS`: Dictionary tracking rejection reasons and their frequencies
  - `_QUALITY_EVALS`: Counter for total quality evaluations performed
- Functions:
  - `get_rejection_stats()`: Returns comprehensive rejection statistics
  - `reset_rejection_stats()`: Clears counters between backtest folds
  - `_eq_reject()`: Increments rejection counter and returns rejection result

```mermaid
flowchart TD
Eval["Quality Evaluation"] --> Increment["_QUALITY_EVALS += 1"]
Increment --> Check{"Rejection?"}
Check -- Yes --> Count["_REJECTION_COUNTS[reason] += 1"]
Count --> Return["Return rejection with metrics"]
Check -- No --> Success["Return acceptance with metrics"]
```

**Diagram sources**
- [filters.py:66-89](file://engine/execution/filters.py#L66-L89)

**Section sources**
- [filters.py:66-89](file://engine/execution/filters.py#L66-L89)

### Live Confirmation Gates (should_confirm_entry)
- Purpose: Ensure structural continuation, proper pullback, momentum, HTF alignment, and no trap patterns before executing.
- Checks:
  - Structure confirmation: Avoid full reversal of prior move.
  - Pullback entry: Avoid chasing extremes; require retracement within dynamic band.
  - Momentum: Last ticks must continue pushing direction.
  - HTF rule: 5m SuperTrend must agree (neutral/opposing blocks).
  - Trap filters: Failed breakout snap-back and spike-and-reverse patterns.
- Output: (confirmed, reason).

```mermaid
flowchart TD
S(["should_confirm_entry"]) --> History{"Enough history?"}
History -- No --> BlockHist["Block: CONFIRM_NO_HISTORY"]
History -- Yes --> Windows["Split into past/recent windows"]
Windows --> Struct{"Structure OK?"}
Struct -- No --> BlockStruct["Block: CONFIRM_STRUCT_BREAK"]
Struct -- Yes --> Pullback{"Pullback OK?"}
Pullback -- No --> BlockPull["Block: CONFIRM_CHASING_SPIKE / CONFIRM_PULLBACK_FAIL"]
Pullback -- Yes --> Mom{"Momentum OK?"}
Mom -- No --> BlockMom["Block: CONFIRM_NO_MOMENTUM"]
Mom -- Yes --> HTF{"HTF agrees?"}
HTF -- No --> BlockHTF["Block: CONFIRM_HTF_OPPOSES"]
HTF -- Yes --> Trap{"Trap detected?"}
Trap -- Yes --> BlockTrap["Block: CONFIRM_BREAKOUT_TRAP / CONFIRM_SPIKE_TRAP"]
Trap -- No --> Confirm["Confirmed"]
```

**Diagram sources**
- [master_runner.py:799-886](file://master_runner.py#L799-L886)

**Section sources**
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [test_entry_confirmation.py:72-192](file://tests/test_entry_confirmation.py#L72-L192)

### Phase 5.5 Decision Filter
- Purpose: Optional ML-aware gating that can block CE/PE entries based on side-specific confidence thresholds and regime conditions.
- Behavior:
  - CE: Quality confidence threshold; mixed regime blocking when enabled.
  - PE: Directional confidence threshold.
  - Returns allow_trade, confidence_adjustment, blocking_reason, recommendation, applied_filters.
- Integration: Can be applied in the live engine flow to further refine entries.

```mermaid
flowchart TD
PStart(["evaluate_phase55_filter"]) --> Cfg{"Enabled?"}
Cfg -- No --> Allow["allow_trade=True"]
Cfg -- Yes --> Side{"Side"}
Side -- CE --> CEChecks["Quality threshold + Regime filter"]
Side -- PE --> PEChecks["Directional threshold"]
CEChecks --> Decide{"Pass?"}
PEChecks --> Decide
Decide -- No --> Block["Block with reason + recommendation"]
Decide -- Yes --> Allow
```

**Diagram sources**
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)

**Section sources**
- [phase55_filter.py:1-200](file://engine/intelligence/phase55_filter.py#L1-L200)

### Backtest Replay and Metrics
- Purpose: Reproduce ORB/momentum candidates on sealed bars, run compute_entry_quality per bar, simulate exits, and compare baseline vs filtered outcomes. Also aggregates rejection reasons.
- Highlights:
  - Candidate generation: ORB breakouts and momentum bursts.
  - Exit model: SL, target, no-life cutoff, max hold, delta proxy, lot size, round-trip cost.
  - Aggregation: Win rate, net PnL, exit mix, total evaluations, rejections by reason.
  - **Statistics Collection**: Uses module-level counters to track evaluation counts and rejection breakdowns.

```mermaid
sequenceDiagram
participant BR as "Backtest Replay"
participant EQ as "compute_entry_quality"
participant EX as "Exit Simulator"
BR->>BR : Load historical bars and generate candidates
loop For each candidate
BR->>EQ : Evaluate entry quality on sealed bars
alt accepted
BR->>EX : Simulate exit (SL/TARGET/NO_LIFE/MAX_HOLD/EOD)
EX-->>BR : Record PnL and exit reason
else rejected
BR->>BR : Increment rejection counter via module stats
end
end
BR-->>BR : Summarize baseline vs filtered + rejection stats
```

**Diagram sources**
- [backtest_entry_quality.py:86-155](file://scripts/backtest_entry_quality.py#L86-L155)
- [filters.py:71-89](file://engine/execution/filters.py#L71-L89)

**Section sources**
- [backtest_entry_quality.py:1-207](file://scripts/backtest_entry_quality.py#L1-L207)
- [filters.py:71-89](file://engine/execution/filters.py#L71-L89)

## Dependency Analysis
- LiveEngine depends on compute_entry_quality for both predict-first and legacy paths to enforce consistent entry quality.
- MasterRunner's should_confirm_entry provides additional live confirmation gates independent of the candle-based quality filter.
- Phase 5.5 filter is optional and can be integrated to adjust allowance based on ML confidence and regime.
- Backtest Replay depends on compute_entry_quality and round_trip_cost to simulate realistic trading economics.
- **Enhanced Statistics**: Module-level counters provide aggregate data for backtest analysis and reporting.

```mermaid
graph LR
LE["LiveEngine"] --> EQ["compute_entry_quality"]
LE --> MRGates["should_confirm_entry"]
LE --> P55["evaluate_phase55_filter"]
BE["Backtest Replay"] --> EQ
BE --> Cost["round_trip_cost"]
EQ --> Stats["_REJECTION_COUNTS & _QUALITY_EVALS"]
```

**Diagram sources**
- [live_engine.py:1090-1111](file://engine/live_engine.py#L1090-L1111)
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)
- [backtest_entry_quality.py:23-33](file://scripts/backtest_entry_quality.py#L23-L33)

**Section sources**
- [live_engine.py:1090-1111](file://engine/live_engine.py#L1090-L1111)
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [phase55_filter.py:96-199](file://engine/intelligence/phase55_filter.py#L96-L199)
- [backtest_entry_quality.py:23-33](file://scripts/backtest_entry_quality.py#L23-L33)

## Performance Considerations
- compute_entry_quality operates on completed 1m candles and uses lightweight calculations (swing extremes, wick ratios, momentum velocity). Complexity is linear in the lookback window size.
- The rejection-first design ensures early exits on failing rules, minimizing unnecessary computation.
- Backtest Replay processes only sealed bars and throttles momentum candidates to avoid burst spam, keeping runtime efficient.
- Phase 5.5 filter adds minimal overhead via confidence lookups and regime inference.
- **Symmetric coordinate handling** adds negligible computational overhead while providing consistent behavior across option types.
- **Module-level counters** provide O(1) increment operations with minimal memory overhead.

## Troubleshooting Guide
Common rejection reasons and diagnostics:
- MOVE_ALREADY_DONE: Entry occurs after a large move; consider waiting for pullback or next setup.
- LATE_ENTRY: Breakout too old; wait for fresh signals.
- BUYING_AT_TOP / REJECTION_CANDLE: Poor candle geometry; avoid chasing or entering into adverse wicks.
- MOMENTUM_DYING: Momentum weakening while price extends; wait for consolidation or trend resumption.
- LOW_QUALITY: Composite score insufficient; review thresholds and market regime.
- NOT_PROFITABLE: Expected move cannot cover costs; adjust lot size, delta proxy, or wait for better setups.
- Live confirmation blocks: CONFIRM_NO_HISTORY, CONFIRM_STRUCT_BREAK, CONFIRM_CHASING_SPIKE, CONFIRM_PULLBACK_FAIL, CONFIRM_NO_MOMENTUM, CONFIRM_HTF_OPPOSES, CONFIRM_BREAKOUT_TRAP, CONFIRM_SPIKE_TRAP.

**Updated Statistics Tracking:**
- Use `get_rejection_stats()` to retrieve comprehensive rejection breakdowns
- Monitor `_QUALITY_EVALS` to track total quality evaluations
- Reset counters between backtest folds using `reset_rejection_stats()`

Use backtest replay to inspect rejection breakdowns and validate parameter changes.

**Section sources**
- [filters.py:126-264](file://engine/execution/filters.py#L126-L264)
- [master_runner.py:799-886](file://master_runner.py#L799-L886)
- [backtest_entry_quality.py:198-202](file://scripts/backtest_entry_quality.py#L198-L202)

## Conclusion
The Entry Quality Filter system combines robust, rejection-first timing and quality checks with **symmetric coordinate handling for CE and PE options**, live confirmation gates, and optional ML-aware Phase 5.5 filtering. The symmetric approach addresses the different characteristics between call and put options through mirrored coordinates, providing consistent behavior across option types while maintaining the same threshold logic. The addition of module-level rejection counters enables comprehensive statistical analysis and reporting. Together, these layers significantly reduce low-probability entries, protect against traps and late entries, and improve the expected profitability of trades. The backtest replay tool enables continuous validation and tuning of parameters and thresholds with detailed rejection statistics.