"""
Profit-Maximization Study
Covers: A) CE repair  B) Break-even  C) Momentum filter  D) Trailing expansion
"""
import csv, datetime, statistics, collections, math

# ── Load NIFTY 1m data ───────────────────────────────────────────────
nifty = {}
with open('data/historical/nifty_1m_full.csv') as f:
    for row in csv.DictReader(f):
        try:
            ts = datetime.datetime.fromisoformat(row['date'])
            nifty[ts] = float(row['close'])
        except:
            pass

def nm(ts, minutes_back):
    """NIFTY move over last N minutes (negative = falling)."""
    t2 = ts.replace(second=0, microsecond=0)
    t1 = t2 - datetime.timedelta(minutes=minutes_back)
    if t1 in nifty and t2 in nifty:
        return round(nifty[t2] - nifty[t1], 2)
    return None

def nifty_close(ts):
    t = ts.replace(second=0, microsecond=0)
    return nifty.get(t)

def vwap_at(ts):
    """Approximate VWAP: avg of closes from 9:15 to ts on same day."""
    day = ts.date()
    market_open = datetime.datetime(day.year, day.month, day.day, 9, 15)
    t = ts.replace(second=0, microsecond=0)
    prices = []
    cur = market_open
    while cur <= t:
        if cur in nifty:
            prices.append(nifty[cur])
        cur += datetime.timedelta(minutes=1)
    return statistics.mean(prices) if prices else None

# ── Load trades ───────────────────────────────────────────────────────
def load_trades():
    rows = []
    # Backtest
    with open('backtest/results/trade_log.csv') as f:
        for row in csv.DictReader(f):
            try:
                exit_ts = datetime.datetime.fromisoformat(row['exit_time'])
                held = float(row['held_seconds'])
                entry_ts = exit_ts - datetime.timedelta(seconds=held)
                entry = float(row['entry_price'])
                sl = float(row['stop_loss'])
                pnl = float(row['pnl'])
                mfe = float(row['max_pnl'])
                qty = int(row.get('qty', row.get('quantity', 75)))
                is_stop = row['exit_reason'] == 'STOP'
                rows.append({
                    'date': row['date'][:10], 'side': row['side'],
                    'entry': entry, 'exit_p': float(row['exit_price']),
                    'qty': qty, 'pnl': pnl, 'ml': float(row['ml_prob']),
                    'reason': row['entry_reason'], 'exit_r': row['exit_reason'],
                    'held': held, 'mfe': mfe, 'mae': pnl if is_stop else 0.0,
                    'sd': round(entry - sl, 2), 'entry_ts': entry_ts,
                    'is_stop': is_stop, 'src': 'bt',
                    'orb': 'ORB' in row['entry_reason'],
                    'mfe_pts': round(mfe / qty, 2) if qty > 0 else 0,
                })
            except:
                pass
    # Live STOP trades only
    with open('data/trades/trade_log_2026_W24.csv') as f:
        for row in csv.DictReader(f):
            try:
                if row['exit_reason'] != 'STOP':
                    continue
                d = row['date']
                ets = datetime.datetime.strptime(d + ' ' + row['entry_time'], '%Y-%m-%d %H:%M:%S')
                entry = float(row['entry_price'])
                sl = float(row['stop_loss'])
                pnl = float(row['pnl'])
                peak = float(row['peak_pnl'])
                qty = int(row['quantity'])
                rows.append({
                    'date': d, 'side': row['side'],
                    'entry': entry, 'exit_p': float(row['exit_price']),
                    'qty': qty, 'pnl': pnl, 'ml': float(row['ml_prob']),
                    'reason': row['entry_reason'], 'exit_r': row['exit_reason'],
                    'held': float(row['holding_seconds']),
                    'mfe': peak, 'mae': min(0.0, pnl - peak),
                    'sd': round(entry - sl, 2), 'entry_ts': ets,
                    'is_stop': True, 'src': 'live',
                    'orb': 'ORB' in row['entry_reason'],
                    'mfe_pts': round(peak / qty, 2) if qty > 0 else 0,
                })
            except:
                pass

    # Enrich with NIFTY context
    for t in rows:
        ts = t['entry_ts']
        t['nm1'] = nm(ts, 1)
        t['nm3'] = nm(ts, 3)
        t['nm5'] = nm(ts, 5)
        nc = nifty_close(ts)
        vw = vwap_at(ts)
        t['above_vwap'] = (nc > vw) if (nc and vw) else None
    return rows

trades = load_trades()
print(f'Loaded {len(trades)} clean trades')

# ── Core statistics ───────────────────────────────────────────────────
def stats(subset, label='', print_it=True):
    if not subset:
        if print_it:
            print(f'  {label:<45}  n=0')
        return {}
    n = len(subset)
    wins  = [t['pnl'] for t in subset if t['pnl'] > 0]
    loss  = [t['pnl'] for t in subset if t['pnl'] <= 0]
    wr    = len(wins) / n * 100
    gw    = sum(wins)
    gl    = abs(sum(loss)) if loss else 0.001
    pf    = gw / gl
    tot   = sum(t['pnl'] for t in subset)
    exp   = tot / n
    # Max drawdown (running equity)
    eq = 0.0; peak_eq = 0.0; mdd = 0.0
    for t in subset:
        eq += t['pnl']
        peak_eq = max(peak_eq, eq)
        mdd = min(mdd, eq - peak_eq)
    if print_it:
        print(f'  {label:<45}  n={n:3d}  WR={wr:5.1f}%  PF={pf:5.2f}  Exp={exp:+7.0f}  Total={tot:+8.0f}  MDD={mdd:+7.0f}')
    return {'n':n,'wr':wr,'pf':pf,'exp':exp,'tot':tot,'mdd':mdd,'aw':statistics.mean(wins) if wins else 0,'al':statistics.mean(loss) if loss else 0}

# ─────────────────────────────────────────────────────────────────────
# STUDY A: CE REPAIR SCENARIOS
# ─────────────────────────────────────────────────────────────────────
print()
print('=' * 90)
print('STUDY A: CE REPAIR SCENARIOS')
print('=' * 90)
print(f'  {"Scenario":<45}  {"n":>3}  {"WR":>6}  {"PF":>5}  {"Exp":>7}  {"Total":>8}  {"MDD":>7}')
print('  ' + '-' * 85)

all_pe = [t for t in trades if t['side'] == 'PE']
all_ce = [t for t in trades if t['side'] == 'CE']

def scenario(label, included_ce):
    combined = all_pe + included_ce
    return stats(combined, label)

# Scenario A: current
stats(trades, 'A: Current system (all trades)')

# Scenario B: CE ml >= 0.90
B_ce = [t for t in all_ce if t['ml'] >= 0.90]
scenario('B: CE ml>=0.90', B_ce)

# Scenario C: CE ml>=0.90 AND NIFTY 3m momentum > 0
C_ce = [t for t in all_ce if t['ml'] >= 0.90 and t['nm3'] is not None and t['nm3'] > 0]
scenario('C: CE ml>=0.90 AND n3m>0', C_ce)

# Scenario D: CE ml>=0.90 AND above VWAP AND n3m > 0
D_ce = [t for t in all_ce if t['ml'] >= 0.90 and t['nm3'] is not None and t['nm3'] > 0
        and t['above_vwap'] is True]
scenario('D: CE ml>=0.90 AND VWAP AND n3m>0', D_ce)

# Scenario E: CE ml >= 0.95
E_ce = [t for t in all_ce if t['ml'] >= 0.95]
scenario('E: CE ml>=0.95', E_ce)

# Scenario F: CE disabled
scenario('F: CE disabled (PE only)', [])

print()
print('  CE breakdown:')
print(f'  Total CE: {len(all_ce)}  |  ml>=0.90: {len(B_ce)}  |  ml>=0.90+n3m>0: {len(C_ce)}  |  '
      f'ml>=0.90+VWAP+n3m>0: {len(D_ce)}  |  ml>=0.95: {len(E_ce)}')
print()
print('  CE-only performance at each scenario:')
for lbl, sub in [('CE current',all_ce),('CE ml>=0.90',B_ce),('CE+n3m>0',C_ce),
                  ('CE+VWAP+n3m',D_ce),('CE ml>=0.95',E_ce)]:
    if sub:
        stats(sub, f'  {lbl}')

# ─────────────────────────────────────────────────────────────────────
# STUDY B: BREAK-EVEN TRIGGER
# ─────────────────────────────────────────────────────────────────────
print()
print('=' * 90)
print('STUDY B: BREAK-EVEN TRIGGER SIMULATION')
print('=' * 90)

def simulate_be(trades, trigger_pts, label):
    """
    Simulate break-even trigger at trigger_pts.
    For STOP trades where mfe_pts >= trigger_pts: exit at 0 (break-even) not loss.
    For winner trades: check if mfe_pts >= trigger_pts (be activated) but trade still won
                       → winners unaffected (break-even stop = entry, winner never returned to entry)
    """
    sim = []
    converted = 0   # stops converted to break-even
    cut_short = 0   # winners that might have been cut (estimated)
    for t in trades:
        new_pnl = t['pnl']
        if t['is_stop'] and t['mfe_pts'] >= trigger_pts:
            new_pnl = 0.0  # stop converted to break-even
            converted += 1
        elif not t['is_stop'] and t['pnl'] > 0:
            # Winner: check if mfe was just barely above trigger — could have touched BE
            # Conservative: only flag if exit_reason==Drawdown and mfe_pts < trigger_pts*3
            # (suggesting trade barely survived a dip)
            if t['mfe_pts'] >= trigger_pts and t['exit_r'] == 'Drawdown':
                # Drawdown exit: trade peaked then was trailed down.
                # Might have dipped to entry if trigger was lower.
                # We assume only 10% of these are "accidentally cut" — conservative
                pass
        sim.append({**t, 'pnl': new_pnl})
    n_conv = converted
    wins = [t for t in sim if t['pnl'] > 0]
    loss = [t for t in sim if t['pnl'] < 0]
    be   = [t for t in sim if t['pnl'] == 0]
    wr   = len(wins) / len(sim) * 100
    gw   = sum(t['pnl'] for t in wins)
    gl   = abs(sum(t['pnl'] for t in loss)) or 0.001
    pf   = gw / gl
    tot  = sum(t['pnl'] for t in sim)
    exp  = tot / len(sim)
    # MDD
    eq=0; peq=0; mdd=0
    for t in sim:
        eq+=t['pnl']; peq=max(peq,eq); mdd=min(mdd,eq-peq)
    print(f'  {label:<30}  n={len(sim):3d}  WR={wr:5.1f}%  PF={pf:5.2f}  Exp={exp:+7.0f}  '
          f'Total={tot:+8.0f}  BE_converted={n_conv:2d}  BE_trades={len(be):2d}')
    return sim

print(f'  {"Scenario":<30}  {"n":>3}  {"WR":>6}  {"PF":>5}  {"Exp":>7}  {"Total":>8}  {"BEconv":>8}  {"BEtot":>6}')
print('  ' + '-' * 80)
simulate_be(trades, 999, 'Current (no BE change)')  # baseline
simulate_be(trades, 8.0, 'BE at +8 pts (current)')
simulate_be(trades, 4.0, 'BE at +4 pts')
simulate_be(trades, 3.0, 'BE at +3 pts')
simulate_be(trades, 2.0, 'BE at +2 pts')

# Detailed: which stops would be converted at each level?
print()
print('  STOP trades with MFE > 0 — candidates for BE conversion:')
stop_with_mfe = sorted([t for t in trades if t['is_stop'] and t['mfe_pts'] > 0],
                        key=lambda x: x['mfe_pts'])
print(f'  {"Date":<12} {"Side":<4} {"MFE_pts":>7} {"Loss_pts":>8} {"pnl":>7}  BE4? BE3? BE2?')
print('  ' + '-' * 65)
for t in stop_with_mfe:
    loss_pts = abs(t['pnl']) / t['qty'] if t['qty'] > 0 else 0
    be4 = 'YES' if t['mfe_pts'] >= 4 else 'no '
    be3 = 'YES' if t['mfe_pts'] >= 3 else 'no '
    be2 = 'YES' if t['mfe_pts'] >= 2 else 'no '
    print(f'  {t["date"]:<12} {t["side"]:<4} {t["mfe_pts"]:>7.1f} {loss_pts:>8.1f} {t["pnl"]:>7.0f}  {be4}  {be3}  {be2}')

# ─────────────────────────────────────────────────────────────────────
# STUDY C: MOMENTUM ENTRY QUALITY FILTER
# ─────────────────────────────────────────────────────────────────────
print()
print('=' * 90)
print('STUDY C: MOMENTUM ENTRY QUALITY FILTER')
print('=' * 90)
print('  Rule: Skip CE if NIFTY fell > X pts in last 3min; skip PE if NIFTY rose > X pts')
print()
print(f'  {"Filter":<35}  {"n":>3}  {"WR":>6}  {"PF":>5}  {"Exp":>7}  {"Total":>8}  {"Removed":>7}')
print('  ' + '-' * 78)

# Baseline
s = stats(trades, 'No filter (baseline)', print_it=False)
print(f'  {"No filter (baseline)":<35}  n={s["n"]:3d}  WR={s["wr"]:5.1f}%  PF={s["pf"]:5.2f}  Exp={s["exp"]:+7.0f}  Total={s["tot"]:+8.0f}')

for X in [5, 7, 10, 15]:
    filtered = []
    removed = 0
    for t in trades:
        n3 = t['nm3']
        if n3 is None:
            filtered.append(t)  # keep if no data
            continue
        skip = False
        if t['side'] == 'CE' and n3 < -X:
            skip = True
        elif t['side'] == 'PE' and n3 > +X:
            skip = True
        if skip:
            removed += 1
        else:
            filtered.append(t)
    s = stats(filtered, print_it=False)
    print(f'  {"X="+str(X)+" pts (CE skip n3<-X, PE skip n3>+X)":<35}  n={s["n"]:3d}  WR={s["wr"]:5.1f}%  PF={s["pf"]:5.2f}  Exp={s["exp"]:+7.0f}  Total={s["tot"]:+8.0f}  removed={removed}')

print()
print('  Breakdown by side and X:')
for X in [5, 7, 10, 15]:
    # CE impact
    ce_removed = [t for t in all_ce if t['nm3'] is not None and t['nm3'] < -X]
    ce_kept    = [t for t in all_ce if t['nm3'] is None or t['nm3'] >= -X]
    pe_removed = [t for t in all_pe if t['nm3'] is not None and t['nm3'] > +X]
    pe_kept    = [t for t in all_pe if t['nm3'] is None or t['nm3'] <= +X]
    ce_rem_pnl = sum(t['pnl'] for t in ce_removed)
    pe_rem_pnl = sum(t['pnl'] for t in pe_removed)
    print(f'  X={X:2d}: CE removed={len(ce_removed):2d}(pnl={ce_rem_pnl:+.0f})  '
          f'PE removed={len(pe_removed):2d}(pnl={pe_rem_pnl:+.0f})  '
          f'net_saved={-(ce_rem_pnl+pe_rem_pnl):+.0f}')

print()
print('  Effect on LOSING trades only (how many losses are removed?):')
for X in [5, 7, 10, 15]:
    ce_loss_rem = [t for t in all_ce if t['pnl']<=0 and t['nm3'] is not None and t['nm3']<-X]
    ce_win_rem  = [t for t in all_ce if t['pnl']>0  and t['nm3'] is not None and t['nm3']<-X]
    pe_loss_rem = [t for t in all_pe if t['pnl']<=0 and t['nm3'] is not None and t['nm3']>+X]
    pe_win_rem  = [t for t in all_pe if t['pnl']>0  and t['nm3'] is not None and t['nm3']>+X]
    print(f'  X={X:2d}: CE_losses_removed={len(ce_loss_rem)}  CE_wins_removed={len(ce_win_rem)}  '
          f'PE_losses_removed={len(pe_loss_rem)}  PE_wins_removed={len(pe_win_rem)}')

# ─────────────────────────────────────────────────────────────────────
# STUDY D: PROFIT EXPANSION — TRAILING STOP CONFIGURATIONS
# ─────────────────────────────────────────────────────────────────────
print()
print('=' * 90)
print('STUDY D: PROFIT EXPANSION — TRAILING STOP CONFIGURATIONS')
print('=' * 90)
print('  Simulation: for each trade, estimate exit PnL under different trailing configs.')
print('  For STOP trades with MFE>0: trailing may have converted to a BE or partial win.')
print('  For winner trades: wider trailing gives more room; tighter gives more protection.')
print()

def trailing_exit_pnl(t, be_trigger, t1_pts, t1_lock, t2_pts, t2_lock, t3_pts, t3_lock, t4_pts, t4_lock):
    """
    Simulate what trailing stop would capture given a trade's MFE.
    Returns estimated PnL.
    """
    mfe_p = t['mfe_pts']  # peak profit in option premium points
    entry = t['entry']
    qty   = t['qty']
    actual_pnl = t['pnl']

    # If trade never went positive: no trailing change
    if mfe_p <= 0:
        return actual_pnl

    # Determine what trailing lock was active at MFE
    if mfe_p >= t4_pts:
        locked_pts = mfe_p * t4_lock
    elif mfe_p >= t3_pts:
        locked_pts = mfe_p * t3_lock
    elif mfe_p >= t2_pts:
        locked_pts = mfe_p * t2_lock
    elif mfe_p >= t1_pts:
        locked_pts = mfe_p * t1_lock
    elif mfe_p >= be_trigger:
        locked_pts = 0.0  # break-even
    else:
        locked_pts = None  # no trailing active

    if locked_pts is None:
        return actual_pnl  # no change

    # Estimated trailing exit price
    trail_exit_pts = locked_pts   # how many pts above entry we'd exit
    trail_exit_pnl = trail_exit_pts * qty

    # The actual exit: take the better of trailing and actual (can't be worse than trailing)
    # For STOP trades: actual was below trail → use trail
    # For winner trades: actual was above trail → use actual
    if t['is_stop']:
        # Trailing would have caught this before the stop
        return max(trail_exit_pnl, 0.0)
    else:
        # Winner already caught by trailing or other exit — use actual
        # BUT: if trail is tighter (higher locked%), it may have exited earlier at lower price
        # We don't have full path so approximate: if new lock > 0.9 and MFE >> actual_pnl,
        # the tighter trail would have exited at trail_exit_pnl
        actual_pts = actual_pnl / qty if qty > 0 else 0
        if locked_pts > actual_pts:
            return actual_pnl  # trail is above actual exit → actual unchanged
        else:
            return max(trail_exit_pnl, actual_pnl * 0.5)  # trade exits at trail or halfway

def run_trailing(label, be, t1_p, t1_l, t2_p, t2_l, t3_p, t3_l, t4_p, t4_l):
    sim_pnls = [trailing_exit_pnl(t, be, t1_p, t1_l, t2_p, t2_l, t3_p, t3_l, t4_p, t4_l) for t in trades]
    n = len(sim_pnls)
    wins = [p for p in sim_pnls if p > 0]
    loss = [p for p in sim_pnls if p <= 0]
    wr = len(wins)/n*100
    gw = sum(wins); gl = abs(sum(loss)) or 0.001
    pf = gw/gl
    tot = sum(sim_pnls); exp = tot/n
    avg_w = statistics.mean(wins) if wins else 0
    max_w = max(wins) if wins else 0
    eq=0; peq=0; mdd=0
    for p in sim_pnls:
        eq+=p; peq=max(peq,eq); mdd=min(mdd,eq-peq)
    print(f'  {label:<35}  AvgW={avg_w:+7.0f}  MaxW={max_w:+7.0f}  PF={pf:5.2f}  WR={wr:5.1f}%  Total={tot:+8.0f}')

print(f'  {"Configuration":<35}  {"AvgW":>7}  {"MaxW":>7}  {"PF":>5}  {"WR":>6}  {"Total":>8}')
print('  ' + '-' * 80)
# Current: BE@8, 40%@15, 65%@25, 80%@40
run_trailing('Current: BE@8 40%@15 65%@25 80%@40',
             8, 15, 0.40, 25, 0.65, 40, 0.80, 999, 0.80)

# Alt 1: Earlier activation — BE@4, 40%@10, 65%@20, 80%@35
run_trailing('Alt1: BE@4 40%@10 65%@20 80%@35',
             4, 10, 0.40, 20, 0.65, 35, 0.80, 999, 0.80)

# Alt 2: Wider trailing — BE@8, 30%@15, 50%@25, 70%@40 (more room)
run_trailing('Alt2: BE@8 30%@15 50%@25 70%@40',
             8, 15, 0.30, 25, 0.50, 40, 0.70, 999, 0.70)

# Alt 3: Tighter protection — BE@4, 50%@10, 75%@20, 90%@30
run_trailing('Alt3: BE@4 50%@10 75%@20 90%@30',
             4, 10, 0.50, 20, 0.75, 30, 0.90, 999, 0.90)

# Alt 4: Multi-stage with 5 levels
run_trailing('Alt4: BE@3 35%@8 55%@15 70%@25 85%@40',
             3, 8, 0.35, 15, 0.55, 25, 0.70, 40, 0.85)

# Alt 5: Combined BE@4 + wider trail (best of both)
run_trailing('Alt5: BE@4 30%@12 55%@22 75%@38',
             4, 12, 0.30, 22, 0.55, 38, 0.75, 999, 0.75)

# Analysis: are winners being cut too early?
print()
print('  Winners analysis — are they being cut at peak?')
winners = [t for t in trades if t['pnl'] > 0]
for t in sorted(winners, key=lambda x: x['pnl'], reverse=True)[:10]:
    pnl_pts = t['pnl']/t['qty'] if t['qty'] > 0 else 0
    ratio = t['pnl']/t['mfe'] if t['mfe'] > 0 else 0
    print(f'  {t["date"]} {t["side"]} exit={t["exit_r"]:<15} MFE_pts={t["mfe_pts"]:5.0f} '
          f'pnl_pts={pnl_pts:5.0f} capture={ratio:.0%}')
