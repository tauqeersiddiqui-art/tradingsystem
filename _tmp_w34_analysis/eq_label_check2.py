import pandas as pd, numpy as np, sys
sys.path.insert(0, '.')
from ml.dataset_builder import create_entry_quality_labels, ENTRY_HORIZON_BARS, QUALITY_THRESHOLD_PTS

dates = pd.date_range('2026-08-03 09:35', periods=10, freq='1min')
high  = [100,100,100,100,100,100,150,150,100,100]
low   = [100]*10
close = [100]*10
df = pd.DataFrame({'date': dates, 'open': close, 'high': high, 'low': low, 'close': close, 'volume': 0})
out = create_entry_quality_labels(df.copy())
H = ENTRY_HORIZON_BARS
print('H =', H, ' threshold =', QUALITY_THRESHOLD_PTS)
day = df['date'].dt.normalize()
fwd = df.groupby(day)['high'].transform(lambda s: s.shift(-1).rolling(H,min_periods=H).max())
print('fwd_high values:', fwd.tolist())
for i in range(10):
    fut = list(range(i+1, min(i+H, 9)+1))
    exp_up = max(high[j] for j in fut) - close[i] if fut else float('nan')
    lbl = out['label_ce'].iloc[i]
    exp_lbl = 1.0 if (fut and exp_up >= QUALITY_THRESHOLD_PTS) else (float('nan') if len(fut) < H else 0.0)
    print('bar', i, '| computed label_ce =', lbl, '| expected true-future label =', exp_lbl)
