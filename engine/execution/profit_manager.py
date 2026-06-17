# engine/execution/profit_manager.py
#
# Centralized profit-lock ladder — the SINGLE source of truth for trailing
# stops on BOTH normal ML trades and scalp trades.  master_runner applies the
# same ladder_stop() to scalp positions, so behaviour is identical.
#
# ─────────────────────────────────────────────────────────────────────────
# VIRTUAL STOP SEMANTICS — READ THIS:
#   stop_loss is a *trigger level* in option-premium space.  It is evaluated
#   once per engine cycle (~1s) against the latest LTP.  There is NO resting
#   SL-M order at the broker.  When ltp <= stop_loss the engine fires a
#   MARKET exit, so the realised fill can be BELOW the trigger when the option
#   gaps or moves fast between polls.  An exit price under the locked level is
#   therefore expected slippage, NOT a logic error.  master_runner emits a
#   [SLIPPAGE] warning when the gap exceeds SLIPPAGE_WARN_PTS.
# ─────────────────────────────────────────────────────────────────────────
#
# Ladder — uses max_pnl (Rs, on the ACTUAL position qty) as the source of truth:
#   MFE >= Rs2000  ->  lock 80% of peak profit
#   MFE >= Rs1200  ->  lock 70% of peak profit
#   MFE >= Rs 800  ->  lock Rs600
#   MFE >= Rs 500  ->  lock Rs350
#   MFE >= Rs 390  ->  lock Rs195   (~+3pt for 65-lot, net-positive floor)
#   MFE >= Rs 195  ->  lock Rs 65   (~+1pt for 65-lot, cost-recovery floor)
#   MFE >= Rs 130  ->  lock Rs 32   (~+0.5pt for 65-lot, slippage-proof entry)
#
# RULES (enforced by construction):
#   * Stop only TIGHTENS (max() ratchet) — it can never loosen.
#   * CE and PE behave identically (both are long-premium positions).
#   * Scalp and normal behave identically (same ladder_stop()).
#   * max_pnl is the only profit reference used.

import logging

logger = logging.getLogger("profit_manager")

# Retained for backward-compat (telegram.messages imports LOCK_PTS).
_LOT_QTY  = 65
_RS_FLOOR = 200.0
LOCK_PTS  = _RS_FLOOR / _LOT_QTY   # 3.077 pts

# ─────────────────────────────────────────────────────────────────────────
# COST-AWARE PROFIT LADDER  (replaces the old fixed Rs rungs)
#
# OLD BUG: bottom rungs locked Rs32 / Rs65 — BELOW the ~Rs66/lot round-trip
# cost. Every trade that locked there was a GUARANTEED net loss after costs
# (e.g. lock Rs32 on a 130-qty trade = Rs132 cost = -Rs100 net). It also
# fired on just Rs130 MFE (1 point), so normal noise stopped trades for a
# guaranteed small loss.
#
# NEW DESIGN (two guarantees):
#   1. No lock is EVER below cost  -> a locked exit is break-even or better,
#      never a guaranteed loss.
#   2. First lock only arms once MFE >= 1.5x cost  -> noise no longer trips it.
# Above cost-recovery it trails ~62% of the running peak, so a trade that
# peaks at +Rs442 keeps ~Rs274 instead of the old hard Rs195 cap.
#
# Worst-case loss is UNCHANGED: until the first lock arms, the stop stays at
# the initial risk_manager stop. The ladder only ever TIGHTENS the stop.
# ─────────────────────────────────────────────────────────────────────────

_COST_PER_LOT = 66.0     # round-trip cost per 65-qty lot (overridable via env)
_LOT_UNITS    = 65
_TRAIL_PCT    = 0.62     # fraction of peak profit retained once cost recovered


def _cost_rs(qty: int) -> float:
    """Round-trip cost in Rs for a given position qty (scales by lots)."""
    import os
    cost_per_lot = float(os.getenv("COST_PER_LOT", _COST_PER_LOT))
    lots = max(1, round(qty / _LOT_UNITS))
    return lots * cost_per_lot


def ladder_locked_rs(max_pnl: float, qty: int = _LOT_UNITS):
    """
    Return (locked_profit_rs, stage_label) for the current peak PnL in Rs.

    Cost-aware: never returns a lock below the trade's round-trip cost, and
    returns 0 (no lock) until MFE clears 1.5x cost so noise can't trip it.
    """
    cost = _cost_rs(qty)

    # Not enough cushion yet — rely on the initial stop (no early lock).
    if max_pnl < cost * 1.5:
        return 0.0, "INITIAL"

    # Trail a fraction of the peak, floored at break-even-after-cost.
    locked = max(_TRAIL_PCT * max_pnl, cost)

    # Lock more aggressively when deep in profit (protect large winners).
    if   max_pnl >= 2000.0:
        locked = max(locked, 0.80 * max_pnl)
        stage  = "S6_LOCK80%"
    elif max_pnl >= 1200.0:
        locked = max(locked, 0.70 * max_pnl)
        stage  = "S5_LOCK70%"
    elif max_pnl >= 800.0:
        locked = max(locked, 0.65 * max_pnl)
        stage  = "S4_TRAIL65%"
    else:
        stage  = "S1_COSTLOCK" if locked <= cost + 1e-6 else "S2_TRAIL62%"

    locked = min(locked, max_pnl)   # never lock more than the peak itself
    return locked, stage


def ladder_stop(entry_price, qty, max_pnl, current_stop):
    """
    Convert the rupee profit-lock to a premium stop level and ratchet UP only.

    Returns (new_stop, stage_label, locked_rs).
    Used by BOTH manage_position (normal trades) and the scalp loop.
    """
    locked_rs, stage = ladder_locked_rs(max_pnl, qty)
    if locked_rs <= 0:
        return current_stop, stage, 0.0
    stop_floor = entry_price + locked_rs / max(qty, 1)
    new_stop   = max(current_stop, stop_floor)   # never loosen
    return new_stop, stage, locked_rs


def manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob, target=None):
    """
    Args:
        entry_price : option premium at entry
        ltp         : current live option premium
        lot_size    : ACTUAL position quantity (total units, e.g. 65 / 130).
                      Named lot_size for backward-compat; callers MUST pass
                      position["qty"] so MFE/max_pnl stays consistent with
                      realized PnL and MAE (single position-size source of truth).
        stop_loss   : current stop premium level
        max_pnl     : peak PnL seen so far (Rs)
        ml_prob     : ML probability at entry
        target      : fixed target premium (optional)

    Returns:
        (updated_stop_loss, updated_max_pnl, exit_reason | None)
    """
    qty     = max(lot_size, 1)
    pnl     = (ltp - entry_price) * qty
    max_pnl = max(max_pnl, pnl)
    reason  = None

    # ── 0  Fixed target hit ───────────────────────────────────────────
    if target is not None and ltp >= target:
        return stop_loss, max_pnl, "TARGET_HIT"

    # ── 1  Centralized profit-lock ladder (single source of truth) ────
    new_stop, stage, locked_rs = ladder_stop(entry_price, qty, max_pnl, stop_loss)
    if new_stop > stop_loss + 1e-6:
        logger.info(
            f"[LADDER]\nMFE={max_pnl:.0f}\nLOCK={locked_rs:.0f}\n"
            f"stage={stage}  SL {stop_loss:.2f}->{new_stop:.2f}"
        )
    stop_loss = new_stop

    # ── 2  Drawdown exit — only after meaningful profit (kept) ────────
    if max_pnl >= qty * 10:
        retention = 0.65 if ml_prob >= 0.65 else 0.55
        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # ── 3  Hard stop — VIRTUAL trigger (market exit, fill may gap below) ─
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason
