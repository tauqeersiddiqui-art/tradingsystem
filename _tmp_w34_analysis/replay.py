import pandas as pd, numpy as np
pd.set_option('display.width', 240)
T = r'd:\All Bots\trading_system\_tmp_w34_analysis'
bars = pd.read_csv(T + r'\bn_bars_aug.csv', parse_dates=['date'])
bars['day'] = bars['date'].dt.date
daybars = {d: g.reset_index(drop=True) for d, g in bars.groupby('day')}

def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + up/dn)

samples = [
    ('2026-08-20','14:17:07','BANKNIFTY26AUG57500PE','PE',-609.0,'worst loser'),
    ('2026-08-20','09:30:11','BANKNIFTY26AUG57600CE','CE',-594.0,'worst loser, 3.8s hold'),
    ('2026-08-18','11:50:29','BANKNIFTY26AUG57300CE','CE',-510.0,'loser'),
    ('2026-08-18','12:07:46','BANKNIFTY26AUG57400CE','CE',-492.0,'loser'),
    ('2026-08-18','10:51:57','BANKNIFTY26AUG57100PE','PE', 537.0,'winner'),
]

for date, tms, sym, side, pnl, tag in samples:
    d = pd.Timestamp(date).date()
    db = daybars[d]
    entry_dt = pd.Timestamp(date + ' ' + tms)
    i = db.index[db['date'] == entry_dt.floor('min')][0]
    o,h,l,c = db['open'],db['high'],db['low'],db['close']
    known = db.iloc[:i+1].copy()  # bars up to and incl. entry minute (entry bar partial at signal time)
    known_c = known['close']
    ema9 = known_c.ewm(span=9, adjust=False).mean().iloc[-1]
    ema20 = known_c.ewm(span=20, adjust=False).mean().iloc[-1]
    r14 = rsi(known_c).iloc[-1]
    tp = (known['high']+known['low']+known['close'])/3
    avg_px = tp.mean()  # volume=0 in source data -> unweighted avg price proxy
    orb = db[db['date'] < pd.Timestamp(date)+pd.Timedelta(hours=9, minutes=30)]
    orb_h, orb_l = orb['high'].max(), orb['low'].min()
    spot = c[i]
    print('='*110)
    print(f'REPLAY {date} {tms} {sym} side={side} pnl={pnl:+.0f} ({tag})')
    print(f'  spot@entry_min={spot:.2f} prev_close={c[i-1]:.2f}  EMA9={ema9:.1f} EMA20={ema20:.1f} RSI14={r14:.1f}')
    print(f'  VWAPproxy={avg_px:.1f} (spot-VWAP={spot-avg_px:+.1f})  ORB0930 hi/lo={orb_h:.1f}/{orb_l:.1f} dist to ORB hi={spot-orb_h:+.1f} lo={spot-orb_l:+.1f}')
    h20,l20 = h[max(0,i-20):i].max(), l[max(0,i-20):i].min()
    print(f'  prior-20bar hi/lo={h20:.1f}/{l20:.1f}  entry spot vs prior hi/lo: {spot-h20:+.1f}/{spot-l20:+.1f}')
    win = db.iloc[max(0,i-14):i+11]
    rows = []
    for j, r in win.iterrows():
        mark = ' <-- ENTRY' if j == i else ''
        dirn = '+' if r.close > r.open else ('-' if r.close < r.open else '=')
        rows.append(f'    {r.date.strftime("%H:%M")} O={r.open:>9.1f} H={r.high:>9.1f} L={r.low:>9.1f} C={r.close:>9.1f} rng={r.high-r.low:>6.1f} {dirn}{mark}')
    print('\n'.join(rows))
    # next-bar move summary
    sgn = 1 if side=='CE' else -1
    base = c[i-1]
    print(f'  favorable spot pts after entry: 1m={sgn*(c[i]-base):+.1f} 3m={sgn*(c[min(i+2,len(db)-1)]-base):+.1f} 5m={sgn*(c[min(i+4,len(db)-1)]-base):+.1f} 10m={sgn*(c[min(i+9,len(db)-1)]-base):+.1f}')
    print()
