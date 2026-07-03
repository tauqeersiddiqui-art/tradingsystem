# ML Pipeline V2 Architecture

## Design Principles

1. Separate probability spaces.
2. Use one canonical feature builder for train, validation, backtest, and live scoring.
3. Validate each model stage independently before composing stages.
4. Prefer calibrated, stable, lower-variance models over models with one-off high win rate.
5. Optimize long-term expectancy, drawdown control, and robustness, not raw win rate.
6. Never select thresholds on final test data.

## Stage 1 - Market Regime Model

Purpose: classify the market state before directional or trade-quality decisions.

Outputs:

- `P(trending)`
- `P(range)`
- `P(volatile)`
- `P(mean_reverting)`
- `P(breakout)`

Inputs:

- multi-timeframe returns
- realized volatility
- ADX and DI spread
- VWAP distance
- opening range expansion
- range compression
- ATR percentile
- session time
- true expiry features

Recommended first model:

- LightGBM multiclass classifier
- calibrated with time-split isotonic or Platt, selected by calibration fold

Validation:

- macro F1
- balanced accuracy
- regime stability by year
- downstream PnL contribution when used as context

## Stage 2 - Directional Model

Purpose: predict market direction, not profitability after costs.

Outputs:

- `P(CE directional success)`
- `P(PE directional success)`

Target:

- CE success: favorable upward spot move within lookahead, with bounded adverse excursion.
- PE success: favorable downward spot move within lookahead, with bounded adverse excursion.
- No transaction costs in this stage.

Inputs:

- price and momentum features
- trend and VWAP features
- multi-timeframe confirmation
- regime probabilities from Stage 1
- time-of-day features

Recommended first model:

- side-specific LGBM classifiers
- CatBoost challenger
- no fixed 50/50 ensemble until both models prove calibrated on the same target

Validation:

- AUC
- average precision
- Brier score
- expected calibration error
- precision/recall by probability bucket
- directional hit rate by regime and time bucket

## Stage 3 - Trade Quality Model

Purpose: estimate whether a directionally valid setup is worth trading.

Outputs:

- expected net PnL
- expected reward/risk
- expected holding time
- expected slippage
- expected drawdown
- probability of stop loss
- probability of target hit

Target:

- Uses realistic costs, spread, slippage, premium response, and exits.
- Uses the direction model output as an input feature, not as a label.

Recommended first models:

- LGBM regressors for expected net PnL, drawdown, holding time
- LGBM classifiers for stop-hit and target-hit probabilities
- quantile regressors for downside tail estimates

Validation:

- MAE/RMSE for continuous targets
- calibration for stop/target probabilities
- realized expectancy by predicted expectancy bucket
- profit factor by bucket
- drawdown by bucket

## Stage 4 - Position Sizing Model

Purpose: translate opportunity quality into controlled risk.

Outputs:

- lot size
- risk percent
- maximum exposure
- confidence weight

Initial implementation:

- deterministic risk policy with ML inputs
- no unconstrained model until enough live samples exist

Formula constraints:

- lot size must be bounded by account risk
- confidence cannot override daily loss limits
- exposure must be reduced in high drawdown or high slippage regimes

Validation:

- risk of ruin
- max drawdown
- daily loss distribution
- Monte Carlo reorder tests
- expected shortfall

## Stage 5 - Trade Filter

Purpose: reject trades that are statistically or operationally weak.

Reject when:

- low liquidity
- high spread
- weak trend
- conflicting higher timeframe
- weak momentum
- poor expected net PnL
- low model confidence
- unstable calibration regime
- post-drift alert active

This stage should be conservative and explainable.

## Stage 6 - Exit Model

Purpose: manage the trade after entry.

Outputs:

- best exit action
- trailing stop update
- partial exit signal
- time exit signal
- target extension signal
- reversal probability

Targets:

- max favorable excursion
- max adverse excursion
- probability of reversal in next N bars
- incremental expected PnL of holding vs exiting

Validation:

- retained MFE percent
- profitable-then-lost rate
- exit slippage
- average giveback
- realized PnL vs hold-to-target baseline

## Ensemble Research

Candidate models:

- LightGBM
- CatBoost
- XGBoost
- Random Forest
- neural network / MLP
- stacking
- weighted ensemble
- regime-dynamic ensemble

Rules:

- Ensemble only models trained on the same target.
- Use out-of-fold predictions for stacking.
- Fit ensemble weights only on calibration folds.
- Measure both calibration and PnL contribution.
- Reject models that improve AUC but worsen Brier score or drawdown.

Recommended initial production architecture after validation:

```text
Stage 1: LGBM regime model
Stage 2: Side-specific LGBM directional models, CatBoost challenger
Stage 3: Side-specific LGBM trade-quality regressors/classifiers
Stage 4: deterministic constrained sizing
Stage 5: rules plus calibrated expected-value gate
Stage 6: initially rule-based exit with ML exit challenger in shadow mode
```

## Migration Strategy

1. Keep June 18 production models active.
2. Train V2 in isolation.
3. Run V2 shadow scoring during live sessions without affecting trades.
4. Compare V2 decisions to production decisions.
5. Require stable walk-forward and live-shadow evidence.
6. Promote only one stage at a time.
7. Keep rollback to June 18 model artifacts.

## Expected Improvements

Expected improvements must be proven, not assumed:

- fewer incompatible probability-space failures
- better calibrated entry probabilities
- lower false positives during inactive or range periods
- better drawdown control
- better CE/PE side-specific behavior
- explainable trade rejection reasons
- drift detection before model degradation becomes trading loss

## Key Risks

- Too few positive trade-quality samples.
- Regime labels may be noisy.
- Option price simulator may not match live fills.
- Live feature drift may invalidate historical validation.
- Dynamic ensembles can overfit.
- Position sizing models can amplify errors.
- Exit models require enough completed trade samples.

