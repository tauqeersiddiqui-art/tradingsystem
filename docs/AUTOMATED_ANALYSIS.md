# AUTOMATED PROFITABILITY ANALYSIS — README

## Overview

Complete automated system for extracting profitability truth from PostgreSQL trades table.

**NO MANUAL INTERVENTION. NO LOGS. NO SIMULATIONS. DATABASE TRUTH ONLY.**

---

## Quick Start

### 1. Run Analysis Now

```bash
python RUN_ANALYSIS.py
```

Extracts all trades, computes all metrics, delivers verdict.

---

### 2. Setup Automated Daily Analysis

```bash
python scripts/setup_automated_analysis.py
```

Schedules:
- Daily analysis at 4:00 PM (after market close)
- Telegram summary sent automatically
- Reports saved to `data/reports/`

---

## What Gets Analyzed

### Core Metrics
- Win rate
- Net PnL (cost-adjusted via `cost_model.py`)
- Profit factor
- Expectancy (Rs per trade)
- Risk/reward ratio
- Max drawdown (Rs and %)

### Strategy Breakdown
- ORB vs ML vs HYBRID performance
- Win rate per strategy
- PnL per strategy
- Best/worst strategy identified

### Time Analysis
- PnL by hour (9:15-15:30)
- PnL by weekday
- First hour vs rest of day
- Best/worst trading hours

### Market Regime
- PnL by regime (TREND_UP, RANGE, etc.)
- Win rate per regime
- Expectancy per regime

### Reality Check
- Theoretical edge (gross PnL)
- Actual edge (net PnL)
- Slippage cost and impact
- Execution quality score

### Drawdown Analysis
- Complete equity curve
- Max drawdown period
- Longest losing streak
- Recovery time

---

## Verdict System

### ✓ PROFITABLE & STABLE
- Positive expectancy
- Profit factor > 1.3
- Max DD < 40% of PnL
- **Action:** Continue trading

### ⚠ PROFITABLE BUT UNSTABLE
- Positive PnL but high volatility
- Max DD > 40%
- **Action:** Reduce position size

### ⚠ BREAK-EVEN
- Near-zero PnL
- **Action:** Re-evaluate parameters

### ✗ LOSING SYSTEM
- Negative expectancy OR profit factor < 1.0
- **Action:** STOP TRADING

---

## Truth Filters (Automatic)

System automatically flags:

- ✗ Profit factor < 1.3 → WARNING
- ✗ Expectancy < 0 → LOSING SYSTEM
- ✗ Drawdown > 40% → UNSTABLE
- ✗ Win rate < 35% AND RR < 1.5 → WEAK EDGE

**NO SOFT LANGUAGE. TRUTH ONLY.**

---

## Report Outputs

### Console
```
python RUN_ANALYSIS.py
```

Full report to stdout.

### JSON
```
python scripts/analysis/generate_performance_report.py --output json
```

### CSV
```
python scripts/analysis/generate_performance_report.py --output csv
```

### All Formats
```
python scripts/analysis/generate_performance_report.py --output all
```

---

## Daily Automation

After running `setup_automated_analysis.py`:

1. **4:00 PM daily** (after market close)
   - Analyzes today's trades
   - Generates report → `data/reports/daily_YYYYMMDD.txt`
   - Sends Telegram summary

2. **Telegram Summary Format:**
   ```
   ✓ DAILY PERFORMANCE REPORT
   2026-08-08

   TRADES: 12
   WIN RATE: 58%
   NET PnL: ₹4,250
   PROFIT FACTOR: 2.1
   EXPECTANCY: ₹354

   MAX DD: ₹1,800 (42%)

   BEST: ORB+ML (₹3,200)

   STATUS: PROFITABLE & STABLE
   ```

3. **Report Retention:** 30 days (auto-cleanup)

---

## Integration with Trading System

Add to `master_runner.py` (end of session):

```python
# At end of trading day (after market close)
if datetime.now().time() >= dtime(15, 30):
    from scripts.analysis.run_daily_analysis import main as run_analysis
    run_analysis()
```

Or let scheduler handle it (recommended).

---

## Database Requirements

Analysis reads ONLY from:
- `trades` table (PostgreSQL)

Required columns:
- `entry_time`, `exit_time`
- `gross_pnl`, `net_pnl`
- `strategy`, `regime`
- `exit_reason`

**Cost-adjusted net PnL is the single source of truth.**

---

## Multi-Index Support (Future)

Currently: BANKNIFTY only

To add NIFTY/SENSEX:
1. Add `index` column to `trades` table
2. Update `performance_analyzer.py` to filter by index
3. Run analysis per index

---

## Troubleshooting

### No trades found
→ System hasn't traded yet. Run in paper mode first.

### PostgreSQL connection failed
→ Check `.env`:
```
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=trading_system
POSTGRES_USER=trading_user
POSTGRES_PASSWORD=...
```

### Reality check unavailable
→ No execution audit data. System needs to complete trades with audit logging enabled.

---

## Exit Codes

- `0` — Profitable & stable
- `1` — Losing system (DO NOT TRADE)
- `2` — Break-even or unstable (review needed)

Use in CI/CD:
```bash
python RUN_ANALYSIS.py
if [ $? -eq 1 ]; then
    echo "LOSING SYSTEM DETECTED — STOPPING DEPLOYMENT"
    exit 1
fi
```

---

## Files Created

```
data/reports/
├── analysis_20260808_160015.txt    # Full report
├── daily_20260808.txt               # Daily summary
├── daily_20260808.json              # Daily JSON
└── ...
```

---

## The Only Question That Matters

After all the engineering, safety, and reliability work:

**Does this system make money?**

Run `python RUN_ANALYSIS.py` to find out.

---

**NO SIMULATIONS. NO LOGS. DATABASE TRUTH ONLY.**
