# Validation and Research Plan

## Validation Philosophy

Every stage must pass standalone validation before being composed into the final trading policy. A high AUC model is not enough. A deployable stage must be calibrated, stable across time, and beneficial to costed trade-level outcomes.

## Data Splits

Use time-aware validation only:

- expanding walk-forward
- rolling walk-forward
- purged splits around lookahead windows
- embargo after each training period
- final untouched out-of-sample period

No random cross-validation for deployment decisions.

## Metrics

Classification:

- AUC
- average precision
- precision/recall
- Brier score
- expected calibration error
- reliability bins

Trading:

- net PnL after costs
- expectancy per trade
- profit factor
- Sharpe ratio
- max drawdown
- win rate
- average winner
- average loser
- payoff ratio
- risk of ruin
- daily loss tail

Robustness:

- fold-to-fold metric variance
- regime-by-regime stability
- time-of-day stability
- CE/PE asymmetry
- live-vs-training PSI
- feature drift alerts

## Threshold Protocol

Thresholds are selected only on calibration folds. Final test folds are used once.

Threshold objective:

```text
maximize expected utility subject to:
  minimum sample size
  positive expectancy
  max drawdown constraint
  calibration constraint
  turnover constraint
```

Do not tune thresholds to recover a preferred trade count.

## Monte Carlo Simulation

Run Monte Carlo on trade-level returns:

- random trade order
- block bootstrap by day
- regime-stratified bootstrap
- slippage stress
- spread stress
- losing-streak stress

Outputs:

- probability of drawdown beyond limit
- risk of ruin
- expected worst 5% outcome
- expected daily loss tail

## Ensemble Research Protocol

Candidates:

- LGBM
- CatBoost
- XGBoost
- Random Forest
- MLP
- stacking
- weighted average
- regime-dynamic ensemble

Evidence required:

- same target and same feature window
- out-of-fold predictions
- calibration comparison
- statistical confidence intervals
- trade-level lift over baseline
- degradation test in adverse regimes

Reject ensemble if:

- it improves AUC but worsens calibration
- it improves average PnL but increases tail drawdown
- it depends on one regime or one year
- it only works after threshold over-tuning

## Deliverables

1. Architecture document.
2. Training pipeline.
3. Validation pipeline.
4. Feature importance analysis.
5. Label quality analysis.
6. Drift detection report.
7. Recommended production architecture.
8. Migration strategy.
9. Risk register.
10. Expected performance improvement estimate with confidence intervals.

