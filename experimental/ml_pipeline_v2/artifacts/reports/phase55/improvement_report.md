# Phase 5.5 Improvement Report

Generated from existing Phase 5 offline artifacts only.

## Baseline

- Trades: 66692
- Net PnL: -2768119.12
- Profit Factor: 0.7322
- Win Rate: 39.70%
- Expectancy: -41.51
- Max Drawdown: -2768261.25

## Top Single Recommendation Tests

- Research none without confidence threshold, reward 22.50, stop 30.00, risk x1.25, trailing=True, partial=0.00: trades=66692, PF=3.6668, expectancy=153.09, net PnL=10209802.88, accepted=True
- Research none without confidence threshold, reward 22.50, stop 22.50, risk x1.25, trailing=True, partial=0.00: trades=66692, PF=3.6286, expectancy=150.89, net PnL=10063439.81, accepted=True
- Research none without confidence threshold, reward 18.75, stop 30.00, risk x1.25, trailing=True, partial=0.00: trades=66692, PF=3.6076, expectancy=149.69, net PnL=9983054.25, accepted=True
- Research none without confidence threshold, reward 18.75, stop 22.50, risk x1.25, trailing=True, partial=0.00: trades=66692, PF=3.5620, expectancy=147.08, net PnL=9808758.75, accepted=True
- Research none without confidence threshold, reward 22.50, stop 15.00, risk x1.25, trailing=True, partial=0.00: trades=66692, PF=3.5531, expectancy=146.56, net PnL=9774360.38, accepted=True
- all require quality confidence >= 0.4358: trades=18447, PF=1.1900, expectancy=28.80, net PnL=531238.50, accepted=True
- all require directional confidence >= 0.4645: trades=16042, PF=1.1651, expectancy=26.44, net PnL=424159.88, accepted=True
- ce require quality confidence >= 0.4358: trades=44077, PF=0.8716, expectancy=-19.60, net PnL=-863868.00, accepted=False
- pe require directional confidence >= 0.4607: trades=47703, PF=0.8199, expectancy=-27.94, net PnL=-1332595.87, accepted=False
- pe require directional confidence >= 0.4468: trades=50192, PF=0.8165, expectancy=-28.50, net PnL=-1430691.00, accepted=False

## Top Combinations

- ce require quality confidence >= 0.4358 | pe require directional confidence >= 0.4645 | Reduce or disable ce trades in mixed: trades=15160, PF=1.2622, expectancy=39.55, net PnL=599552.63, coverage=22.73%
- ce require quality confidence >= 0.4358 | pe require directional confidence >= 0.4645 | Reduce or disable all trades in range: trades=14669, PF=1.2507, expectancy=38.11, net PnL=559068.00, coverage=22.00%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358 | Reduce or disable ce trades in Low Volatility: trades=13840, PF=1.2475, expectancy=38.93, net PnL=538801.13, coverage=20.75%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358: trades=13854, PF=1.2473, expectancy=38.90, net PnL=538908.38, coverage=20.77%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358 | pe require directional confidence >= 0.4645: trades=13854, PF=1.2473, expectancy=38.90, net PnL=538908.38, coverage=20.77%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358 | pe require directional confidence >= 0.4607: trades=13854, PF=1.2473, expectancy=38.90, net PnL=538908.38, coverage=20.77%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358 | pe require directional confidence >= 0.4468: trades=13854, PF=1.2473, expectancy=38.90, net PnL=538908.38, coverage=20.77%
- all require quality confidence >= 0.4358 | pe require directional confidence >= 0.4607 | Reduce or disable ce trades in mixed: trades=14776, PF=1.2440, expectancy=36.66, net PnL=541651.50, coverage=22.16%
- all require quality confidence >= 0.4358 | pe require directional confidence >= 0.4468 | Reduce or disable ce trades in mixed: trades=14972, PF=1.2403, expectancy=36.07, net PnL=540092.25, coverage=22.45%
- all require directional confidence >= 0.4645 | ce require quality confidence >= 0.4358 | Reduce or disable all trades in Low Volatility: trades=13565, PF=1.2349, expectancy=37.39, net PnL=507141.75, coverage=20.34%

## Final Recommendation

- Source: combination_optimizer
- Expected PF: 1.2622
- Expected expectancy: 39.55
- Expected net PnL: 599552.63
- Confidence: high
