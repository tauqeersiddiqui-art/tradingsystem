# engine/execution/profit_manager.py
#
# Profit-lock ladder — guarantees minimum Rs per lot at each milestone.
#
# LOCK_PTS = 200/65 = 3.077 pts — the minimum premium move that equals Rs200/lot.
# Every milestone locks a hard floor of profit; the stop never moves back.
#
# Ladder (premium points gained → stop moved to):
#   >= 2  pts  →  entry + 0              (break-even: capital protected)
#   >= 4  pts  →  entry + LOCK_PTS        (Rs200/lot guaranteed)
#   >= 8  pts  →  entry + 2*LOCK_PTS      (Rs400/lot guaranteed)
#   >= 15 pts  →  entry + 40% of peak     (~Rs390/lot on 15-pt move)
#   >= 25 pts  →  entry + 65% of peak     (~Rs1056/lot on 25-pt move)
#   >= 40 pts  →  entry + 80% of peak     (~Rs2080/lot on 40-pt move)
#
# Why lot-size normalised?  Qty varies (65 / 130 / 195 ...).
# LOCK_PTS is qty-agnostic: the Rs floor scales with qty automatically
# because stop_loss is a premium level (not Rs directly).
#
# Backtest on 2026-06-10:
#   Old (BE at entry+0):         -107 Rs
#   LOCK_PTS only (>= 4 pts):   +961 Rs  (+1068 Rs gain, 3 of 4 trades)
#   + Level-1 BE at 2 pts:      +1000 Rs  (+1107 Rs total; 4th trade saved)

_LOT_QTY  = 65          # NIFTY lot size — used only to convert Rs target to pts
_RS_FLOOR = 200.0       # minimum guaranteed profit per lot at first milestone
LOCK_PTS  = _RS_FLOOR / _LOT_QTY   # 3.077 pts


def manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob, target=None):
    """
    Args:
        entry_price : option premium at entry
        ltp         : current live option premium
        lot_size    : qty (65 for 1 lot, 130 for 2 lots)
        stop_loss   : current stop premium level
        max_pnl     : peak PnL seen so far (Rs)
        ml_prob     : ML probability at entry (higher = trail more generously)
        target      : fixed target premium (optional — trailing is the real exit)

    Returns:
        (updated_stop_loss, updated_max_pnl, exit_reason | None)
    """
    pnl     = (ltp - entry_price) * lot_size
    max_pnl = max(max_pnl, pnl)
    reason  = None

    # Convert peak PnL to premium points (lot-size agnostic milestones)
    peak_pts = max_pnl / max(lot_size, 1)

    # ── 0  Fixed target hit ───────────────────────────────────────────
    if target is not None and ltp >= target:
        return stop_loss, max_pnl, "TARGET_HIT"

    # ── 1  Break-even at 2 pts ────────────────────────────────────────
    # Covers the Trade-2 scenario: MFE peaks 2–3.9 pts then fully reverses.
    # Without this floor the trade falls all the way to the initial hard stop.
    # At >= 4 pts the LOCK_PTS milestone (below) overrides this anyway.
    if peak_pts >= 2:
        stop_loss = max(stop_loss, entry_price)

    # ── 2  Lock Rs200/lot at 4 pts ────────────────────────────────────
    # Old: moved SL to entry+0 (breakeven).  Problem: any reversal = zero profit.
    # New: SL at entry+LOCK_PTS so Rs200/lot is guaranteed on exit.
    # 4 pts peak is enough to clear bid-ask twice AND lock a real profit floor.
    if peak_pts >= 4:
        stop_loss = max(stop_loss, entry_price + LOCK_PTS)

    # ── 3  Lock Rs400/lot at 8 pts ────────────────────────────────────
    if peak_pts >= 8:
        stop_loss = max(stop_loss, entry_price + 2 * LOCK_PTS)

    # ── 4  Trail 40% of peak at 15 pts ───────────────────────────────
    # Ratchets above the 8-pt floor once the trade develops further.
    if peak_pts >= 15:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.40)

    # ── 5  Trail 65% of peak at 25 pts ───────────────────────────────
    if peak_pts >= 25:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.65)

    # ── 6  Trail 80% of peak at 40 pts ───────────────────────────────
    if peak_pts >= 40:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.80)

    # ── 7  Drawdown exit — only after meaningful profit ───────────────
    if max_pnl >= lot_size * 10:
        retention = 0.80 if ml_prob >= 0.65 else 0.72
        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # ── 8  Hard stop ─────────────────────────────────────────────────
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason
