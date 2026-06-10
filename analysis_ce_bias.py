
import csv, datetime, warnings
warnings.filterwarnings('ignore')

def load_bt():
    rows=[]
    with open('backtest/results/trade_log.csv') as f:
        for row in csv.DictReader(f):
            try:
                exit_ts=datetime.datetime.fromisoformat(row['exit_time'])
                held=float(row['held_seconds'])
                entry_ts=exit_ts-datetime.timedelta(seconds=held)
                e=float(row['entry_price']); sl=float(row['stop_loss'])
                rows.append({'date':row['date'][:10],'side':row['side'],'entry':e,
                    'pnl':float(row['pnl']),'ml':float(row['ml_prob']),
                    'reason':row['entry_reason'],'exit_r':row['exit_reason'],
                    'held':float(row['held_seconds']),'mfe':float(row['max_pnl']),
                    'sd':round(e-sl,2),'entry_ts':entry_ts,
                    'is_stop':row['exit_reason']=='STOP','bug':False,
                    'orb':'ORB' in row['entry_reason']})
            except: pass
    return rows

def load_lv():
    rows=[]
    with open('data/trades/trade_log_2026_W24.csv') as f:
        for row in csv.DictReader(f):
            try:
                d=row['date']
                ets=datetime.datetime.strptime(d+' '+row['entry_time'],'%Y-%m-%d %H:%M:%S')
                e=float(row['entry_price']); sl=float(row['stop_loss'])
                pnl=float(row['pnl']); peak=float(row['peak_pnl'])
                is_stop=row['exit_reason']=='STOP'
                rows.append({'date':d,'side':row['side'],'entry':e,
                    'pnl':pnl,'ml':float(row['ml_prob']),
                    'reason':row['entry_reason'],'exit_r':row['exit_reason'],
                    'held':float(row['holding_seconds']),
                    'mfe':peak if is_stop else None,
                    'sd':round(e-sl,2),'entry_ts':ets,
                    'is_stop':is_stop,'bug':row['exit_reason']=='TARGET_HIT',
                    'orb':'ORB' in row['entry_reason']})
            except: pass
    return rows

nifty={}
with open('data/historical/nifty_1m_full.csv') as f:
    for row in csv.DictReader(f):
        try:
            ts=datetime.datetime.fromisoformat(row['date'])
            nifty[ts]={'c':float(row['close'])}
        except: pass

def nm(ts,d):
    t1=ts.replace(second=0,microsecond=0)
    t2=t1+datetime.timedelta(minutes=d)
    if t1 in nifty and t2 in nifty: return round(nifty[t2]['c']-nifty[t1]['c'],2)
    return None

def stats(sub):
    if not sub: return 0,0,0,0,0
    wins=[t['pnl'] for t in sub if t['pnl']>0]
    loss=[t['pnl'] for t in sub if t['pnl']<=0]
    wr=len(wins)/len(sub)*100
    pf=sum(wins)/abs(sum(loss)) if wins and loss else 0
    exp=sum(t['pnl'] for t in sub)/len(sub)
    return len(sub),wr,pf,exp,sum(t['pnl'] for t in sub)

bt=load_bt(); lv=load_lv()
clean=bt+[t for t in lv if not t['bug']]
all_ce=[t for t in clean if t['side']=='CE']
all_pe=[t for t in clean if t['side']=='PE']

for t in all_ce+all_pe:
    t['n3']=nm(t['entry_ts'],-3)
    t['p3']=nm(t['entry_ts'],+3)

print('='*70)
print('DUAL-SIDE SELECTOR SIMULATION')
print('='*70)
print('%-35s %4s %5s %5s %7s %8s' % ('Rule','n','WR','PF','Exp','Total'))
print('-'*65)
n,wr,pf,exp,tot=stats(all_pe)
print('%-35s %4d %5.1f%% %5.2f %+7.0f %+8.0f  BASELINE' % ('PE only',n,wr,pf,exp,tot))
n,wr,pf,exp,tot=stats(clean)
print('%-35s %4d %5.1f%% %5.2f %+7.0f %+8.0f  CURRENT' % ('All clean (PE+CE)',n,wr,pf,exp,tot))
print()
# When CE fires, the live engine sets pe_adj=0 (direction gate)
# PE_avg on CE-eligible rows = 0.29 (from training data analysis)
# Margin selector: take CE only if CE_prob > 0.29 + margin
PE_AVG = 0.29
for margin in [0.05, 0.10, 0.15, 0.20]:
    min_ce = PE_AVG + margin
    ce_filt = [t for t in all_ce if t['ml'] >= min_ce]
    combined = all_pe + ce_filt
    n,wr,pf,exp,tot=stats(combined)
    print('%-35s %4d %5.1f%% %5.2f %+7.0f %+8.0f' % (
        'PE + CE (margin>=%.2f, CE>=%.2f)'%(margin,min_ce),n,wr,pf,exp,tot))

print()
print('='*70)
print('CE THRESHOLD ANALYSIS')
print('='*70)
print('%-28s %4s %5s %5s %7s %8s' % ('Filter','n','WR','PF','Exp','Total'))
print('-'*58)
for thr in [0.00,0.80,0.85,0.90,0.95]:
    ce_f=[t for t in all_ce if t['ml']>=thr]
    combined=all_pe+ce_f
    n,wr,pf,exp,tot=stats(combined)
    nc=len(ce_f)
    print('%-28s %4d %5.1f%% %5.2f %+7.0f %+8.0f  (CE=%d)' % (
        'PE + CE>=%.2f'%thr,n,wr,pf,exp,tot,nc))

print()
print('='*70)
print('CE ONLY at different thresholds:')
for thr in [0.00,0.70,0.75,0.80,0.85,0.90,0.95]:
    ce_f=[t for t in all_ce if t['ml']>=thr]
    if not ce_f: print('CE>=%.2f  n=0'%thr); continue
    n,wr,pf,exp,tot=stats(ce_f)
    print('CE>=%.2f  n=%2d  WR=%.0f%%  PF=%.2f  Exp=%+.0f' % (thr,n,wr,pf,exp))

print()
print('='*70)
print('NIFTY DIRECTION AT CE ENTRIES')
print('='*70)
bullish=[t for t in all_ce if t['n3'] is not None and t['n3']>+5]
bearish=[t for t in all_ce if t['n3'] is not None and t['n3']<-5]
flat   =[t for t in all_ce if t['n3'] is None or -5<=t['n3']<=5]
print('CE: n=%d  Nifty_rising(>+5pt before): n=%d WR=%.0f%%  '
      'falling(<-5): n=%d WR=%.0f%%  flat: n=%d WR=%.0f%%' % (
    len(all_ce),
    len(bullish), 100*len([t for t in bullish if t['pnl']>0])/(len(bullish) or 1),
    len(bearish), 100*len([t for t in bearish if t['pnl']>0])/(len(bearish) or 1),
    len(flat),    100*len([t for t in flat    if t['pnl']>0])/(len(flat)    or 1)))
print()

print('='*70)
print('TASK 5: EVERY LOSING CE TRADE — was PE better?')
print('='*70)
print('Backtest CE wins (for comparison):')
for t in [t for t in all_ce if t['pnl']>0]:
    print('  WIN  %s  ml=%.2f  pnl=%+.0f  exit=%s  n3=%s' % (
        t['date'],t['ml'],t['pnl'],t['exit_r'],
        ('%+.1f'%t['n3']) if t['n3'] else '--'))
print()
print('All CE losses — NIFTY context:')
for t in [t for t in all_ce if t['pnl']<=0]:
    n3s=('%+.1f'%t['n3']) if t['n3'] is not None else '--'
    p3s=('%+.1f'%t['p3']) if t['p3'] is not None else '--'
    bull='BULL' if (t['n3'] and t['n3']>0) else 'BEAR'
    print('  LOSS %s  ml=%.2f  pnl=%+.0f  NIFTY_-3m=%s  NIFTY_+3m=%s  %s' % (
        t['date'],t['ml'],t['pnl'],n3s,p3s,bull))
