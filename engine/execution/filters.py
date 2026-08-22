# execution/filters.py

import os


def has_oi_wall(option_chain, atm_strike, direction):

    try:

        if not option_chain:
            return False

        nearby = sorted(
            option_chain,
            key=lambda x: abs(x.get("strike", 0) - atm_strike)
        )[:5]

        avg_ce = sum(s.get("ce_oi", 0) for s in nearby) / max(len(nearby), 1)
        avg_pe = sum(s.get("pe_oi", 0) for s in nearby) / max(len(nearby), 1)

        for s in nearby:

            strike = s.get("strike", 0)
            ce_oi = s.get("ce_oi", 0)
            pe_oi = s.get("pe_oi", 0)

            if direction == "CE":

                if strike > atm_strike and ce_oi > avg_ce * 2:
                    return True

            elif direction == "PE":

                if strike < atm_strike and pe_oi > avg_pe * 2:
                    return True

        return False

    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# ENTRY QUALITY / TIMING REJECTION FILTER (Task #7)
#
# Rejection-first architecture: compute_entry_quality() returns
# accepted=False with the FIRST rule that fails; entry proceeds ONLY if
# no rejection fires. No new indicators are introduced — only OHLC
# geometry plus momentum_velocity using the EXACT ml/feature_config.py
# formula (diff of normalized 1-bar returns).
# ══════════════════════════════════════════════════════════════════════

# ── Named thresholds (env-overridable) ────────────────────────────────
SWING_LOOKBACK     = 20                                              # bars for swing extreme / avg range
MOVE_PCT_MAX       = float(os.getenv("EQ_MOVE_PCT_MAX", "0.004"))    # 0.4% — move already done
BREAKOUT_MAX_AGE_S = float(os.getenv("EQ_BREAKOUT_MAX_AGE_S", "120"))# late entry cutoff
CLOSE_POS_MAX      = float(os.getenv("EQ_CLOSE_POS_MAX", "0.85"))    # buying-at-top (symmetric in mirrored coords)
WICK_RATIO_MAX     = float(os.getenv("EQ_WICK_RATIO_MAX", "0.6"))    # adverse wick / full range
MIN_QUALITY_SCORE  = int(os.getenv("EQ_MIN_QUALITY_SCORE", "3"))
MOVE_PCT_GOOD      = float(os.getenv("EQ_MOVE_PCT_GOOD", "0.003"))   # fresh-move bonus
CLOSE_POS_GOOD     = 0.7                                             # score bonus (symmetric in mirrored coords)
NOT_PROFIT_BUFFER  = 0.2                                             # +20% over round-trip cost
DELTA_PROXY        = 0.5                                             # rough ATM option delta
LOT_QTY_PROXY      = 30                                              # one BANKNIFTY lot

# Module-level rejection counters for backtest aggregation.
_REJECTION_COUNTS: dict = {}
_QUALITY_EVALS: int = 0


def get_rejection_stats() -> dict:
    """Aggregate rejection counts (for backtest / EOD reporting)."""
    return {
        "evals": _QUALITY_EVALS,
        "rejections": dict(_REJECTION_COUNTS),
        "total_rejections": sum(_REJECTION_COUNTS.values()),
    }


def reset_rejection_stats() -> None:
    """Clear rejection counters (e.g. between backtest folds)."""
    global _QUALITY_EVALS
    _REJECTION_COUNTS.clear()
    _QUALITY_EVALS = 0


def _eq_reject(reason: str, metrics: dict) -> dict:
    _REJECTION_COUNTS[reason] = _REJECTION_COUNTS.get(reason, 0) + 1
    return {"accepted": False, "reason": reason, "metrics": metrics}


def _mom_velocity(closes) -> float:
    """momentum_velocity — SAME formula as ml/feature_config.py: diff of
    normalized 1-bar returns (NOT raw price delta)."""
    if len(closes) < 3:
        return 0.0
    r1 = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] != 0 else 0.0
    r2 = (closes[-2] - closes[-3]) / closes[-3] if closes[-3] != 0 else 0.0
    return r1 - r2


def df_from_ticks(ticks, buckets: int = 6):
    """Bucket raw (ts, price) ticks into synthetic OHLC bars so the
    candle-based quality rules can also run on tick-only paths (scalp
    engine has no 1m candles). Returns a pandas DataFrame or None."""
    if not ticks:
        return None
    try:
        import pandas as pd
    except Exception:
        return None
    prices = [p for _, p in ticks]
    n = len(prices)
    k = max(1, min(buckets, n))
    rows = []
    size = n / k
    for i in range(k):
        seg = prices[int(i * size):max(int((i + 1) * size), int(i * size) + 1)]
        if not seg:
            continue
        rows.append({"open": seg[0], "high": max(seg),
                     "low": min(seg), "close": seg[-1]})
    return pd.DataFrame(rows) if rows else None


def compute_entry_quality(df_window, side: str, ltp: float, ts,
                          orb_state: dict = None, cost_rs: float = None) -> dict:
    """
    Entry-timing / trade-quality REJECTION filter (rejection-first).

    Evaluates rules in fixed order; first failure wins and increments the
    module-level rejection counter for that reason:
      1. MOVE_ALREADY_DONE — move off the 20-bar swing already > MOVE_PCT_MAX
      2. LATE_ENTRY        — breakout older than BREAKOUT_MAX_AGE_S
      3. BUYING_AT_TOP     — last completed candle closed at its extreme
      4. REJECTION_CANDLE  — adverse wick dominates the bar
      5. MOMENTUM_DYING    — momentum_velocity falling while price still extends
      6. LOW_QUALITY       — composite score < MIN_QUALITY_SCORE
      7. NOT_PROFITABLE    — expected premium move can't cover round-trip cost

    df_window: DataFrame of COMPLETED 1m candles (open/high/low/close).
    orb_state: {"breakout_ts": datetime|None, "orb_done": bool}.
    cost_rs:   optional round-trip cost (Rs) — enables NOT_PROFITABLE check.

    Returns {"accepted": bool, "reason": str|None, "metrics": {...}}.
    """
    global _QUALITY_EVALS
    _QUALITY_EVALS += 1

    orb_state = orb_state or {}
    is_ce = (side or "CE").upper() != "PE"

    neutral = {
        "move_pct": 0.0, "wick_ratio": 0.0, "close_position": 0.5,
        "breakout_age_s": None, "momentum_velocity_now": 0.0,
        "momentum_velocity_prev": 0.0, "score": MIN_QUALITY_SCORE,
    }
    # Fail-open: without data there is nothing to reject on.
    if df_window is None or len(df_window) < 4 or ltp is None or ltp <= 0:
        return {"accepted": True, "reason": None, "metrics": neutral}

    # ── Swing / move extension ────────────────────────────────────────
    n = min(len(df_window), SWING_LOOKBACK)
    recent_low  = float(df_window["low"].iloc[-n:].min())
    recent_high = float(df_window["high"].iloc[-n:].max())
    if is_ce:
        move_pct = (ltp - recent_low) / recent_low if recent_low else 0.0
    else:
        move_pct = (recent_high - ltp) / recent_high if recent_high else 0.0

    # ── Breakout age ──────────────────────────────────────────────────
    breakout_ts = orb_state.get("breakout_ts")
    breakout_age_s = None
    if breakout_ts is not None and ts is not None:
        try:
            breakout_age_s = (ts - breakout_ts).total_seconds()
        except Exception:
            breakout_age_s = None

    # ── Last COMPLETED candle geometry ────────────────────────────────
    o = float(df_window["open"].iloc[-1])
    h = float(df_window["high"].iloc[-1])
    l = float(df_window["low"].iloc[-1])
    c = float(df_window["close"].iloc[-1])
    rng = h - l
    raw_close_pos = (c - l) / rng if rng > 0 else 0.5
    # PE mirrors: 1 - raw so "closing at the top of the bar" reads as 0.
    # Thresholds below are SYMMETRIC in these mirrored coordinates — the
    # value is mirrored once, the threshold never (fixes the Task #14
    # double-mirror bug where the two mirrors cancelled each other).
    close_position = raw_close_pos if is_ce else 1.0 - raw_close_pos
    adverse_wick = (h - max(o, c)) if is_ce else (min(o, c) - l)
    wick_ratio = adverse_wick / rng if rng > 0 else 0.0

    # ── momentum_velocity (feature_config formula) ────────────────────
    closes_all = list(df_window["close"])
    mom_now  = _mom_velocity(closes_all)
    mom_prev = _mom_velocity(closes_all[:-1]) if len(closes_all) >= 4 else mom_now

    metrics = {
        "move_pct": move_pct,
        "wick_ratio": wick_ratio,
        "close_position": close_position,
        "breakout_age_s": breakout_age_s,
        "momentum_velocity_now": mom_now,
        "momentum_velocity_prev": mom_prev,
        "score": 0,
    }

    # 1. MOVE_ALREADY_DONE — the swing already ran; we'd be chasing.
    if move_pct > MOVE_PCT_MAX:
        return _eq_reject("MOVE_ALREADY_DONE", metrics)

    # 2. LATE_ENTRY — breakout flagged too long ago.
    if breakout_age_s is not None and breakout_age_s > BREAKOUT_MAX_AGE_S:
        return _eq_reject("LATE_ENTRY", metrics)

    # 3. BUYING_AT_TOP — last candle closed at the adverse extreme.
    # Identical direction for both sides in mirrored coordinates:
    # CE close at bar top (raw≈1) and PE chase at bar bottom (raw≈0 →
    # mirrored≈1) both read close_position > CLOSE_POS_MAX.
    if close_position > CLOSE_POS_MAX:
        return _eq_reject("BUYING_AT_TOP", metrics)

    # 4. REJECTION_CANDLE — adverse wick dominates the bar.
    if wick_ratio > WICK_RATIO_MAX:
        return _eq_reject("REJECTION_CANDLE", metrics)

    # 5. MOMENTUM_DYING — velocity falling while price still extends.
    price_extending = (
        (closes_all[-1] > closes_all[-3]) if is_ce
        else (closes_all[-1] < closes_all[-3])
    )
    if mom_now < mom_prev and price_extending:
        return _eq_reject("MOMENTUM_DYING", metrics)

    # 6. LOW_QUALITY — composite score.
    score = 0
    if move_pct < MOVE_PCT_GOOD:
        score += 1
    if close_position < CLOSE_POS_GOOD:
        score += 1
    if mom_now > mom_prev:
        score += 1
    if breakout_age_s is None or breakout_age_s <= BREAKOUT_MAX_AGE_S:
        score += 1
    metrics["score"] = score
    if score < MIN_QUALITY_SCORE:
        return _eq_reject("LOW_QUALITY", metrics)

    # 7. NOT_PROFITABLE — expected favorable move can't cover costs.
    # ASSUMPTION (documented): the filter only sees spot, so we proxy the
    # premium move as 0.5 * (20-bar avg range) spot pts converted at a rough
    # ATM delta of 0.5 and priced for ONE BANKNIFTY lot (30 qty). Caller
    # passes the authoritative round-trip cost (engine.execution.cost_model).
    if cost_rs is not None and cost_rs > 0:
        avg_range = float((df_window["high"].iloc[-n:] -
                           df_window["low"].iloc[-n:]).mean())
        expected_move_pts = 0.5 * avg_range
        expected_premium_rs = expected_move_pts * DELTA_PROXY * LOT_QTY_PROXY
        if expected_premium_rs < cost_rs * (1.0 + NOT_PROFIT_BUFFER):
            return _eq_reject("NOT_PROFITABLE", metrics)

    return {"accepted": True, "reason": None, "metrics": metrics}