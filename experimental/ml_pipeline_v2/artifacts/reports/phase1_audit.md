# Pipeline V2 Phase 1 Audit Output

## Label Summary

- Rows: 505,214
- Date range: 2021-01-01 09:29:00 to 2026-06-19 14:26:00
- CE positive rate: 0.018028
- PE positive rate: 0.023234
- Both positive rate: 0.000000
- Neither positive rate: 0.958738
- CE eligible rate: 0.439764
- PE eligible rate: 0.439764

## Static Feature Parity Warnings

- momentum_velocity currently differs between training and live: training uses diff(returns), live uses point acceleration.
- time_to_expiry_min currently equals minutes to market close, not true contract time to expiry.
- volume_ratio is always 1.0 for zero-volume index history; live option liquidity needs separate features.

## Feature Ranges

- momentum_velocity: min=-0.038527775, p01=-0.0013466195, p50=-2.2432418e-06, p99=0.0013851604, max=0.040622758
- range_compression: min=0, p01=0.19512195, p50=0.55719645, p99=0.99999997, max=1
- price_vs_vwap: min=-0.049916435, p01=-0.0075577604, p50=6.267898e-05, p99=0.0069736449, max=0.026624388
- adx: min=2.881517, p01=9.8401315, p50=22.849795, p99=57.032356, max=82.911815
- di_spread: min=-60, p01=-37.998231, p50=0.22596391, p99=38.451791, max=60
- returns: min=-0.038645604, p01=-0.00095667461, p50=0, p99=0.00095850375, max=0.030901494
- return_3: min=-0.041063545, p01=-0.0017680884, p50=5.8191693e-06, p99=0.0017966703, max=0.03328801
- volatility: min=0, p01=9.0473182e-05, p50=0.00025844161, p99=0.0014981022, max=0.0090952255
- atr: min=2.428889, p01=3.3635181, p50=7.3387468, p99=22.609113, max=99.800708
- time_to_expiry_min: min=0, p01=4, p50=188, p99=372, max=375
- moneyness: min=-0.02, p01=-0.0024822194, p50=2.8331032e-05, p99=0.0024934531, max=0.02

## Bucket Data

Detailed bucket data is written to the companion JSON report.
