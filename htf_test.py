
import csv, datetime, numpy as np

nifty = {}
with open('data/historical/nifty_1m_full.csv') as f:
    for row in csv.DictReader(f):
        try:
            nifty[datetime.datetime.fromisoformat(row['date'])] = float(row['close'])
        except: pass

def htf_bullish(ts, fast_n, slow_n):
    t = ts.replace(second=0, microsecond=0)
    prices = []
    cur = t - datetime.timedelta(minutes=slow_n + 5)
    while cur <= t:
        if cur in nifty: prices.append(nifty[cur])
        cur += datetime.timedelta(minutes=1)
    if len(prices) < slow_n: return None
    fast = float(np.mean(prices[-fast_n:]))
    slow = float(np.mean(prices[-slow_n:]))
    return fast > slow

def load_trades():
    rows = []
    with open('backtest/results/trade_log.csv') as f:
        for row in csv.DictReader(f):
            try:
                exit_ts = datetime.datetime.fromisoformat(row['exit_time'])
                held    = float(row['held_seconds'])
                entry_ts= exit_ts - datetime.timedelta(seconds=held)
                entry   = float(row['entry_price'])
                sl      = float(row['stop_loss'])
                pnl     = float(row['pnl'])
                mfe     = float(row['max_pnl'])
                qty     = int(row.get('qty', row.get('quantity', 75)))
                rows.append({
                    'date':row['date'][:10], 'side':row['side'],
                    'entry':entry, 'qty':qty, 'pnl':pnl, 'ml':float(row['ml_prob']),
                    'exit_r':row['exit_reason'], 'held':held, 'mfe':mfe,
                    'mfe_pts':round(mfe/qty,2) if qty>0 else 0,
                    'is_stop':row['exit_reason']=='STOP', 'entry_ts':entry_ts,
                    'orb':'ORB' in row['entry_reason'],
                })
            except: pass
    with open('data/trades/trade_log_2026_W24.csv') as f:
        for row in csv.DictReader(f):
            try:
                if row['exit_reason'] != 'STOP': continue
                d = row['date']
                ets = datetime.datetime.strptime(d+' '+row['entry_time'],'%Y-%m-%d %H:%M:%S')
                entry = float(row['entry_price']); qty = int(row['quantity'])
                peak  = float(row['peak_pnl']);    pnl  = float(row['pnl'])
                rows.append({
                    'date':d, 'side':row['side'], 'entry':entry, 'qty':qty, 'pnl':pnl,
                    'ml':float(row['ml_prob']), 'exit_r':row['exit_reason'],
                    'held':float(row['holding_seconds']), 'mfe':peak,
                    'mfe_pts':round(peak/qty,2) if qty>0 else 0,
                    'is_stop':True, 'entry_ts':ets,
                    'orb':'ORB' in row['entry_reason'],
                })
            except: pass
    return rows

def apply_be(trades_in, trigger=4):
    result = []
    for t in trades_in:
        pnl = 0.0 if (t['is_stop'] and t['mfe_pts'] >= trigger) else t['pnl']
        result.append({**t, 'pnl': pnl})
    return result

def stats(subset, label, indent=0):
    pad = ' ' * indent
    if not subset:
        print(f'{pad}{label:<42}  n=  0'); return {}
    n    = len(subset)
    wins = [t['pnl'] for t in subset if t['pnl'] > 0]
    loss = [t['pnl'] for t in subset if t['pnl'] <= 0]
    wr   = len(wins)/n*100
    gw   = sum(wins); gl = abs(sum(loss)) or 0.001
    pf   = gw/gl
    tot  = sum(t['pnl'] for t in subset)
    exp  = tot/n
    eq=0; peq=0; mdd=0
    for t in subset: eq+=t['pnl']; peq=max(peq,eq); mdd=min(mdd,eq-peq)
    print(f'{pad}{label:<42}  n={n:3d}  WR={wr:5.1f}%  PF={pf:5.2f}  Exp={exp:+7.0f}  Total={tot:+8.0f}  MDD={mdd:+7.0f}')
    return {'n':n,'wr':wr,'pf':pf,'exp':exp,'tot':tot,'mdd':mdd}

trades   = load_trades()
ce_trades = [t for t in trades if t['side'] == 'CE']
pe_trades  = [t for t in trades if t['side'] == 'PE']
print(f'Loaded {len(trades)} trades  CE={len(ce_trades)}  PE={len(pe_trades)}')

# Compute HTF flags for all CE trades
configs = [('15m','8_15',8,15), ('30m','15_30',15,30), ('60m','30_60',30,60)]
print('Computing HTF flags for CE entries...')
for _,key,fast,slow in configs:
    for t in ce_trades:
        t[f'htf_{key}'] = htf_bullish(t['entry_ts'], fast, slow)
print('Done.')

print()
print('='*95)
print('P3 BACKTEST: HTF CE TREND FILTER  (BE@4 pts applied throughout)')
print('='*95)
print(f'  {"Scenario":<42}  {"n":>3}  {"WR":>6}  {"PF":>5}  {"Exp":>7}  {"Total":>8}  {"MDD":>7}')
print('  ' + '-'*87)

base = apply_be(trades, trigger=4)
stats(base, 'Baseline (no HTF, BE@4)', indent=2)
print()

for label, key, fast, slow in configs:
    htf_key = f'htf_{key}'
    ce_pass  = [t for t in ce_trades if t.get(htf_key) is True]
    ce_block = [t for t in ce_trades if t.get(htf_key) is False]
    ce_unkn  = [t for t in ce_trades if t.get(htf_key) is None]
    combined = apply_be(pe_trades + ce_pass, trigger=4)
    bl_wins  = [t for t in ce_block if t['pnl'] > 0]
    bl_loss  = [t for t in ce_block if t['pnl'] <= 0]
    print(f'  --- {label} filter (EMA{fast} > EMA{slow}) ---')
    stats(combined, f'PE + CE(HTF:{label} pass)', indent=4)
    print(f'    CE blocked={len(ce_block)} (wins_blocked={len(bl_wins)}, losses_blocked={len(bl_loss)}, pnl_blocked={sum(t["pnl"] for t in ce_block):+.0f})')
    print(f'    CE unknown(insufficient data)={len(ce_unkn)}')
    if ce_pass:
        ce_sim = apply_be(ce_pass, trigger=4)
        cew = [t['pnl'] for t in ce_sim if t['pnl']>0]
        cel = [t['pnl'] for t in ce_sim if t['pnl']<=0]
        print(f'    CE(pass) n={len(ce_pass)}  WR={len(cew)/len(ce_pass)*100:.0f}%  pnl={sum(t["pnl"] for t in ce_sim):+.0f}')
    print()

print()
print('='*95)
print('CE TRADE-LEVEL HTF FLAGS:')
print(f'  {"Date":<12} {"ML":>5} {"Out":>4}  {"pnl":>7}  {"15m":>3} {"30m":>3} {"60m":>3}  note')
print('  ' + '-'*60)
for t in sorted(ce_trades, key=lambda x: x['date']):
    h15 = t.get('htf_8_15');  s15 = ('T' if h15 is True else ('F' if h15 is False else '?'))
    h30 = t.get('htf_15_30'); s30 = ('T' if h30 is True else ('F' if h30 is False else '?'))
    h60 = t.get('htf_30_60'); s60 = ('T' if h60 is True else ('F' if h60 is False else '?'))
    outcome = 'WIN' if t['pnl'] > 0 else 'LOSS'
    note = 'ORB' if t['orb'] else 'ML'
    print(f'  {t["date"]:<12} {t["ml"]:>5.2f} {outcome:>4}  {t["pnl"]:>7.0f}  {s15:>3} {s30:>3} {s60:>3}  {note}')
