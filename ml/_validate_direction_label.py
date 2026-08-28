# Quick validation of forward-direction labels for the "predict movement BEFORE it happens" fix.
# Tests: horizon x threshold x calibration -> OOS AUC & WR at practical thresholds.
import pandas as pd, numpy as np, sys, warnings, lightgbm as lgb
sys.path.insert(0, '.')
warnings.filterwarnings('ignore')
from ml.feature_config import FEATURE_COLUMNS
from ml.predictor_champion import CalibratedLGBM
from sklearn.metrics import roc_auc_score

df = pd.read_csv('ml/models/training_dataset.csv')
df['date'] = pd.to_datetime(df['date'], format='mixed', errors='coerce')
df = df.sort_values('date').reset_index(drop=True)
close = df['close'].to_numpy(dtype=float)
day = df['date'].dt.normalize()
mins = df['date'].dt.hour * 60 + df['date'].dt.minute
in_sess = ((mins >= 9 * 60 + 30) & (mins < 11 * 60)) | ((mins >= 14 * 60) & (mins < 15 * 60 + 15))
df['in_sess'] = in_sess

for H, T in [(5, 20), (10, 30), (10, 20), (20, 40)]:
    fwd = df.groupby(day)['close'].transform(lambda s: s.shift(-H)).to_numpy(dtype=float)
    net = fwd - close
    cut = df.date.max() - pd.Timedelta(days=14)
    df['net'] = net
    tr = df[(df.date < cut) & df.in_sess & ~np.isnan(net)].dropna(subset=FEATURE_COLUMNS).reset_index(drop=True)
    ev = df[(df.date >= cut) & df.in_sess & ~np.isnan(net)].dropna(subset=FEATURE_COLUMNS).reset_index(drop=True)
    print(f'\n===== H={H} T={T}pt (tr={len(tr):,} ev={len(ev):,}) =====')
    for side, sign in [('ce', 1), ('pe', -1)]:
        trl = (tr.net * sign >= T).astype(int).values
        evl = (ev.net * sign >= T).astype(int).values
        m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, max_depth=6,
                               verbose=-1, random_state=42, n_jobs=-1)
        m.fit(tr[FEATURE_COLUMNS], trl)
        split = int(len(tr) * 0.8)
        cal = CalibratedLGBM(m)
        cal.fit_calibration(tr[FEATURE_COLUMNS].iloc[split:], trl[split:])
        p = cal.predict_proba(ev[FEATURE_COLUMNS])[:, 1]
        auc = roc_auc_score(evl, p)
        print(f'  {side.upper()}: base={evl.mean():.1%} AUC={auc:.3f} '
              f'mean={p.mean():.3f} std={p.std():.3f} >0.45={100 * (p > 0.45).mean():.1f}%')
        for thr in [0.45, 0.50, 0.55]:
            msk = p >= thr
            if msk.sum() > 0:
                print(f'    thr={thr}: n={msk.sum()} WR={evl[msk].mean():.1%}')
