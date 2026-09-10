# Missing Data & Environment Variables

This file documents exactly what data files and environment variables are needed to run the full pipeline but are currently missing or not accessible.

## Missing Data Files

### 1. Option Premium Data (for non-synthetic signals)
- **Expected path**: `data/option_premiums_2026/*.csv`
- **Format**: Minute-level OHLC for BANKNIFTY CE/PE options
- **Columns needed**: `timestamp, symbol, open, high, low, close, volume, strike, expiry, option_type`
- **Current status**: NOT FOUND - using synthetic option pricing via `OptionPriceSimulator` in backtest
- **Impact**: `make_signals_with_future_pnl.py` flags all signals as `synthetic_flag=True`

### 2. Historical Data (AVAILABLE)
- **Path**: `data/historical/banknifty_1m_full.csv` ✓
- **Rows**: ~1.3M rows (2021-2026)
- **Columns**: `date,open,high,low,close,volume`

## Missing Environment Variables

### Required for Live Trading (NOT needed for backtests)
| Variable | Description | Default | Required For |
|----------|-------------|---------|--------------|
| `KITE_TOTP_SECRET` | TOTP secret for Zerodha login | None | Live broker connection |
| `KITE_API_KEY` | Zerodha API key | None | Live broker connection |
| `KITE_API_SECRET` | Zerodha API secret | None | Live broker connection |
| `KITE_ACCESS_TOKEN` | Pre-generated access token | None | Live broker connection |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | None | Telegram notifications |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | None | Telegram notifications |

### Required for AI Brain Review (optional)
| Variable | Description | Default | Required For |
|----------|-------------|---------|--------------|
| `LLM_API_KEY` | OpenAI-compatible API key | `freellmapi-xxx` (placeholder) | AI trade review |
| `LLM_BASE_URL` | API base URL | `https://api.openai.com/v1` | AI trade review |
| `LLM_MODEL` | Model name | `gpt-4o-mini` | AI trade review |

### Backtest Configuration (have defaults)
| Variable | Description | Default |
|----------|-------------|---------|
| `COST_PER_LOT` | Round-trip cost per lot (INR) | `66.0` |
| `CHAMPION_THRESHOLD` | ML probability threshold | `0.65` |
| `MAX_TRADES_PER_DAY` | Daily trade limit | `30` |
| `LOT_SIZE` | Units per lot | `30` |
| `ENABLE_VOL_SCALING` | Volatility sizing flag | `0` (OFF) |
| `ENABLE_ML_EXITS` | ML exit models flag | `0` (OFF) |
| `USE_LADDERING` | Ladder entry flag | `0` (OFF) |
| `USE_ATR_TRAIL` | ATR trailing stop flag | `0` (OFF) |

## Quick Start (Synthetic Data)

All backtests and threshold sweeps can run with **only the historical candle data** (which exists). The pipeline synthesizes option prices using the `OptionPriceSimulator` in `backtest/backtest_engine.py`.

```bash
# 1. Generate signals with future P&L (uses synthetic option pricing)
python tools/make_signals_with_future_pnl.py

# 2. Sweep thresholds to find best expectancy
python tools/threshold_sweep.py --horizon 5 --cost 66 --min-trades 50

# 3. Calibrate probabilities and compute position sizes
python tools/calibrate_and_size.py --horizon 5

# 4. Build stop/MFE dataset and train exit models
python tools/build_stop_mfe_dataset.py --horizons 5,15
python models/stop_mfe_train.py --horizons 5,15

# 5. Run backtest sweep (1x, 2x, 3x cost)
python tools/run_backtest_sweep.py --cost-multipliers 1,2,3 --tail 50000
```

## Blocker Summary

| Blocker | Severity | Workaround |
|---------|----------|------------|
| No option premium OHLC | Medium | Synthetic pricing (implemented) |
| No TOTP secret for live | High (live only) | Use `--dry-totp` / PAPER_MODE=1 |
| No LLM_API_KEY | Low (optional) | AI review skipped silently |
| Safety classifier blocking runs | High (this session) | Wait for classifier to unblock |

## Files Created This Session (Ready to Run)

```
tools/
├── make_signals_with_future_pnl.py      # P3.1 - Signal + future P&L dataset
├── threshold_sweep.py                   # P3.2 - Threshold expectancy sweep
├── calibrate_and_size.py                # P3.3 - Probability calibration + sizing
├── build_stop_mfe_dataset.py            # P4.1 - Stop hit / MFE dataset
├── run_backtest_sweep.py                # P5 - Multi-slippage backtest runner
models/
├── stop_mfe_train.py                    # P4.2 - Train stop/MFE models
tests/
├── test_threshold_sweep.py              # Unit tests for threshold sweep
├── test_calibrate_and_size.py           # Unit tests for calibration
├── test_stop_mfe.py                     # Unit tests for stop/MFE
.github/workflows/
├── pytest.yml                           # CI workflow
```

All scripts have CLI defaults matching the spec:
- `cost=66` (per lot per side)
- `min_trades=50`
- `horizons=[1,3,5,15]`