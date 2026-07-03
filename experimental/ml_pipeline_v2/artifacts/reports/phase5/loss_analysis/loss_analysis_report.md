# Phase 5 Loss Analysis

This report is generated from experimental Pipeline V2 artifacts only.

## Baseline

- Trades: 66692
- Net PnL: -2768119.12
- Profit Factor: 0.7322
- Win Rate: 39.70%
- Expectancy: -41.51

## Losses

- Losing trades: 40214
- Loss rate: 60.30%
- Gross loss: 10335770.25

## Largest Loss Buckets

- target_missed=True: losses=37861, gross_loss=10110236.25, expectancy=-252.50
- holding_time_bucket=target_not_reached_in_window: losses=37861, gross_loss=10110236.25, expectancy=-252.50
- confidence_bucket=low_confidence: losses=36454, gross_loss=9193703.63, expectancy=-53.17
- side=ce: losses=20387, gross_loss=5308259.25, expectancy=-50.81
- stop_hit=0: losses=29268, gross_loss=5288877.75, expectancy=40.29
- volatility_bucket=normal_volatility: losses=21772, gross_loss=5270761.50, expectancy=-52.98
- primary_failure=target_missed_long_hold: losses=27692, gross_loss=5216698.12, expectancy=-188.38
- stop_hit=1: losses=10946, gross_loss=5046892.50, expectancy=-449.32
- primary_failure=stop_hit: losses=10946, gross_loss=5046892.50, expectancy=-461.07
- side=pe: losses=19827, gross_loss=5027511.00, expectancy=-32.21
- time_of_day=morning: losses=18257, gross_loss=4530264.75, expectancy=-47.22
- trend_strength_bucket=strong_trend: losses=17131, gross_loss=4428871.12, expectancy=-37.65
- market_regime=mixed: losses=15124, gross_loss=3763555.88, expectancy=-48.24
- volatility_bucket=high_volatility: losses=11231, gross_loss=3624071.63, expectancy=-0.45
- trend_strength_bucket=moderate_trend: losses=12208, gross_loss=3127135.12, expectancy=-43.82
