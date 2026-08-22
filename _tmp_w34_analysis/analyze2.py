import pandas as pd, numpy as np
pd.set_option('display.width', 260); pd.set_option('display.max_columns', 60)
T = r'd:\All Bots\trading_system\_tmp_w34_analysis'
feats = pd.read_csv(T + r'\per_trade_features.csv')
trades = pd.read_csv(r'd:\All Bots\trading_system\data\trades\trade_log_2026_W34.csv')

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

print('=== PART A: aggregates over', len(blk), 'distinct entries (raw ledger rows:', len(trades), ') ===')
wins = blk[blk.repl_pnl>0]; losses = blk[blk.repl_pnl<=0]
print(f'win rate: {len(wins)}/{len(blk)} = {len(wins)/len(blk)*100:.1f}%')
print(f'avg win: {wins.repl_pnl.mean():.0f}  avg loss: {losses.repl_pnl.mean():.0f}  payoff ratio: {abs(wins.repl_pnl.mean()/losses.repl_pnl.mean()):.2f}')
print(f'total representative pnl: {blk.repl_pnl.sum():.0f}')
print(f'raw sum of all ledger pnl rows (inflated by dup logging): {trades.pnl.sum():.0f}')
print(f'avg hold s: {blk.hold_med.mean():.1f}  median hold s: {blk.hold_med.median():.1f}')
print(f'avg MFE rs: {blk.MFE.mean():.0f}  avg MAE rs: {blk.MAE.mean():.0f}  capture ratio sum(MFE)/sum(|MAE|): {blk.MFE.sum()/abs(blk.MAE.sum()):.2f}')
print(f'slippage_pts: {trades.slippage_pts.notna().sum()} non-null of {len(trades)} -> NOT RECORDED')
print(f'signal_price/fill_price/first_bid populated: {(trades.signal_price>0).sum()} / {(trades.fill_price>0).sum()} / {(trades.first_bid.notna() & (trades.first_bid!=0)).sum()}')
print()
print('PnL by strategy:'); print(blk.groupby('regime').agg(n=('repl_pnl','size'), wins=('repl_pnl', lambda s:(s>0).sum()), pnl=('repl_pnl','sum'), avg=('repl_pnl','mean')).to_string())
print()
blk['hour'] = blk.entry_dt.dt.strftime('%H:%M')
def bucket(t):
    h,m = map(int,t.split(':'))
    x = h*60+m
    if x < 600: return '1 open 09:15-10:00'
    if x < 690: return '2 morning 10:00-11:30'
    if x < 810: return '3 midday 11:30-13:30'
    return '4 afternoon 13:30-15:30'
blk['tod'] = blk['hour'].map(bucket)
print('PnL by time-of-day:'); print(blk.groupby('tod').agg(n=('repl_pnl','size'), wins=('repl_pnl', lambda s:(s>0).sum()), pnl=('repl_pnl','sum')).to_string())
print()
print('Exit reason distribution (raw rows):'); print(trades.exit_reason.value_counts().to_string())
print('Exit reason (distinct entries):'); print(blk.exit_reason.value_counts().to_string())
print()
print('R_multiple anomaly check: median R of wins:', wins.R_mult.median(), ' median R of losses:', losses.R_mult.median())
print('R values >0 on losing trades:', ((blk.repl_pnl<0)&(blk.R_mult>0)).sum(), 'of', (blk.repl_pnl<0).sum(), 'losers')
print()
print('MFE==0 (never profitable) trades:', (blk.MFE==0).sum(), 'of', len(blk))
print()
print('=== PART C: instant-loss subset (MFE <= 10% of |MAE|) ===')
inst = blk[blk.MFE <= 0.1*abs(blk.MAE)]
print(inst[['date','entry_time','symbol','side','entry_price','exit_med','repl_pnl','MFE','MAE','hold_med']].to_string(index=False))
