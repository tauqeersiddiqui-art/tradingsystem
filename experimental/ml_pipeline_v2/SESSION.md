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

## 2026-07-06 - Phase 6 Normal ML Rescue Engine

Scope controls:

- Worked only inside `experimental/ml_pipeline_v2`.
- Did not modify production trading code.
- Did not modify the live engine.
- Did not modify broker integration.
- Did not modify execution logic.
- Did not modify Phase 5.5.
- Did not retrain models.
- Reused Phase 5 completed-trade artifacts as the sole input.

Implemented:

- Added `src/ml_pipeline_v2/phase6.py` (7 analysis modules).
- Added `scripts/run_phase6_ml_rescue.py` (CLI runner).

Phase 6 milestone: 7-module deep analysis focused exclusively on the Normal ML engine.

Module 1 — Entry Quality Engine:
- Computes a direction-aware Entry Quality Score (0-100) per trade using ADX (15 pts),
  DI spread (15 pts), SuperTrend direction (15 pts), EMA alignment (10 pts),
  VWAP position (10 pts), RSI zone (10 pts), trend strength (8 pts),
  range compression (5 pts), volatility (5 pts), momentum velocity (5 pts),
  and market regime (2 pts).
- Measures score separation between winners and losers.
- Tests score thresholds (30/40/50/60/70) for PF impact.
- Generates: `entry_quality_report.json`, `entry_quality_scores.csv`.

Module 2 — Trade Capture Engine:
- Computes capture %, giveback %, MFE/MAE per trade.
- Simulates 10 alternative exit strategies: actual, early_50pct_mfe, early_75pct_mfe,
  trailing_60pct, trailing_80pct, atr_trailing, breakeven_stop, time_exit_6bars,
  time_exit_8bars, partial_50pct_at_target.
- Ranks exit strategies by profit factor.
- Generates: `trade_capture_report.json`, `exit_strategy_ranking.csv`, `capture_metrics.csv`.

Module 3 — Confidence Calibration:
- Computes Expected Calibration Error (ECE) from reliability curve.
- Measures win rate and PF by confidence bucket.
- Optimises confidence thresholds from 0.28 to 0.80 for expectancy and PF.
- Generates per-side CE/PE calibration breakdown.
- Generates: `confidence_calibration_report.json`, `threshold_optimization.csv`, `bucket_stats.csv`.

Module 4 — Range Market Intelligence:
- Compares range vs non-range performance across all metrics.
- Analyses indicator differences between range winners and range losers.
- Generates time-of-day and direction breakdown for range trades.
- Identifies 4 skip/delay/confirm conditions (low confidence, low ADX, against-VWAP,
  SuperTrend contradiction).
- Generates: `range_market_report.json`, `indicator_comparison.csv`.

Module 5 — Position Size Analysis:
- Measures top-10 loss concentration.
- Computes Kelly fraction and half-Kelly from historical outcomes.
- Simulates 4 confidence-tiered sizing strategies.
- Generates loss breakdown by regime and confidence bucket.
- Generates: `position_size_report.json`, `tiered_sizing.csv`, `loss_by_regime.csv`.

Module 6 — Loss Clustering:
- K-Means (k=6) on 13 numeric features + regime dummies + side encoding.
- Auto-labels each cluster (low-confidence, weak-trend, range-market, high-volatility).
- Ranks clusters by total loss.
- Generates per-cluster recommended action (skip_entry / reduce_size / investigate).
- Generates: `loss_clusters.json`, `loss_clusters.csv`.

Module 7 — ML Improvement Engine:
- Synthesises evidence from all 6 modules into structured recommendations.
- Each recommendation includes: evidence, expected PF improvement, expected expectancy
  improvement, expected trade reduction, statistical confidence, and risk.
- Recommendations ranked by expected PF improvement.
- Generates: `ml_improvement_plan.json`.

Validation executed:

```powershell
python -m py_compile experimental\ml_pipeline_v2\src\ml_pipeline_v2\phase6.py experimental\ml_pipeline_v2\scripts\run_phase6_ml_rescue.py
python experimental\ml_pipeline_v2\scripts\run_phase6_ml_rescue.py --min-trades 200
```

Run summary:

- Input trades (Phase 5 completed): 66,692
- Baseline profit factor: 0.7322
- Baseline win rate: 39.70%
- Baseline expectancy: -41.51
- Baseline total PnL: -2,768,119.12
- Modules completed: 7 / 7

Module 1 — Entry Quality Score:
- Winner score mean: 55.21  |  Loser score mean: 55.50  |  Separation: −0.29
- Entry quality score is NOT predictive — winners and losers score virtually identically.
- Best threshold (30/100) retains 85.9% of trades → PF=0.735 (minimal improvement).
- Finding: Entry selection is not the core problem; the engine enters reasonable trades.

Module 2 — Trade Capture:
- avg_capture_pct: −12.3%  |  avg_giveback_pct: 122.8%
- The system gives back 122% of its favorable move on average → exits too late.
- Best exit simulation: early_75pct_mfe → PF=7.84, avg_pnl=+₹229.5
- Actual exit strategy ranks last. This is the dominant failure mode.

Module 3 — Confidence Calibration:
- ECE: 0.0345 (model is moderately well-calibrated)
- Optimal threshold: 0.46 → PF=1.486, avg_pnl=+₹67.2
- Retains only 7.1% of trades (4,711). High filter but meaningful edge.

Module 4 — Range Market Intelligence:
- Range win rate: 39.1%  |  Non-range win rate: 39.9%  |  Gap: 0.84 pp
- Range is NOT a primary failure driver; the difference is negligible.
- 4 skip/delay conditions identified as secondary refinements.

Module 5 — Position Size Analysis:
- Kelly fraction: −0.145 (negative edge — no mathematically justified position size)
- Top-10 loss concentration: 0.185% (losses are spread, not concentrated)
- Tiered sizing best PF: 0.738 — marginal improvement; sizing cannot rescue a broken exit.

Module 6 — Loss Clustering:
- 40,214 losing trades → 6 clusters
- Worst cluster: "low-confidence" (26.3% of losses, ₹2,926,557)
  - avg_conf=0.41, avg_adx=23.1, dominant regime=mixed, dominant side=PE
  - Recommended action: skip_entry

Module 7 — ML Improvement Plan (6 recommendations, ranked by PF delta):
- REC_TC_01: Adopt early_75pct_mfe exit  →  ΔPF=+7.11  (HIGH confidence)
- REC_CC_01: Confidence threshold >= 0.46  →  ΔPF=+0.75  (MODERATE confidence)
- REC_RM_01: Regime-gate RANGE trades  →  ΔPF=+0.05  (MODERATE confidence)
- REC_LC_01: Skip low-confidence cluster  →  ΔPF=+0.03  (MODERATE confidence)
- REC_EQ_01: Entry quality score >= 30  →  ΔPF=+0.003  (negligible)
- REC_PS_01: Reduce position size (Kelly < 0)  →  ΔPF=0.0  (HIGH confidence)

Key conclusion:
The Normal ML engine's dominant failure is exit timing, not entry quality.
avg_giveback_pct=122.8% means trades enter at the right time but hold too long and reverse.
Fixing the exit (trailing stop, earlier exit at MFE fraction) is the highest-leverage intervention.
Entry Quality Score provides no discrimination (0.29 pt separation); the entry signal works.

Run command:

```powershell
python experimental\ml_pipeline_v2\scripts\run_phase6_ml_rescue.py --min-trades 200
```

Reports written under:

- `artifacts/reports/phase6/phase6_summary.json`
- `artifacts/reports/phase6/entry_quality/entry_quality_report.json`
- `artifacts/reports/phase6/trade_capture/trade_capture_report.json`
- `artifacts/reports/phase6/confidence_calibration/confidence_calibration_report.json`
- `artifacts/reports/phase6/range_market/range_market_report.json`
- `artifacts/reports/phase6/position_size/position_size_report.json`
- `artifacts/reports/phase6/loss_clusters/loss_clusters.json`
- `artifacts/reports/phase6/improvement/ml_improvement_plan.json`

Caveats:

- Phase 6 is research-only and does not integrate with the live engine.
- Entry Quality Score is a heuristic computed from available offline features;
  it has not been validated against live trade fills.
- Exit strategy simulations use MFE/MAE summaries, not tick-level path ordering.
- Loss clusters are unsupervised — validate each cluster manually before applying rules.
- Kelly fraction and sizing simulations assume stationarity of the historical distribution.

## 2026-07-07 - Phase 7 Adaptive Exit Intelligence

Scope controls:

- Worked only inside `experimental/ml_pipeline_v2`.
- Did not modify production trading code.
- Did not modify the live engine.
- Did not modify broker integration.
- Did not modify execution logic.
- Did not modify risk management.
- Did not modify Phase 5.5.
- Did not retrain models.
- Reused Phase 5 completed-trade artifacts and raw training_dataset_v3.csv as inputs.

Implemented:

- Added `src/ml_pipeline_v2/phase7.py` (6 modules).
- Added `scripts/run_phase7_adaptive_exit.py` (CLI runner).

Phase 7 objective:
Design live-feasible exit strategies that approach the Phase 6 offline benchmark (PF 7.84
via early_75pct_mfe) WITHOUT using future information. MFE is only known after trade
completion and cannot be used for live exits. All 16 strategies use only current-bar and
past-bar indicator values.

Module 1 — Live Exit Strategy Library (STRATEGY_META catalogue):
- 16 exit strategies defined with descriptions, live_feasibility flags, params, and
  implementation notes. Strategies: atr_trail_1.5/2.0/2.5, chandelier_1.0/1.5,
  supertrend_exit, ema20_exit, vwap_exit, be_trail_1.0/1.5, partial_runner,
  time_6/8/10bars, vol_adaptive, momentum_exhaust.
- "actual" (Phase 5 baseline) is flagged uses_future=True for reference only.

Module 2 — Exit Simulation Engine (bar-by-bar replay on raw dataset):
- Aligns all Phase 5 completed trades to raw training_dataset_v3.csv by date.
- Extracts vectorised bar windows (n_trades × 12 bars) for close/high/low/ATR/indicators.
- Simulates each of the 16 live strategies using only current/past bar data.
- ATR Trail: stop trails only in trade direction; updated each bar.
- Chandelier: stop = rolling_max_high(window) − mult × ATR; anchored to highest high since entry.
- BE+Trail: two-phase — wide initial stop → move to entry once +8 pts unrealized → trail from there.
- Partial Runner: first 50% exits at 15pt target; remainder trails with 2.0× ATR stop; PnL
  computed as 50/50 blended exit price.
- All strategies compute PnL as (exit_price − entry) × 30 lots − ₹132 brokerage.
- Generates: `exit_strategy_comparison.json`, `exit_strategy_comparison.csv`,
  `best_strategy_per_trade.csv`.

Module 3 — Profit Lock Engine:
- Tests 11 profit-lock mechanisms: ATR trail (0.5x–2.5x), volatility-adaptive
  (1.5x/2.0x base), break-even variants (BE8/trail1.0, BE8/trail1.5, BE10/trail1.0).
- Measures profit_factor, avg_pnl, avg_mfe_capture_pct, avg_exit_bar per method.
- Generates: `profit_lock_report.json`, `profit_lock_comparison.csv`.

Module 4 — Exit Quality Score (0-100, per-bar, live-feasible):
- Computes EQS per trade at bars 3, 6, 9 from 6 components:
  profit_captured (0-25), trend_health_adx (0-20), momentum_velocity (0-20),
  supertrend_alignment (0-15), time_pressure (0-10), rsi_health (0-10).
- Recommends: Hold (>=70), Trail (50-70), ScaleOut (30-50), Exit (<30).
- Computes action_analysis: for each recommended action at each check bar,
  measures actual PnL outcomes to validate signal quality.
- Generates: `exit_quality_report.json`, `eqs_samples.csv`.

Module 5 — Live Feasibility Check:
- Explicitly verifies every strategy uses only real-time available inputs.
- Lists accepted inputs: close, high, low, ATR, SuperTrend, EMA20, VWAP, momentum,
  RSI, volatility (all from current/past bars).
- Flags "actual" baseline as FAIL — uses MFE/MAE lookahead data.
- All 16 simulated strategies receive PASS verdict.
- Generates: `live_exit_validation.json`, `feasibility_summary.csv`.

Module 6 — Exit Recommendation Engine:
- Ranks all live-feasible strategies by expected PF improvement over baseline.
- Each recommendation includes: profit_factor, pf_delta, expectancy, win_rate,
  avg_exit_bar, avg_mfe_capture_pct, implementation_complexity, risk notes.
- Generates: `recommended_exit_strategy.json`, `exit_recommendations.csv`.

Key design decisions:
- Entry price = close of entry bar (consistent with Phase 5 convention).
- Stop hit detection: CE → low ≤ stop; PE → high ≥ stop.
- Vectorised window extraction: entry_indices[:, None] + offsets[None, :] for O(1) window lookup.
- Only trades with entry_idx + 12 <= len(raw) included (boundary-safe).
- Forward-fill NaN along bar axis before simulation.

Run command:

```powershell
python -m py_compile experimental\ml_pipeline_v2\src\ml_pipeline_v2\phase7.py experimental\ml_pipeline_v2\scripts\run_phase7_adaptive_exit.py
python experimental\ml_pipeline_v2\scripts\run_phase7_adaptive_exit.py
```

Reports written under:

- `artifacts/reports/phase7/phase7_summary.json`
- `artifacts/reports/phase7/simulation/exit_strategy_comparison.json`
- `artifacts/reports/phase7/simulation/exit_strategy_comparison.csv`
- `artifacts/reports/phase7/simulation/best_strategy_per_trade.csv`
- `artifacts/reports/phase7/profit_lock/profit_lock_report.json`
- `artifacts/reports/phase7/profit_lock/profit_lock_comparison.csv`
- `artifacts/reports/phase7/exit_quality/exit_quality_report.json`
- `artifacts/reports/phase7/exit_quality/eqs_samples.csv`
- `artifacts/reports/phase7/feasibility/live_exit_validation.json`
- `artifacts/reports/phase7/feasibility/feasibility_summary.csv`
- `artifacts/reports/phase7/recommendations/recommended_exit_strategy.json`
- `artifacts/reports/phase7/recommendations/exit_recommendations.csv`

Run results: (populate after executing run command above)

Caveats:

- Phase 7 is research-only and does not integrate with the live engine.
- Bar-by-bar simulation uses 1-minute OHLCV close as entry price; actual fill price
  may differ due to slippage.
- Stop hit detection uses intrabar high/low as proxies; tick-level path ordering not available.
- Partial runner requires position-size management in execution layer (not modelled here).
- Phase 6 benchmark (PF 7.84, early_75pct_mfe) uses future MFE — NOT achievable live.
  Live-feasible strategies will produce lower PF but are genuinely tradeable.
