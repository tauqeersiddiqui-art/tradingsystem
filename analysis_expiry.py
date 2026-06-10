import csv, datetime, statistics

def is_expiry_day_backtest(date_str):
    return datetime.date.fromisoformat(date_str).weekday() == 3

def parse_expiry_from_symbol(sym):
    try:
        s = sym.replace('NIFTY','')
        yy = s[:2]; rest = s[2:]
        if rest[:2] in ('10','11','12'):
            mm = int(rest[:2]); dd = rest[2:4]
        else:
            mm = int(rest[0]); dd = rest[1:3]
        return '20%s-%02d-%s' % (yy, mm, dd)
    except:
        return None

bt = []
with open('backtest/results/trade_log.csv') as f:
    for row in csv.DictReader(f):
        try:
            d = row['date'][:10]
            entry = float(row['entry_price'])
            sl    = float(row['stop_loss'])
            sd    = round(entry - sl, 3)
            is_stop = row['exit_reason'] == 'STOP'
            mfe_v   = float(row['max_pnl'])
            bt.append({
                'date': d, 'side': row['side'], 'entry': entry,
                'qty': int(row.get('qty', row.get('quantity', 75))),
                'pnl': float(row['pnl']), 'ml': float(row['ml_prob']),
                'reason': row['entry_reason'], 'exit_r': row['exit_reason'],
                'held': float(row['held_seconds']),
                'mfe': mfe_v if mfe_v >= 0 else 0,
                'mae': float(row['pnl']) if is_stop else None,
                'sd': sd, 'source': 'backtest',
                'expiry': is_expiry_day_backtest(d),
                'is_orb': 'ORB' in row['entry_reason'],
                'stop_hit': is_stop,
            })
        except:
            pass

live = []
with open('data/trades/trade_log_2026_W24.csv') as f:
    for row in csv.DictReader(f):
        try:
            d = row['date']
            sym = row['symbol']
            exp_date = parse_expiry_from_symbol(sym)
            on_e = (exp_date == d)
            entry = float(row['entry_price'])
            sl    = float(row['stop_loss'])
            sd    = round(entry - sl, 3)
            is_stop = row['exit_reason'] == 'STOP'
            peak = float(row['peak_pnl'])
            pnl  = float(row['pnl'])
            live.append({
                'date': d, 'side': row['side'], 'entry': entry,
                'qty': int(row['quantity']),
                'pnl': pnl, 'ml': float(row['ml_prob']),
                'reason': row['entry_reason'], 'exit_r': row['exit_reason'],
                'held': float(row['holding_seconds']),
                'mfe': peak if is_stop else None,
                'mae': min(0.0, pnl - peak) if is_stop else None,
                'sd': sd, 'source': 'live',
                'expiry': on_e,
                'is_orb': 'ORB' in row['entry_reason'],
                'stop_hit': is_stop,
            })
        except:
            pass

all_trades = bt + live

def M(subset, label, indent=0):
    pad = ' ' * indent
    if not subset:
        print('%s%-44s  n=  0  (insufficient data)' % (pad, label))
        return {}
    n    = len(subset)
    wins = [t['pnl'] for t in subset if t['pnl'] > 0]
    loss = [t['pnl'] for t in subset if t['pnl'] <= 0]
    wr   = len(wins) / n
    aw   = statistics.mean(wins) if wins else 0
    al   = statistics.mean(loss) if loss else 0
    tot  = sum(t['pnl'] for t in subset)
    exp  = tot / n
    gw   = sum(wins)
    gl   = abs(sum(loss)) if loss else 0.01
    pf   = gw / gl
    held_v = [t['held'] for t in subset if t['held'] is not None]
    avg_h  = statistics.mean(held_v) if held_v else 0
    mfe_v  = [t['mfe'] for t in subset if t.get('mfe') is not None]
    mae_v  = [t['mae'] for t in subset if t.get('mae') is not None]
    avg_mfe = statistics.mean(mfe_v) if mfe_v else 0
    avg_mae = statistics.mean(mae_v) if mae_v else 0
    sh      = sum(1 for t in subset if t['stop_hit']) / n
    print('%s%-44s n=%3d WR=%5.1f%% exp=%+6.0f PF=%5.2f SH=%4.1f%% hold=%5.0fs MFE=%+7.0f MAE=%+7.0f' % (
        pad, label, n, wr*100, exp, pf, sh*100, avg_h, avg_mfe, avg_mae))
    return dict(n=n, wr=wr, exp=exp, pf=pf, sh=sh, avg_h=avg_h,
                avg_mfe=avg_mfe, avg_mae=avg_mae, tot=tot, aw=aw, al=al)

HDR = '  %-44s %s' % ('Group', 'n    WR      exp     PF    SH%   hold    MFE     MAE')
SEP = '  ' + '-'*96

print()
print('=' * 98)
print('EXPIRY vs NON-EXPIRY  |  backtest(116) + live W24(25)  =  141 trades total')
print('=' * 98)
print(HDR); print(SEP)
M(all_trades, 'ALL TRADES')
print()
M([t for t in all_trades if t['expiry']],     'EXPIRY DAY',     2)
M([t for t in all_trades if not t['expiry']], 'NON-EXPIRY DAY', 2)

print()
print('=' * 98)
print('SPLIT BY TRADE TYPE  x  EXPIRY STATUS')
print('=' * 98)
print(HDR); print(SEP)
print('  -- ORB Breakout Trades --')
M([t for t in all_trades if t['is_orb'] and     t['expiry']],  'ORB  | Expiry',     4)
M([t for t in all_trades if t['is_orb'] and not t['expiry']],  'ORB  | Non-Expiry', 4)
print('  -- Pure ML Trades --')
M([t for t in all_trades if not t['is_orb'] and     t['expiry']],  'ML   | Expiry',     4)
M([t for t in all_trades if not t['is_orb'] and not t['expiry']], 'ML   | Non-Expiry', 4)

print()
print('=' * 98)
print('SPLIT BY SIDE  x  EXPIRY STATUS')
print('=' * 98)
print(HDR); print(SEP)
print('  -- CE Trades --')
M([t for t in all_trades if t['side']=='CE' and     t['expiry']],  'CE   | Expiry',     4)
M([t for t in all_trades if t['side']=='CE' and not t['expiry']], 'CE   | Non-Expiry', 4)
print('  -- PE Trades --')
M([t for t in all_trades if t['side']=='PE' and     t['expiry']],  'PE   | Expiry',     4)
M([t for t in all_trades if t['side']=='PE' and not t['expiry']], 'PE   | Non-Expiry', 4)

print()
print('=' * 98)
print('FOUR-WAY:  SIDE x TYPE x EXPIRY')
print('=' * 98)
print(HDR); print(SEP)
for side in ['CE','PE']:
    for orb in [True, False]:
        tag = 'ORB' if orb else 'ML'
        e  = [t for t in all_trades if t['side']==side and t['is_orb']==orb and     t['expiry']]
        ne = [t for t in all_trades if t['side']==side and t['is_orb']==orb and not t['expiry']]
        if e or ne:
            print('  -- %s  %s --' % (side, tag))
            M(e,  '%s+%s | Expiry'       % (side, tag), 4)
            M(ne, '%s+%s | Non-Expiry'   % (side, tag), 4)

print()
print('=' * 98)
print('SOURCE SPLIT: backtest Thursday vs live June-09  (same expiry label, different data quality)')
print('=' * 98)
print(HDR); print(SEP)
M([t for t in bt   if     t['expiry']],  'Backtest | Thursday (expiry)',     2)
M([t for t in bt   if not t['expiry']],  'Backtest | Non-Thursday',          2)
M([t for t in live if     t['expiry']],  'Live     | 2026-06-09 (expiry)',    2)
M([t for t in live if not t['expiry']],  'Live     | Non-expiry (none today)', 2)

print()
print('=' * 98)
print('STOP ANALYSIS: expiry vs non-expiry')
print('=' * 98)
print(HDR); print(SEP)
for exp_flag, label in [(True,'Expiry'), (False,'Non-Expiry')]:
    sub = [t for t in all_trades if t['expiry']==exp_flag]
    stops = [t for t in sub if t['stop_hit']]
    non_s = [t for t in sub if not t['stop_hit']]
    print('  -- %s --' % label)
    M(stops, 'STOP exits  | %s' % label, 4)
    M(non_s, 'Other exits | %s' % label, 4)

print()
print('=' * 98)
print('MFE=0 RATE (straight-down losses): expiry vs non-expiry STOP trades')
print('=' * 98)
for exp_flag, label in [(True,'Expiry'), (False,'Non-Expiry')]:
    stops = [t for t in all_trades if t['expiry']==exp_flag and t['stop_hit']]
    if not stops:
        print('  %s: no STOP trades' % label); continue
    zero_mfe = [t for t in stops if t.get('mfe') is not None and t['mfe'] <= 0]
    pos_mfe  = [t for t in stops if t.get('mfe') is not None and t['mfe'] > 0]
    print('  %-15s STOP n=%2d  MFE=0 (straight down): %2d (%4.1f%%)  MFE>0 (recovered then stopped): %2d (%4.1f%%)' % (
        label+':', len(stops),
        len(zero_mfe), 100*len(zero_mfe)/len(stops) if stops else 0,
        len(pos_mfe),  100*len(pos_mfe)/len(stops)  if stops else 0))

print()
print('Expiry calendar: backtest Thursday dates (%d total):' % sum(1 for t in bt if t['expiry']))
exp_dates = sorted(set(t['date'] for t in bt if t['expiry']))
for i in range(0, len(exp_dates), 6):
    print('  ', exp_dates[i:i+6])
print('Live expiry date:  2026-06-09  (day_of_week=%d, Tuesday=1, Thursday=3)' %
      datetime.date.fromisoformat('2026-06-09').weekday())
