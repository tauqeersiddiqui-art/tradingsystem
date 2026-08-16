# Research Backtest Engine

A clean room research layer that mirrors live trading logic without modifying production code.

## Structure

```
research/backtest/
├── engine/
│   ├── __init__.py
│   ├── parity_test.py      # LIVE vs RESEARCH comparison
│   ├── golden_trades.py    # Deterministic historical trade cases
│   └── run_research.py     # Main backtest runner
├── data/
│   └── market_data.py      # Historical data loader
├── results/
│   ├── trade_log.csv       # Generated trades (validated)
│   └── metrics.json        # Performance metrics
├── tests/
│   └── test_size_validity.py
└── README.md
```

## Live Entry Path (Traced from live_engine.py)

1. **Data Ingestion** → `market_data` dict with candle + df_window
2. **Feature Building** → `build_features()` from `ml.feature_config`
3. **ORB** → `update_orb()` (9:15–9:29, reconstructed from index)
4. **ML Prediction** → `ChampionPredictor.predict_features()` for CE/PE
5. **Threshold** → `learner.get_ml_threshold()` (adaptive) + side floors
   - PE floor: `_MIN_ML_FLOOR = 0.55`
   - CE floor: `_CE_ML_FLOOR = 0.65`
6. **Trend Confirmation** → `htf_supertrend_dir(5m)`, VWAP bias
7. **Decision Intelligence** → weighted FINAL_SCORE (ML*0.5 + ORB*0.2 + Global*0.2 + Vol*0.1)
8. **Risk** → `compute_entry_stops()` (ATR-based SL/TP)
9. **PnL Guard** → `expected_pnl >= _MIN_EXPECTED_PNL (150)`
10. **Final Decision** → signal emitted if all gates pass

## Live Exit Path

1. **LTP Simulation** → Option pricer premium change
2. **SL** → Hard stop (premium below 90% entry)
3. **Trailing** → `profit_manager.manage_position()` (drawdown trailing)
4. **ML Exit** → `learner.should_exit_early()` (edge decay)
5. **Time Exit** → `TIME_EXIT_WEAK` (held > 300s, max_pnl < 100)
6. **Final Price** → Simulated close minus 0.5 slip
7. **Cost** → `cost_model.round_trip_cost(qty)` = lots × 66
8. **Net PnL** → `gross_pnl - cost`

## Live Sizing Path

1. `lot_size = config.LOT_SIZE` (default 30)
2. `qty = lot_size × LOTS_PER_TRADE` (live = 1)
3. Invariant: `qty % 30 == 0`

## Usage

```bash
python research/backtest/engine/run_research.py --start 2026-07-01 --end 2026-07-31
```# Trigger CI
