---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### CE / PE
- Definition：Call Option (CE) and Put Option (PE) contracts traded on the NFO segment; the system trades BANKNIFTY weekly/monthly options with 100-pt strike spacing and a 30-qty lot size.
- Aliases：call option、put option、CE、PE

### ATM
- Definition：At-the-money strike computed as `round(BANKNIFTY spot / 100) * 100`; used to select the nearest CE/PE contract and to drive option-chain subscription drift detection.
- Aliases：atm strike、atm

### HTF / htf5
- Definition：Higher Time Frame 5-minute SuperTrend direction signal passed into entry logic; values -1 (bearish), 0 (neutral), +1 (bullish). Scalp mode requires explicit agreement (+1 for CE, -1 for PE) rather than mere non-opposition.
- Aliases：htf、htf5、SuperTrend、5m SuperTrend

### ORB
- Definition：Opening Range Breakout window tracked by the engine (high/low bounds); used as a trap filter so entries that break out then snap back inside the range are rejected.
- Aliases：opening range breakout、orb_high、orb_low

### VWAP
- Definition：Volume-weighted average price used as a trend-alignment gate; entries require price to be on the correct side of VWAP relative to trade direction.
- Aliases：vwap、VWAP alignment

### ATR-adaptive SL
- Definition：Stop-loss sizing based on current ATR multiplied by a tier multiplier (STRICT/MED/WIDE) derived from move strength, HTF agreement, VWAP confirmation, and ML activity, with a fixed-point floor to avoid micro-ATR environments producing impossibly tight stops.
- Aliases：atr_sl、ATR stop、adaptive stop

### NO_LIFE exit
- Definition：Scalp-specific time-invalidation rule: if a position has not reached the breakeven zone (entry + SCALP_BE_PTS) within SCALP_NO_LIFE_SECONDS, it is cut at market instead of running the full stop.
- Aliases：no_life、no-life exit

### EXHAUSTION / tail
- Definition：Entry filter that rejects momentum bursts where the last quarter of the lookback window carries most of the total move (exceeds SCALP_EXHAUST_TAIL_FRAC of total_move), treating them as exhausted spikes rather than sustainable trends.
- Aliases：exhaustion filter、tail of spike

### SAFE_SCALP
- Definition：Stricter scalp mode activated when the ML engine is inactive for ML_INACTIVITY_MINUTES; tightens pullback band, raises momentum threshold, and requires HTF agreement rather than allowing neutral.
- Aliases：safe mode、safe scalp

### PAPER_MODE / DRY_RUN
- Definition：Two independent simulation flags: PAPER_MODE seeds candle history from CSV and disables live broker interaction; DRY_RUN skips live broker reconciliation so paper trades do not interfere with real broker state.
- Aliases：paper mode、dry run、simulation

### RECOVERY / RECONCILIATION
- Definition：Startup routine that restores same-day state from `runtime_state.json` and reconciles against the live broker; if a broker position exists without saved state, entries are paused and the orphan position is flattened safely.
- Aliases：restart recovery、broker reconciliation、orphan position

### RANGE regime
- Definition：Market regime classification flag (SKIP_RANGE_REGIME) that historically produced a 31% win rate with negative expectancy; when enabled, the engine skips entries during range-bound sessions.
- Aliases：range days、regime filter

### LUNCH FILTER
- Definition：Time-based filter that blocks entries between 11:00–12:30 to avoid low-volatility chop; kept off by default for dry-run testing and flipped on for real money.
- Aliases：lunch chop filter、LUNCH_FILTER_ENABLED

### STAGE / STAGED SL
- Definition：Multi-stage stop management for scalps: Stage 1 starts with initial SL only until breakeven (SCALP_BE_PTS); Stage 2 moves SL to breakeven; Stage 3 activates trailing (SCALP_TRAIL_START_PTS, SCALP_TRAIL_PTS) that ratchets up below peak.
- Aliases：staged stop、stage 1、stage 2、stage 3
