
import csv, datetime, statistics, collections

def load_bt():
    rows=[]
    with open('backtest/results/trade_log.csv') as f:
        for row in csv.DictReader(f):
            try:
                d=row['date'][:10]
                exit_ts=datetime.datetime.fromisoformat(row['exit_time'])
                held=float(row['held_seconds'])
                entry_ts=exit_ts-datetime.timedelta(seconds=held)
                e=float(row['entry_price']); sl=float(row['stop_loss'])
                pnl=float(row['pnl']); mfe=float(row['max_pnl'])
                is_stop=row['exit_reason']=='STOP'
                rows.append({'date':d,'side':row['side'],'entry':e,'exit_p':float(row['exit_price']),
                    'qty':int(row.get('qty',row.get('quantity',75))),'pnl':pnl,'ml':float(row['ml_prob']),
                    'reason':row['entry_reason'],'exit_r':row['exit_reason'],
                    'held':held,'mfe':mfe,'mae':pnl if is_stop else 0.0,'sd':round(e-sl,2),
                    'entry_ts':entry_ts,'exit_ts':exit_ts,'src':'bt','regime':row.get('regime','RANGE'),
                    'is_stop':is_stop,'bug':False,'orb':'ORB' in row['entry_reason']})
            except: pass
    return rows

def load_lv():
    rows=[]
    with open('data/trades/trade_log_2026_W24.csv') as f:
        for row in csv.DictReader(f):
            try:
                d=row['date']
                ets=datetime.datetime.strptime(d+' '+row['entry_time'],'%Y-%m-%d %H:%M:%S')
                xts=datetime.datetime.strptime(d+' '+row['exit_time'],'%Y-%m-%d %H:%M:%S')
                e=float(row['entry_price']); sl=float(row['stop_loss'])
                pnl=float(row['pnl']); peak=float(row['peak_pnl'])
                is_stop=row['exit_reason']=='STOP'
                bug=row['exit_reason']=='TARGET_HIT'
                rows.append({'date':d,'side':row['side'],'entry':e,'exit_p':float(row['exit_price']),
                    'qty':int(row['quantity']),'pnl':pnl,'ml':float(row['ml_prob']),
                    'reason':row['entry_reason'],'exit_r':row['exit_reason'],
                    'held':float(row['holding_seconds']),
                    'mfe':peak if is_stop else None,'mae':min(0.0,pnl-peak) if is_stop else None,
                    'sd':round(e-sl,2),'entry_ts':ets,'exit_ts':xts,'src':'live',
                    'regime':row['regime'],'is_stop':is_stop,'bug':bug,'orb':'ORB' in row['entry_reason']})
            except: pass
    return rows

nifty={}
with open('data/historical/nifty_1m_full.csv') as f:
    for row in csv.DictReader(f):
        try:
            ts=datetime.datetime.fromisoformat(row['date'])
            nifty[ts]={'c':float(row['close']),'h':float(row['high']),'l':float(row['low']),'o':float(row['open'])}
        except: pass

def nm(ts,d):
    t1=ts.replace(second=0,microsecond=0)
    t2=t1+datetime.timedelta(minutes=d)
    if t1 in nifty and t2 in nifty: return round(nifty[t2]['c']-nifty[t1]['c'],2)
    return None

def atr14(ts):
    t=ts.replace(second=0,microsecond=0)
    cc=[]
    for i in range(20,0,-1):
        c=nifty.get(t-datetime.timedelta(minutes=i))
        if c: cc.append(c)
    if len(cc)<14: return None
    cc=cc[-14:]; trs=[]
    for i in range(1,len(cc)):
        trs.append(max(cc[i]['h']-cc[i]['l'],abs(cc[i]['h']-cc[i-1]['c']),abs(cc[i]['l']-cc[i-1]['c'])))
    return round(sum(trs)/len(trs),2) if trs else None

bt=load_bt(); lv=load_lv()
for t in bt+lv:
    t['atr']=atr14(t['entry_ts'])
    t['nm1']=nm(t['entry_ts'],-1)
    t['nm3']=nm(t['entry_ts'],-3)
    t['np1']=nm(t['entry_ts'],+1)
    t['np3']=nm(t['entry_ts'],+3)

all_t=bt+lv
clean=bt+[t for t in lv if not t['bug']]

# -----------------------------------------------------------------
# TASK 2: LOSS CLASSIFICATION
# -----------------------------------------------------------------
def classify_loss(t):
    pnl=t['pnl']; mfe=t['mfe']; mae=t['mae']
    nm1=t['nm1']; nm3=t['nm3']; np1=t['np1']; np3=t['np3']; sd=t['sd']
    side=t['side']
    if t['bug']: return 'BUG_EXIT'
    if pnl > 0: return 'WINNER'
    mfe_pts=(mfe/t['qty']) if (mfe and t['qty']>0) else 0
    loss_pts=abs(pnl)/t['qty'] if t['qty']>0 else 0
    if mfe and mfe>0 and t['is_stop']:
        if mfe_pts>=5: return 'GOOD_TRADE_REVERSED'
        else:          return 'STOP_TOO_TIGHT'
    if mfe is not None and mfe<=0 and loss_pts<=sd*1.2:
        return 'SPREAD_LOSS'
    if np1 is not None:
        if side=='CE' and np1<-2: return 'WRONG_DIRECTION'
        if side=='PE' and np1>+2: return 'WRONG_DIRECTION'
    if nm3 is not None:
        if side=='PE' and nm3<-5: return 'LATE_ENTRY'
        if side=='CE' and nm3>+5: return 'LATE_ENTRY'
    if np1 is not None and np3 is not None and abs(np3)<3:
        return 'THETA_FLAT_MARKET'
    if mfe is not None and mfe<=0:
        return 'WRONG_DIRECTION'
    return 'OTHER'

losses=[t for t in clean if t['pnl']<=0]
total_l=len(losses)
cat_counts=collections.Counter(classify_loss(t) for t in losses)

print()
print('='*72)
print('TASK 2: LOSS CLASSIFICATION  (clean valid exits, n=%d losses of %d trades)' % (total_l,len(clean)))
print('='*72)
cats=['WRONG_DIRECTION','SPREAD_LOSS','STOP_TOO_TIGHT','GOOD_TRADE_REVERSED',
      'LATE_ENTRY','THETA_FLAT_MARKET','OTHER']
for cat in cats:
    n=cat_counts.get(cat,0)
    pct=100*n/total_l if total_l else 0
    ls=sum(t['pnl'] for t in losses if classify_loss(t)==cat)
    print('  %-25s  n=%3d  %5.1f%%  total_loss=%+.0f' % (cat,n,pct,ls))
print('  TOTAL                       %3d  100.0%%' % total_l)

# -----------------------------------------------------------------
# TASK 3: CE vs PE
# -----------------------------------------------------------------
def sstats(subset,label):
    if not subset: print('  %-24s  n=0' % label); return
    n=len(subset)
    wins=[t['pnl'] for t in subset if t['pnl']>0]
    loss=[t['pnl'] for t in subset if t['pnl']<=0]
    wr=len(wins)/n*100
    aw=statistics.mean(wins) if wins else 0
    al=statistics.mean(loss) if loss else 0
    tot=sum(t['pnl'] for t in subset)
    pf=sum(wins)/abs(sum(loss)) if wins and loss else (float('inf') if not loss else 0)
    mfe_v=[t['mfe'] for t in subset if t.get('mfe') is not None]
    mae_v=[t['mae'] for t in subset if t.get('mae') is not None]
    avg_mfe=statistics.mean(mfe_v) if mfe_v else 0
    avg_mae=statistics.mean(mae_v) if mae_v else 0
    print('  %-24s n=%3d WR=%5.1f%% AvgW=%+7.0f AvgL=%+7.0f PF=%5.2f MFE=%+7.0f MAE=%+7.0f Exp=%+7.0f' % (
        label,n,wr,aw,al,pf,avg_mfe,avg_mae,tot/n))

print()
print('='*100)
print('TASK 3: CE vs PE  (clean trades only, n=%d)' % len(clean))
print('='*100)
print('  %-24s %s' % ('Group','n    WR      AvgW    AvgL     PF     MFE     MAE     Exp'))
print('  '+'-'*92)
sstats(clean,'ALL CLEAN')
print()
sstats([t for t in clean if t['side']=='CE'],'CE total')
sstats([t for t in clean if t['side']=='CE' and not t['orb']],'  CE pure-ML')
sstats([t for t in clean if t['side']=='CE' and t['orb']],'  CE ORB')
print()
sstats([t for t in clean if t['side']=='PE'],'PE total')
sstats([t for t in clean if t['side']=='PE' and not t['orb']],'  PE pure-ML')
sstats([t for t in clean if t['side']=='PE' and t['orb']],'  PE ORB')

# -----------------------------------------------------------------
# TASK 4: ML CALIBRATION
# -----------------------------------------------------------------
print()
print('='*100)
print('TASK 4: ML CALIBRATION BY BUCKET (clean trades)')
print('='*100)
print('  %-18s %s' % ('ML Bucket','n    WR      AvgPnL    PF     MFE     Exp'))
print('  '+'-'*72)
buckets=[(0.65,0.70),(0.70,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.90),(0.90,0.95),(0.95,1.01)]
for lo,hi in buckets:
    sub=[t for t in clean if lo<=t['ml']<hi]
    if not sub: print('  %-18s  n=0' % ('%.2f-%.2f'%(lo,hi))); continue
    n=len(sub)
    wins=[t['pnl'] for t in sub if t['pnl']>0]
    loss=[t['pnl'] for t in sub if t['pnl']<=0]
    wr=len(wins)/n*100
    avg_pnl=sum(t['pnl'] for t in sub)/n
    pf=sum(wins)/abs(sum(loss)) if wins and loss else (float('inf') if not loss else 0)
    mfe_v=[t['mfe'] for t in sub if t.get('mfe') is not None]
    avg_mfe=statistics.mean(mfe_v) if mfe_v else 0
    print('  %-18s n=%3d WR=%5.1f%% AvgPnL=%+7.0f PF=%5.2f MFE=%+7.0f' % (
        '%.2f-%.2f'%(lo,hi),n,wr,avg_pnl,pf,avg_mfe))

# -----------------------------------------------------------------
# TASK 5: ENTRY TIMING
# -----------------------------------------------------------------
print()
print('='*100)
print('TASK 5: ENTRY TIMING — NIFTY MOVE BEFORE/AFTER  (losing clean trades, n=%d)' % total_l)
print('='*100)
print('  %-12s %-4s %-4s %6s %6s %6s %6s %7s %7s  %-20s' % (
    'Date','Side','Typ','ML','ATR','Opt','PnL','NIFTY-3m','NIFTY+3m','ExitReason'))
print('  '+'-'*90)
losing_clean=[t for t in clean if t['pnl']<=0]
for t in sorted(losing_clean,key=lambda x:x['date']):
    m3=('%+.1f'%t['nm3']) if t['nm3'] is not None else '--'
    p3=('%+.1f'%t['np3']) if t['np3'] is not None else '--'
    atr_s=('%.1f'%t['atr']) if t['atr'] else '--'
    typ='ORB' if t['orb'] else 'ML'
    print('  %-12s %-4s %-4s %6.2f %6s %6.1f %7.0f %8s %8s  %-20s' % (
        t['date'],t['side'],typ,t['ml'],atr_s,t['entry'],t['pnl'],m3,p3,t['exit_r'][:20]))

# NIFTY move analysis summary
nm3_vals=[t['nm3'] for t in losing_clean if t['nm3'] is not None]
np3_vals=[t['np3'] for t in losing_clean if t['np3'] is not None]
nm1_vals=[t['nm1'] for t in losing_clean if t['nm1'] is not None]
np1_vals=[t['np1'] for t in losing_clean if t['np1'] is not None]
print()
print('  NIFTY movement summary for losing trades:')
print('  Avg NIFTY -3min: %+.2f  -1min: %+.2f  +1min: %+.2f  +3min: %+.2f' % (
    statistics.mean(nm3_vals) if nm3_vals else 0,
    statistics.mean(nm1_vals) if nm1_vals else 0,
    statistics.mean(np1_vals) if np1_vals else 0,
    statistics.mean(np3_vals) if np3_vals else 0))

pe_loss=[t for t in losing_clean if t['side']=='PE']
ce_loss=[t for t in losing_clean if t['side']=='CE']
if pe_loss:
    pe_nm3=[t['nm3'] for t in pe_loss if t['nm3'] is not None]
    pe_np3=[t['np3'] for t in pe_loss if t['np3'] is not None]
    print('  PE losers avg NIFTY -3m: %+.2f  +3m: %+.2f  (PE profits when NIFTY falls)' % (
        statistics.mean(pe_nm3) if pe_nm3 else 0,
        statistics.mean(pe_np3) if pe_np3 else 0))
if ce_loss:
    ce_nm3=[t['nm3'] for t in ce_loss if t['nm3'] is not None]
    ce_np3=[t['np3'] for t in ce_loss if t['np3'] is not None]
    print('  CE losers avg NIFTY -3m: %+.2f  +3m: %+.2f  (CE profits when NIFTY rises)' % (
        statistics.mean(ce_nm3) if ce_nm3 else 0,
        statistics.mean(ce_np3) if ce_np3 else 0))

# -----------------------------------------------------------------
# TASK 6: STOP EFFICIENCY
# -----------------------------------------------------------------
print()
print('='*100)
print('TASK 6: STOP EFFICIENCY')
print('='*100)
stop_trades=[t for t in clean if t['is_stop']]
if stop_trades:
    mfe_at_stop=[t for t in stop_trades if t.get('mfe') is not None]
    never_pos=[t for t in mfe_at_stop if t['mfe']<=0]
    went_pos =[t for t in mfe_at_stop if t['mfe']>0]
    print('  Total STOP trades: %d' % len(stop_trades))
    print('  Never profitable (MFE<=0): %d (%4.1f%%)  avg_pnl=%+.0f' % (
        len(never_pos),100*len(never_pos)/len(stop_trades),
        statistics.mean(t['pnl'] for t in never_pos) if never_pos else 0))
    print('  Went positive then stopped: %d (%4.1f%%)  avg_MFE=%+.0f  avg_pnl=%+.0f' % (
        len(went_pos),100*len(went_pos)/len(stop_trades),
        statistics.mean(t['mfe'] for t in went_pos) if went_pos else 0,
        statistics.mean(t['pnl'] for t in went_pos) if went_pos else 0))
    if went_pos:
        mfe_pts=[t['mfe']/t['qty'] for t in went_pos if t['qty']>0]
        sl_pts=[abs(t['pnl'])/t['qty'] for t in went_pos if t['qty']>0]
        print('  Avg MFE before reversal: %.1f pts  Avg loss at stop: %.1f pts  Ratio: %.2f' % (
            statistics.mean(mfe_pts),statistics.mean(sl_pts),
            statistics.mean(mfe_pts)/statistics.mean(sl_pts) if statistics.mean(sl_pts) else 0))
print()
print('  Straight-down stop losers (MFE=0, worst category):')
for t in never_pos[:5]:
    nm3_s=('%+.1f'%t['nm3']) if t['nm3'] is not None else '--'
    np3_s=('%+.1f'%t['np3']) if t['np3'] is not None else '--'
    print('    %s %s ml=%.2f sd=%.1f pnl=%+.0f NIFTY_3m_before=%s after=%s' % (
        t['date'],t['side'],t['ml'],t['sd'],t['pnl'],nm3_s,np3_s))
