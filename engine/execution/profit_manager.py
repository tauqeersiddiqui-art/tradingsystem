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
#   MFE >= Rs 300  ->  lock Rs200
#   MFE >= Rs 150  ->  lock Rs100
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

# (threshold_rs, stage_label, fixed_lock_rs, pct_of_peak)
_LADDER = [
    (2000.0, "S6_LOCK80%", None,  0.80),
    (1200.0, "S5_LOCK70%", None,  0.70),
    ( 800.0, "S4_LOCK600", 600.0, None),
    ( 500.0, "S3_LOCK350", 350.0, None),
    ( 300.0, "S2_LOCK200", 200.0, None),
    ( 150.0, "S1_LOCK100", 100.0, None),
]


def ladder_locked_rs(max_pnl: float):
    """Return (locked_profit_rs, stage_label) for the current peak PnL in Rs."""
    for threshold_rs, label, fixed_rs, pct in _LADDER:
        if max_pnl >= threshold_rs:
            locked = fixed_rs if fixed_rs is not None else max_pnl * pct
            return locked, label
    return 0.0, "INITIAL"


def ladder_stop(entry_price, qty, max_pnl, current_stop):
    """
    Convert the rupee profit-lock to a premium stop level and ratchet UP only.

    Returns (new_stop, stage_label, locked_rs).
    Used by BOTH manage_position (normal trades) and the scalp loop.
    """
    locked_rs, stage = ladder_locked_rs(max_pnl)
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
        retention = 0.80 if ml_prob >= 0.65 else 0.72
        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # ── 3  Hard stop — VIRTUAL trigger (market exit, fill may gap below) ─
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason
