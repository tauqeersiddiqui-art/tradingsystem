import pandas as pd, numpy as np
pd.set_option('display.width', 260); pd.set_option('display.max_columns', 60)
T = r'd:\All Bots\trading_system\_tmp_w34_analysis'
trades = pd.read_csv(r'd:\All Bots\trading_system\data\trades\trade_log_2026_W34.csv')
bars = pd.read_csv(T + r'\bn_bars_aug.csv', parse_dates=['date'])
bars['day'] = bars['date'].dt.date

gk = ['date','entry_time','symbol','side','regime','entry_price','entry_reason']
blk = trades.groupby(gk).agg(
    n_rows=('pnl','size'), qty=('quantity','first'),
    exit_med=('exit_price','median'), pnl_sum=('pnl','sum'),
    MFE=('MFE','median'), MAE=('MAE','median'),
    hold_med=('holding_seconds','median'),
    R_mult=('R_multiple','median'), ml_prob=('ml_prob','first'),
    exit_reason=('exit_reason', lambda s: s.mode().iloc[0])).reset_index()
blk['entry_dt'] = pd.to_datetime(blk['date'] + ' ' + blk['entry_time'])
blk['repl_pnl'] = (blk['exit_med'] - blk['entry_price']) * blk['qty']
blk['win'] = blk['repl_pnl'] > 0

daybars = {d: g.reset_index(drop=True) for d, g in bars.groupby('day')}

rows = []
for _, t in blk.iterrows():
    d = t['entry_dt'].date(); db = daybars[d]
    minute = t['entry_dt'].floor('min')
    idx = db.index[db['date'] == minute]
    if len(idx) == 0:
        rows.append(dict(block=t, err='no bar')); continue
    i = idx[0]
    if i < 2: continue
    o,h,l,c = db['open'], db['high'], db['low'], db['close']
    prior = slice(max(0,i-20), i); prior10 = slice(max(0,i-10), i)
    h20,l20 = h[prior].max(), l[prior].min()
    h10,l10 = h[prior10].max(), l[prior10].min()
    rng20 = h20-l20
    entry_spot = c[i]          # end-of-entry-minute spot proxy
    prev_close = c[i-1]
    is_ce = t['side'] == 'CE'
    if is_ce:
        extreme, dist_extreme = h20, entry_spot - h20
        pos_range = (entry_spot - l20)/rng20
        broke = h[i] > h20
        reverted = any(c[i:i+5] < h20)
    else:
        extreme, dist_extreme = l20, l20 - entry_spot
        pos_range = (h20 - entry_spot)/rng20
        broke = l[i] < l20
        reverted = any(c[i:i+5] > l20)
    # move consumed since swing start (30 bars)
    w = slice(0, i+1)
    if is_ce:
        swing = l[w].min(); moverng = h[w].max() - swing
        consumed = (entry_spot - swing)/moverng if moverng>0 else np.nan
    else:
        swing = h[w].max(); moverng = swing - l[w].min()
        consumed = (swing - entry_spot)/moverng if moverng>0 else np.nan
    # exhaustion context on completed bars
    dirc = 0
    for k in range(i-1, max(0,i-11), -1):
        up = c[k] > o[k]
        if (is_ce and up) or ((not is_ce) and not up): dirc += 1
        else: break
    rngs = (h[prior]-l[prior])
    last_rng = h[i-1]-l[i-1]
    rng_ratio = last_rng / rngs.mean() if rngs.mean()>0 else np.nan
    # breakout first touch
    bars_since_break = np.nan
    if broke:
        session = db  # day starts 09:15
        lvl = h20 if is_ce else l20
        col = session['high'] if is_ce else session['low']
        touch = session.index[(col > lvl) if is_ce else (col < lvl)]
        if len(touch): bars_since_break = i - touch[0]
    # post-entry spot path (direction-adjusted favorable pts)
    sgn = 1 if is_ce else -1
    base = prev_close
    def fav(k):
        j = i+k
        return np.nan if j >= len(db) else sgn*(c[j]-base)
    fav1, fav3, fav5, fav10 = fav(0), fav(2), fav(4), fav(9)
    nxt = slice(i, min(i+5, len(db)))
    worst = sgn*((l[nxt].min()-base) if is_ce else (base-h[nxt].max()))
    best  = sgn*((h[nxt].max()-base) if is_ce else (base-l[nxt].min()))
    # classification
    if broke and reverted: cls = 'FALSE_BREAKOUT'
    elif pos_range >= 0.80 and fav5 is not np.nan and fav5 < 0: cls = 'LATE_ENTRY'
    elif fav5 is not None and fav5 > 0: cls = 'GOOD_ENTRY'
    else: cls = 'LATE_ENTRY' if pos_range >= 0.80 else 'MIXED'
    rows.append(dict(date=str(d), time=str(t['entry_dt'].time())[:8], symbol=t['symbol'],
        side=t['side'], regime=t['regime'], n_rows=t['n_rows'], qty=t['qty'],
        entry=t['entry_price'], exit_med=t['exit_med'], pnl=t['repl_pnl'],
        ml_prob=t['ml_prob'], MFE=t['MFE'], MAE=t['MAE'], hold_s=t['hold_med'],
        R=t['R_mult'], exit_reason=t['exit_reason'],
        spot_prev=prev_close, spot_entry=entry_spot, h20=round(h20,1), l20=round(l20,1),
        dist_ext=round(dist_extreme,1), pos_rng=round(pos_range,2), consumed=round(consumed,2),
        consec_dir=dirc, last_rng_ratio=round(rng_ratio,2), broke=int(broke),
        bars_since_break=bars_since_break,
        fav1=round(fav1,1), fav3=round(fav3,1), fav5=round(fav5,1), fav10=round(fav10,1),
        worst5=round(worst,1), best5=round(best,1), cls=cls))

out = pd.DataFrame(rows)
out.to_csv(T + r'\per_trade_features.csv', index=False)
print(out.to_string(index=False))


