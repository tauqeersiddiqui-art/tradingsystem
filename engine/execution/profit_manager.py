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
#   MFE >= Rs 390  ->  lock Rs195   (net-positive floor, BANKNIFTY 30-lot)
#   MFE >= Rs 195  ->  lock Rs 65   (cost-recovery floor, BANKNIFTY 30-lot)
#   MFE >= Rs 130  ->  lock Rs 32   (slippage-proof entry, BANKNIFTY 30-lot)
#
# RULES (enforced by construction):
#   * Stop only TIGHTENS (max() ratchet) — it can never loosen.
#   * CE and PE behave identically (both are long-premium positions).
#   * Scalp and normal behave identically (same ladder_stop()).
#   * max_pnl is the only profit reference used.

import logging
import os

logger = logging.getLogger("profit_manager")

# Retained for backward-compat (telegram.messages imports LOCK_PTS).
_TRAIL_ARM_PTS_DEFAULT = 10.0
_TRAIL_GAP_PTS_DEFAULT = 5.0
LOCK_PTS = _TRAIL_GAP_PTS_DEFAULT


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def trail_settings() -> tuple[float, float]:
    """Return (arm_points, trail_gap_points) for long-option trailing stops."""
    arm_pts = max(0.0, _env_float("TRAIL_ARM_PTS", _TRAIL_ARM_PTS_DEFAULT))
    gap_pts = max(0.0, _env_float("TRAIL_GAP_PTS", _TRAIL_GAP_PTS_DEFAULT))
    return arm_pts, gap_pts

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

_COST_PER_LOT = 66.0     # round-trip cost per lot (overridable via env)
# Fraction of peak profit retained once cost recovered. Raised 0.62 -> 0.72 on
# 2026-06-29: an 18pt MFE (Rs540) was booking only Rs222 (59% give-back). At 0.72
# the same peak locks ~Rs389, so fast reversals surrender far less of the move.
_TRAIL_PCT    = 0.72


def _lot_units(config) -> int:
    """Get lot size from Config (single source of truth)."""
    from engine.execution.cost_model import lot_qty
    return lot_qty(config)


def _cost_rs(qty: int, config=None) -> float:
    """Round-trip cost in Rs for a given position qty (scales by lots).

    Delegates to the authoritative cost model (single source of truth)."""
    from engine.execution.cost_model import round_trip_cost
    return round_trip_cost(qty, config)


def _profit_lock_floor_rs(qty: int, config=None) -> float:
    """
    Minimum gross profit the ladder should try to protect.

    Default behavior is the user's requested gross-cost floor: Rs66 per lot.
    Extra buffer / net-profit requirements are opt-in via env.
    """
    cost = _cost_rs(qty, config)
    lots = max(1, round(qty / _lot_units(config)))
    slip_buffer = max(0.0, _env_float("PROFIT_LOCK_SLIPPAGE_BUFFER_RS", 0.0)) * lots
    min_net = max(0.0, _env_float("PROFIT_LOCK_MIN_NET_PROFIT_RS", 0.0))
    return cost + slip_buffer + min_net


def _dynamic_lock_profile(ml_prob, regime):
    """
    DYNAMIC tight/loose decision — ML conviction + market regime choose how much
    of a winning move to give back before locking. This is what lets the engine
    "decide when to tighten / loosen" instead of one fixed 0.72.

    Returns dict: trail_pct, arm_mult, ret_hi, ret_lo, label.
      * trail_pct  — fraction of peak profit retained by the trailing lock
      * arm_mult   — MFE (in cost-multiples) before the first lock arms
      * ret_hi/lo  — drawdown-exit retention for high / low ML prob
    Logic:
      - High conviction (>=0.78) or TREND day  -> LOOSEN (let winners run)
      - Low  conviction (<=0.58) or RANGE/VOL  -> TIGHTEN (lock profit fast)
    """
    prob   = ml_prob if (ml_prob and ml_prob > 0) else 0.5
    reg    = str(regime or "").upper()
    trend  = "TREND" in reg
    choppy = ("RANGE" in reg) or ("VOLATILE" in reg) or ("EXPANSION" in reg)

    # conviction axis
    if prob >= 0.78:
        trail_pct, arm_mult, ret_hi, ret_lo, label = 0.66, 1.6, 0.66, 0.58, "LOOSE_HICONV"
    elif prob <= 0.58:
        trail_pct, arm_mult, ret_hi, ret_lo, label = 0.80, 1.3, 0.78, 0.70, "TIGHT_LOCONV"
    else:
        trail_pct, arm_mult, ret_hi, ret_lo, label = 0.72, 1.5, 0.72, 0.62, "BAL"

    # regime axis (stacks on conviction)
    if trend:
        trail_pct -= 0.06; ret_hi -= 0.05; ret_lo -= 0.05; arm_mult += 0.3
        label += "+TREND"
    elif choppy:
        trail_pct += 0.06; ret_hi += 0.05; ret_lo += 0.05; arm_mult -= 0.2
        label += "+CHOP"

    trail_pct = max(0.55, min(trail_pct, 0.88))
    ret_hi    = max(0.55, min(ret_hi, 0.85))
    ret_lo    = max(0.50, min(ret_lo, 0.80))
    arm_mult  = max(1.1, min(arm_mult, 2.0))
    return {"trail_pct": trail_pct, "arm_mult": arm_mult,
            "ret_hi": ret_hi, "ret_lo": ret_lo, "label": label}


def ladder_locked_rs(max_pnl: float, qty: int = None,
                     ml_prob=None, regime=None, config=None):
    """
    Return (locked_profit_rs, stage_label) for the current peak PnL in Rs.

    Cost-aware AND conviction/regime-aware: never returns a lock below the
    trade's round-trip cost, and the arming multiple + trail fraction now come
    from _dynamic_lock_profile() so high-conviction / trend trades give the move
    more room while weak / choppy trades lock fast.
    """
    qty = max(int(qty or 0), 1)
    cost = _cost_rs(qty, config)
    floor = _profit_lock_floor_rs(qty, config)
    prof = _dynamic_lock_profile(ml_prob, regime)
    arm_threshold = max(floor, cost * prof["arm_mult"])

    # Not enough cushion yet — rely on the initial stop (no early lock).
    if max_pnl < arm_threshold:
        return 0.0, "INITIAL"

    # Trail a dynamic fraction of the peak, floored at the configured gross lock.
    locked = max(prof["trail_pct"] * max_pnl, floor)

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
        stage  = "S1_COSTLOCK" if locked <= floor + 1e-6 else f"S2_{prof['label']}"

    locked = min(locked, max_pnl)   # never lock more than the peak itself
    return locked, stage


def ladder_stop(entry_price, qty, max_pnl, current_stop, ml_prob=None, regime=None, config=None):
    """
    Convert the rupee profit-lock to a premium stop level and ratchet UP only.

    Returns (new_stop, stage_label, locked_rs).
    Used by BOTH manage_position (normal trades) and the scalp loop.
    ml_prob / regime drive the dynamic tight-vs-loose trail.
    """
    locked_rs, stage = ladder_locked_rs(max_pnl, qty, ml_prob, regime, config)
    if locked_rs <= 0:
        return current_stop, stage, 0.0
    stop_floor = entry_price + locked_rs / max(qty, 1)
    new_stop   = max(current_stop, stop_floor)   # never loosen
    return new_stop, stage, locked_rs


def manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob,
                    target=None, regime=None, config=None):
    """
    Args:
        entry_price : option premium at entry
        ltp         : current live option premium
        lot_size    : ACTUAL position quantity (total units, e.g. 30 / 60).
                      Named lot_size for backward-compat; callers MUST pass
                      position["qty"] so MFE/max_pnl stays consistent with
                      realized PnL and MAE (single position-size source of truth).
        stop_loss   : current stop premium level
        max_pnl     : peak PnL seen so far (Rs)
        ml_prob     : ML probability at entry
        target      : fixed target premium (optional)
        config      : Config object for lot size and cost parameters

    Returns:
        (updated_stop_loss, updated_max_pnl, exit_reason | None)
    """
    qty     = max(lot_size, 1)
    pnl     = (ltp - entry_price) * qty
    max_pnl = max(max_pnl, pnl)
    reason  = None

    # ── 0  Optional fixed target hit (disabled by default) ────────────
    if _env_bool("TARGET_EXIT_ENABLED", False) and target is not None and ltp >= target:
        return stop_loss, max_pnl, "TARGET_HIT"

    # ── 1  Centralized profit-lock ladder (single source of truth) ────
    new_stop, stage, locked_rs = ladder_stop(
        entry_price, qty, max_pnl, stop_loss, ml_prob, regime, config
    )
    if new_stop > stop_loss + 1e-6:
        logger.info(
            f"[LADDER]\nMFE={max_pnl:.0f}\nLOCK={locked_rs:.0f}\n"
            f"stage={stage}  SL {stop_loss:.2f}->{new_stop:.2f}"
        )
    stop_loss = new_stop

    # ── 2  Drawdown exit — only after meaningful profit ───────────────
    # DYNAMIC: retention now comes from _dynamic_lock_profile(), so a trending
    # high-conviction trade keeps running while a choppy/weak one locks fast.
    # Drawdown exit disabled: the 5-point trail is the profit-protection exit.

    # ── 3  Hard stop — VIRTUAL trigger (market exit, fill may gap below) ─
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason
