# Phase 1 - Root Cause Validation

Status: evidence gathered from existing code and datasets. Production models and engine were not modified.

## Executive Finding

The June 19 model stack did not fail because CatBoost was broken, features were misordered, or thresholds were applied incorrectly.

The root issue is target mismatch:

- June 18 models produce directional entry probabilities that the live engine expects.
- June 19 models produce rare profitable-after-cost event probabilities.
- The live engine consumed the June 19 rare-event probability as if it were a directional entry probability.

Those probability spaces are incompatible.

## Evidence Summary

Dataset: `ml/models/training_dataset_v3.csv`

Rows:

```text
505,214 rows
date range: 2021-01-01 09:29:00 -> 2026-06-19 14:26:00
```

Labels:

```text
label_ce positives: 9,108   rate: 0.018028
label_pe positives: 11,738  rate: 0.023234
both positive: 0.000000
neither positive: 0.958738
```

The June 19 labels represent rare profitable-after-cost opportunities, not routine directional outcomes.

## Validation Items

### 1. Label Generation

`ml/dataset_builder_v3.py` labels a bar positive only if an option trade is net-positive after spread and costs within the lookahead window.

That target is useful for trade quality, but it should not be the target for a directional model. A directional model must answer:

```text
P(favorable spot move | current market state)
```

The trade-quality model should answer:

```text
E(net PnL | direction, entry, costs, liquidity, volatility, time)
```

### 2. Feature Engineering

The production feature set has 35 canonical features. Model feature order was validated:

```text
June 18 LGBM: canonical order true
June 19 LGBM: canonical order true
CatBoost: canonical order true
```

Feature ordering is not the root cause.

### 3. Feature Leakage

No direct future features were found in the model feature list. However, V2 must protect against target leakage by construction:

- All rolling features must use only bars available at decision time.
- Future MFE/MAE, net PnL, target hit, and stop hit must exist only as labels.
- Calibration and thresholds must be fit only on training/calibration windows, never on test windows.

### 4. Class Imbalance

The June 19 cost-aware labels are extremely imbalanced:

```text
CE positive rate: 1.80%
PE positive rate: 2.32%
```

This explains near-zero predicted probabilities on most live bars. A calibrated rare-event model should output near zero most of the time.

### 5. Cost Modelling

Cost modelling belongs in the trade-quality layer. It should not be baked into the directional label.

Costs to model separately:

- Brokerage and statutory costs
- Bid/ask spread
- Slippage
- Option theta decay
- Premium convexity and delta response
- Liquidity and order-book depth

### 6. Calibration

June 19 model calibration is coherent for its rare-event target. On sampled V3 data:

```text
June19 CE AUC: 0.9528  AP: 0.3394  Brier: 0.014708
June19 PE AUC: 0.9353  AP: 0.3273  Brier: 0.020622
```

The issue is not that calibration is mathematically broken. The issue is that the calibrated probability is the wrong probability for the live engine.

### 7. Threshold Optimisation

June 19 thresholds around 0.79 are thresholds for rare profitable-after-cost events. Applying them as live directional thresholds is invalid.

V2 rule:

- Directional thresholds are evaluated on directional labels.
- Trade-quality thresholds are evaluated on expected net PnL and risk metrics.
- No threshold may be selected on test data.

### 8. Regime Dependence

Positive label rates differ materially by regime proxy:

```text
mixed           CE 0.0116  PE 0.0175
range           CE 0.0115  PE 0.0139
trend           CE 0.0132  PE 0.0201
volatile_trend  CE 0.0386  PE 0.0450
```

V2 should use a regime model and regime-conditioned validation.

### 9. Time-of-Day Behaviour

The V3 labeler creates zero-positive zones during inactive windows:

```text
11:00 hour: CE 0.0000  PE 0.0000
12:00 hour: CE 0.0000  PE 0.0000
13:00 hour: CE 0.0000  PE 0.0000
```

This should be handled by a trade filter or trade-quality model, not by the directional model.

### 10. Expiry-Day Behaviour

Current `time_to_expiry_min` equals minutes to market close, not actual contract days-to-expiry. V2 must add true expiry features:

- days to expiry
- minutes to expiry close
- weekly/monthly expiry flag
- expiry-day flag
- post-expiry-roll regime

### 11. Market Trend vs Range

Directional filters alone are insufficient. Direction-filter agreement was:

```text
CE direction-ok coverage: 0.3688  CE label rate: 0.0158
PE direction-ok coverage: 0.3254  PE label rate: 0.0308
```

V2 should model trend/range/volatile/mean-reverting/breakout as a first-stage context, not as hard-coded entry logic only.

### 12. Live vs Training Distribution Drift

Known drift risk found:

```text
training momentum_velocity = diff of returns
live momentum_velocity = price-point acceleration
```

This is a numerical feature mismatch. V2 must enforce a single shared feature builder for train, validation, backtest, and live candidate scoring.

### 13. CE/PE Asymmetry

PE cost-aware positives are consistently more frequent than CE:

```text
CE: 1.80%
PE: 2.32%
```

V2 should keep CE and PE as separate heads or separate models, with side-specific calibration and quality models.

### 14. Probability Calibration Quality

Calibration must be audited with:

- Brier score
- expected calibration error
- reliability bins
- calibration drift by time period
- calibration drift by regime
- calibration drift by CE/PE side

V2 should reject any model whose score is high but calibration is unreliable.

### 15. Ensemble Weighting

Fixed 50/50 averaging is too blunt. Evidence showed CatBoost is not globally broken, but its probability space matched the June 19 rare-event target, not June 18 directional target.

V2 ensemble rules:

- Ensemble only models trained on the same target.
- Weight models by out-of-sample calibration and profit contribution.
- Consider regime-specific dynamic weights.

### 16. Model Confidence Reliability

Confidence must be evaluated by what happens after a prediction, not only by predicted probability magnitude.

Required reliability checks:

- Precision by probability bucket
- Net PnL by probability bucket
- Drawdown by probability bucket
- Stability of bucket outcomes across walk-forward folds
- Regime-specific confidence decay

## Root Cause

The June 19 stack is a good candidate for a trade-quality filter, not a replacement for the directional model. It was consumed by the live engine as if it were a directional model, which caused near-zero live probabilities and stopped entries.

## Phase 1 Recommendation

Build V2 as a multi-stage pipeline:

1. Directional model: uncosted market direction target.
2. Trade-quality model: cost-aware expected trade outcomes.
3. Filter/sizing/exit layers: risk and execution decisions.

Do not merge rare-event cost-aware probabilities with directional probabilities.

