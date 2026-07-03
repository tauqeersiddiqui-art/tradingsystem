# Session Notes

## 2026-07-04 - Phase 5 Profitability Intelligence

Scope controls:

- Worked only inside `experimental/ml_pipeline_v2`.
- Did not modify production trading code.
- Did not integrate with the live engine.
- Did not retrain models.
- Reused existing Pipeline V2 dataset/model/report artifacts.

Implemented:

- Added `src/ml_pipeline_v2/phase5.py`.
- Added `scripts/run_phase5_profitability.py`.
- Exported `phase5` from `src/ml_pipeline_v2/__init__.py`.

Phase 5 milestone completed:

- Reconstructed offline completed-trade outcomes from existing V2 labels on the held-out test split.
- Loaded existing Phase 4/V2 model artifacts for directional, quality, target-hit, stop-hit, and regime confidence scores.
- Generated loss analysis for every losing reconstructed trade.
- Tested profitability filters for confidence, trend strength, RSI, ATR, time, regime, and volatility.
- Generated evidence-gated filter recommendations using approximate Welch 95% expectancy-lift confidence intervals.
- Generated per-trade AI-style review rows for every completed trade in the analyzed split.
- Generated regime performance matrices for market regime, trending/range, high/low volatility, gap-opening proxy, and expiry proxy.
- Generated counterfactual strategy optimizer reports for threshold, risk, reward, stop, trailing stop, partial exit, and entry-filter combinations.
- Generated recommendation reports marked research-only.

Validation executed:

```powershell
python -m py_compile experimental\ml_pipeline_v2\src\ml_pipeline_v2\phase5.py experimental\ml_pipeline_v2\scripts\run_phase5_profitability.py
python experimental\ml_pipeline_v2\scripts\run_phase5_profitability.py --split test --min-trades 200
```

Run summary:

- Dataset: `ml/models/training_dataset_v3.csv`
- Split: held-out test split
- Selected rows: 75,781
- Reconstructed completed trades: 66,692
- Losing trades: 40,214
- Baseline net PnL: -2,768,119.12
- Baseline profit factor: 0.7322
- Baseline win rate: 39.70%
- Baseline expectancy: -41.51
- Recommended filters: 7
- Total recommendations: 18

Key evidence:

- Largest loss pattern: target missed / target not reached in holding window.
- Low-confidence trades account for the largest confidence-bucket loss concentration.
- Evidence-backed filters include minimum quality confidence and PE directional confidence thresholds.
- Weak segments include mixed regime, low volatility, and range conditions.

Reports written under:

- `artifacts/reports/phase5/phase5_summary.json`
- `artifacts/reports/phase5/trades/completed_trades.csv`
- `artifacts/reports/phase5/loss_analysis/`
- `artifacts/reports/phase5/optimizer/`
- `artifacts/reports/phase5/trade_reviews/`
- `artifacts/reports/phase5/strategy_optimizer/`
- `artifacts/reports/phase5/regime_matrix/`
- `artifacts/reports/phase5/recommendations/`

Caveat:

- Phase 5 currently analyzes V2 offline reconstructed trade outcomes, not live-engine trade logs. This is intentional for the current research-only milestone.
