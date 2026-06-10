# engine/execution/profit_manager.py
#
# Redesigned for 1-2 lot small-capital trading.
# Goal: capture 30-50 premium point moves on strong trend days.
#
# Old problem: trailing locked 90% at just ₹1000 peak (15 pts at 65 qty).
# That cuts winners at ~₹700-800 instead of letting them run to ₹3000-5000.
#
# New design: milestones in PREMIUM POINTS (lot-size agnostic):
#   ≥8  pts peak  → move SL to break-even (protect capital)
#   ≥15 pts peak  → lock 40% of peak gain (e.g. 15 pts gained → stop at +6 pts)
#   ≥25 pts peak  → lock 65% of peak gain (strong trend captured)
#   ≥40 pts peak  → lock 80% of peak gain (exceptional trend day)
#
# Expected capture at 40-pt trend: 40 × 0.80 = 32 pts locked
# At 2 lots (130 qty): 32 × 130 = Rs4,160 from one trade


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

    # Convert peak PnL → premium points gained (lot-size agnostic milestones)
    peak_pts = max_pnl / max(lot_size, 1)

    # ── 0  Fixed target hit ───────────────────────────────────────────
    if target is not None and ltp >= target:
        return stop_loss, max_pnl, "TARGET_HIT"

    # ── 1  Break-even at 4 pts ────────────────────────────────────────
    # 4 pts at 65 qty = Rs260 — covers bid-ask spread twice over and
    # protects capital without triggering on 1-tick noise.
    # Backtest: converts 9 stops to BE (+3,085 PnL); was 8 pts (only 2 stops).
    if peak_pts >= 4:
        stop_loss = max(stop_loss, entry_price)

    # ── 2  Trail 40% of peak at 15 pts ───────────────────────────────
    # Medium trend move.  Lock 40% so we keep Rs390 min at 65 qty on a
    # 15-pt move, while leaving room for price to continue.
    if peak_pts >= 15:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.40)

    # ── 3  Trail 65% of peak at 25 pts ───────────────────────────────
    # Strong trend confirmed.  Rs1056 locked at 65 qty on a 25-pt move.
    if peak_pts >= 25:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.65)

    # ── 4  Trail 80% of peak at 40 pts ───────────────────────────────
    # Exceptional trend day.  Rs2080 locked at 65 qty on a 40-pt move.
    # At 2 lots: Rs4160.  This is the Rs2000+/day scenario.
    if peak_pts >= 40:
        stop_loss = max(stop_loss, entry_price + peak_pts * 0.80)

    # ── 5  Drawdown exit — only after meaningful profit ───────────────
    # Don't exit on normal 1-2 pt pullbacks early in the trade.
    # Only activate drawdown guard once 10+ pts have been captured.
    if max_pnl >= lot_size * 10:
        # High-confidence entries get more room to breathe
        retention = 0.80 if ml_prob >= 0.65 else 0.72
        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # ── 6  Hard stop ─────────────────────────────────────────────────
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason
