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

## 2026-07-05 - Phase 5.5 Autonomous Strategy Improvement Engine

Scope controls:

- Worked only inside `experimental/ml_pipeline_v2`.
- Did not modify production trading code.
- Did not integrate with the live engine.
- Did not retrain models.
- Reused existing Phase 2, 3, 4, and 5 artifacts, especially the Phase 5 completed-trade reconstruction and recommendation reports.

Implemented:

- Added `src/ml_pipeline_v2/phase55.py`.
- Added `scripts/run_phase55_strategy_improvement.py`.
- Exported `phase55` from `src/ml_pipeline_v2/__init__.py`.

Phase 5.5 milestone completed:

- Validated every Phase 5 recommendation as a single applied recommendation against the 66,692 reconstructed trades.
- Tested recommendation combinations up to 3 filters with trade-count, profitability, significance, and chronological stability gates.
- Built counterfactual simulations for all 40,214 losing trades covering confidence thresholds, stop/target changes, trailing stops, partial exits, skip logic, and regime filters.
- Built replay intelligence for every reconstructed trade with entry reason, exit reason, SHAP-style feature explanation, regime, confidence, risk, counterfactual outcome, and skip decision.
- Expanded filter ranking across confidence, ADX, ATR, RSI, EMA distance, VWAP distance, time, day, regime, and volatility filters.
- Generated a final evidence-gated `recommended_strategy.json`.

Validation executed:

```powershell
python -m py_compile experimental\ml_pipeline_v2\src\ml_pipeline_v2\phase55.py experimental\ml_pipeline_v2\scripts\run_phase55_strategy_improvement.py
python experimental\ml_pipeline_v2\scripts\run_phase55_strategy_improvement.py --min-trades 200 --min-trade-coverage 0.20 --max-combination-size 3
```

Run summary:

- Baseline reconstructed trades: 66,692
- Baseline losing trades: 40,214
- Baseline net PnL: -2,768,119.12
- Baseline profit factor: 0.7322
- Baseline expectancy: -41.51
- Single recommendation tests: 18
- Accepted single tests: 7
- Ranked accepted combinations: 126
- Losing trade counterfactuals: 40,214
- Replay records: 66,692
- Ranked filter tests: 167
- Accepted standalone filters: 5

Final recommended strategy:

- Apply CE quality confidence threshold: `ce require quality confidence >= 0.4358`
- Apply PE directional confidence threshold: `pe require directional confidence >= 0.4645`
- Reduce or disable CE trades in `mixed` regime

Expected offline improvement:

- Trade count: 15,160
- Trade coverage: 22.73%
- Net PnL: 599,552.63
- Profit factor: 1.2622
- Win rate: 52.20%
- Expectancy: 39.55
- Max drawdown: -94,716.00
- Expectancy lift CI low: 73.47
- Approx p-value: 2.11e-97
- Temporal stability: passed 4 / 4 chronological folds

Reports written under:

- `artifacts/reports/phase55/phase55_summary.json`
- `artifacts/reports/phase55/improvement_report.md`
- `artifacts/reports/phase55/single_recommendation_validation/`
- `artifacts/reports/phase55/combination_optimizer/`
- `artifacts/reports/phase55/counterfactual_trade_simulator/`
- `artifacts/reports/phase55/trade_replay_intelligence/`
- `artifacts/reports/phase55/filter_ranking/`
- `artifacts/reports/phase55/final_recommendation_engine/recommended_strategy.json`

Caveats:

- Phase 5.5 remains research-only and uses offline reconstructed V2 outcomes, not live fills.
- Counterfactual exit outcomes are estimated from MFE/MAE summaries, not full tick-level path ordering.
- The final recommendation is evidence-gated for offline profitability and temporal stability, but still requires paper-trading or shadow validation before any production use.
