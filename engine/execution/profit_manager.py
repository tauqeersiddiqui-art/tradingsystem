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
#   MFE >= Rs 390  ->  lock Rs195   (~+13pt for a 30-qty lot, net-positive floor)
#   MFE >= Rs 195  ->  lock Rs 65   (~+6.5pt for a 30-qty lot, cost-recovery floor)
#   MFE >= Rs 130  ->  lock Rs 32   (~+4.3pt for a 30-qty lot, slippage-proof entry)
#
# RULES (enforced by construction):
#   * Stop only TIGHTENS (max() ratchet) — it can never loosen.
#   * LONG CE & PE both profit when premium RISES → identical trailing logic.
#   * Scalp and normal behave identically (same ladder_stop()).
#   * max_pnl is the only profit reference used.

import logging

logger = logging.getLogger("profit_manager")

# Retained for backward-compat (telegram.messages imports LOCK_PTS).
# Lot size mirrors Config.LOT_SIZE / cost_model (BANKNIFTY = 30) — single
# source of truth is engine.config.Config.LOT_SIZE.
_LOT_QTY  = 30
_RS_FLOOR = 200.0
LOCK_PTS  = _RS_FLOOR / _LOT_QTY   # 6.667 pts

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

_COST_PER_LOT = 66.0     # round-trip cost per 30-qty lot (overridable via env)
_LOT_UNITS    = 30       # BANKNIFTY lot size — mirrors Config.LOT_SIZE
_TRAIL_PCT    = 0.62     # fraction of peak profit retained once cost recovered

# ── TIER 2: Trailing & Scale-Out Configuration ─────────────────────────
# These can be overridden via Config (env vars TRAIL_ACTIVATION_PTS,
# TRAIL_DISTANCE_PTS, SCALE_OUT_PCT, SCALE_OUT_PTS)
_DEFAULT_TRAIL_ACTIVATION_PTS = 2.0   # trailing activates at +2pt profit
_DEFAULT_TRAIL_DISTANCE_PTS   = 2.0   # trail 2pt behind peak
_DEFAULT_SCALE_OUT_PCT        = 0.5   # scale out 50%
_DEFAULT_SCALE_OUT_PTS        = 2.0   # scale out at +2pt profit


def _cost_rs(qty: int) -> float:
    """Round-trip cost in Rs for a given position qty (scales by lots).

    Delegates to cost_model so profit locking and PnL accounting share ONE
    cost + lot-size source of truth.
    """
    from engine.execution.cost_model import round_trip_cost
    return round_trip_cost(qty)


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


def ladder_stop(entry_price, qty, max_pnl, current_stop, config=None, side="CE"):
    """
    Convert the rupee profit-lock to a premium stop level and ratchet TIGHTER only.

    Returns (new_stop, stage_label, locked_rs, scale_out_info).
    Used by BOTH manage_position (normal trades) and the scalp loop.

    Tier 2 changes:
    - Trailing only activates after TRAIL_ACTIVATION_PTS profit (default +2pt)
    - Trail TRAIL_DISTANCE_PTS behind peak (default 2pt)
    - Scale out SCALE_OUT_PCT (default 50%) at SCALE_OUT_PTS (default +2pt)

    LONG CE & PE both profit when premium RISES → identical trailing logic (stop trails UP).
    """
    import os

    # Get config values with defaults
    trail_activation_pts = float(getattr(config, "TRAIL_ACTIVATION_PTS", _DEFAULT_TRAIL_ACTIVATION_PTS)) if config else _DEFAULT_TRAIL_ACTIVATION_PTS
    trail_distance_pts   = float(getattr(config, "TRAIL_DISTANCE_PTS", _DEFAULT_TRAIL_DISTANCE_PTS)) if config else _DEFAULT_TRAIL_DISTANCE_PTS
    scale_out_pct        = float(getattr(config, "SCALE_OUT_PCT", _DEFAULT_SCALE_OUT_PCT)) if config else _DEFAULT_SCALE_OUT_PCT
    scale_out_pts        = float(getattr(config, "SCALE_OUT_PTS", _DEFAULT_SCALE_OUT_PTS)) if config else _DEFAULT_SCALE_OUT_PTS

    locked_rs, stage = ladder_locked_rs(max_pnl, qty)

    # Convert peak PnL to points profit for trailing logic
    # LONG CE & PE: profit when premium RISES → pts_profit = (peak_premium - entry)
    pts_profit = max_pnl / max(qty, 1) if max_pnl > 0 else 0.0

    # ── TIER 2: Trailing only activates after TRAIL_ACTIVATION_PTS ──
    if pts_profit < trail_activation_pts:
        # Not enough profit to activate trailing — keep initial stop
        return current_stop, stage, 0.0, None

    if locked_rs <= 0:
        return current_stop, stage, 0.0, None

    # ── LONG CE & PE: profit when premium RISES ──
    peak_price = entry_price + pts_profit
    trail_floor = peak_price - trail_distance_pts
    stop_floor = max(trail_floor, entry_price + locked_rs / max(qty, 1))
    new_stop = max(current_stop, stop_floor)   # never loosen (ratchet UP)

    # Scale-out: trigger when premium has risen SCALE_OUT_PTS from entry
    scale_out_info = None
    if pts_profit >= scale_out_pts and not getattr(ladder_stop, f"_scaled_out_{side}", False):
        scale_out_info = {
            "pct": scale_out_pct,
            "trigger_pts": scale_out_pts,
            "peak_price": peak_price,
        }
        setattr(ladder_stop, f"_scaled_out_{side}", True)
    elif pts_profit < scale_out_pts:
        setattr(ladder_stop, f"_scaled_out_{side}", False)

    return new_stop, stage, locked_rs, scale_out_info


def manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob, target=None, config=None, side="CE"):
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
        config      : Config object for Tier 2 params (TRAIL_ACTIVATION_PTS, etc.)
        side        : "CE" or "PE" — determines profit direction

    Returns:
        (updated_stop_loss, updated_max_pnl, exit_reason | None, scale_out_info | None)
    """
    qty     = max(lot_size, 1)
    # PnL: LONG CE & PE both profit when premium RISES
    pnl     = (ltp - entry_price) * qty
    max_pnl = max(max_pnl, pnl)
    reason  = None

    # Task #19 (Phase-10): when the Phase-10 premium-space trail is active
    # (live_engine.check_exit, ML_TRAIL_ENABLED), it is the SOLE stop
    # mechanism for ML trades — the legacy Rs-based ladder would otherwise
    # tighten far beyond the approved breakeven@+10 / trail-8-after-+20
    # tiers (e.g. locking 62% of peak at +10 pts) and break the validated
    # backtest semantics. Scalp callers pass config=None and keep the ladder.
    _p10 = bool(getattr(config, "ML_TRAIL_ENABLED", False)) if config is not None else False
    if _p10:
        # Fixed target still fires (Phase-10 TARGET = +80 pts premium).
        if target is not None and ltp >= target:
            return stop_loss, max_pnl, "TARGET_HIT", None
        if ltp <= stop_loss:
            reason = "Stop Loss"
        return stop_loss, max_pnl, reason, None

    # ── 0  Fixed target hit ───────────────────────────────────────────
    # Target is ABOVE entry for both CE and PE (profit when premium rises)
    if target is not None and ltp >= target:
        return stop_loss, max_pnl, "TARGET_HIT", None

    # ── 1  Centralized profit-lock ladder (single source of truth) ────
    new_stop, stage, locked_rs, scale_out_info = ladder_stop(entry_price, qty, max_pnl, stop_loss, config, side)
    tightened = new_stop > stop_loss + 1e-6
    if tightened:
        logger.info(
            f"[LADDER]\nMFE={max_pnl:.0f}\nLOCK={locked_rs:.0f}\n"
            f"stage={stage}  SL {stop_loss:.2f}->{new_stop:.2f}  ({side})"
        )
    stop_loss = new_stop

    # ── 2  Drawdown exit — only after meaningful profit (kept) ────────
    if max_pnl >= qty * 10:
        retention = 0.65 if ml_prob >= 0.65 else 0.55
        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # ── 3  Hard stop — VIRTUAL trigger (market exit, fill may gap) ─
    # LONG CE & PE: stop hit when ltp <= stop_loss (premium falls back)
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason, scale_out_info
