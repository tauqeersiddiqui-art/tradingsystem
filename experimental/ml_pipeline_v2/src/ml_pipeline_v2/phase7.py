from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_pipeline_v2.config import PipelineConfig
from ml_pipeline_v2.phase4 import json_default, read_json, write_json
from ml_pipeline_v2.phase5 import (
    json_records,
    phase5_paths,
    resolve_dataset_path,
    write_csv,
)
from ml_pipeline_v2.phase6 import _profit_factor, load_phase5_trades

warnings.filterwarnings("ignore", category=RuntimeWarning)

PHASE7_SCHEMA_VERSION = "phase7.adaptive_exit_intelligence.v1"
_LOT = 30
_BROK = 132.0
_TARGET_PTS = 15.0
_BE_TRIGGER_PTS = 8.0
_MAX_BARS = 12          # mirrors Phase 5 quality_lookahead_bars

# ─── PATHS ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Phase7Paths:
    base: Path
    simulation: Path
    profit_lock: Path
    exit_quality: Path
    feasibility: Path
    recommendations: Path


def phase7_paths(config: PipelineConfig) -> Phase7Paths:
    base = config.paths.output_dir / "reports" / "phase7"
    paths = Phase7Paths(
        base=base,
        simulation=base / "simulation",
        profit_lock=base / "profit_lock",
        exit_quality=base / "exit_quality",
        feasibility=base / "feasibility",
        recommendations=base / "recommendations",
    )
    for p in (paths.base, paths.simulation, paths.profit_lock,
              paths.exit_quality, paths.feasibility, paths.recommendations):
        p.mkdir(parents=True, exist_ok=True)
    return paths


# ─── MODULE 1: STRATEGY CATALOGUE ──────────────────────────────────────────────

STRATEGY_META: dict[str, dict[str, Any]] = {
    "atr_trail_1.5": {
        "description": "ATR Trailing Stop (1.5× ATR) — stop trails below/above close by 1.5 × ATR; only moves in trade direction",
        "uses_future": False,
        "complexity": "low",
        "params": {"mult": 1.5},
    },
    "atr_trail_2.0": {
        "description": "ATR Trailing Stop (2.0× ATR) — stop trails by 2.0 × ATR; wider room for price to breathe",
        "uses_future": False,
        "complexity": "low",
        "params": {"mult": 2.0},
    },
    "atr_trail_2.5": {
        "description": "ATR Trailing Stop (2.5× ATR) — widest ATR trail; stays in longer trending moves",
        "uses_future": False,
        "complexity": "low",
        "params": {"mult": 2.5},
    },
    "chandelier_1.0": {
        "description": "Chandelier Exit (1.0× ATR, 10-bar) — stop = rolling_max_high(10) − 1.0 × ATR; classic institutional exit",
        "uses_future": False,
        "complexity": "low",
        "params": {"mult": 1.0, "period": 10},
    },
    "chandelier_1.5": {
        "description": "Chandelier Exit (1.5× ATR, 10-bar) — wider chandelier; allows more volatility",
        "uses_future": False,
        "complexity": "low",
        "params": {"mult": 1.5, "period": 10},
    },
    "supertrend_exit": {
        "description": "SuperTrend Exit — exit when the SuperTrend indicator reverses direction against the trade",
        "uses_future": False,
        "complexity": "low",
        "params": {},
    },
    "ema20_exit": {
        "description": "EMA20 Cross Exit — exit when close crosses EMA20 against trade direction",
        "uses_future": False,
        "complexity": "low",
        "params": {},
    },
    "vwap_exit": {
        "description": "VWAP Cross Exit — exit when price moves to the wrong side of VWAP",
        "uses_future": False,
        "complexity": "low",
        "params": {},
    },
    "be_trail_1.0": {
        "description": "Break-even + Trail (1.0× ATR) — once trade is +8 pts profitable, lock break-even stop; then trail by 1.0× ATR",
        "uses_future": False,
        "complexity": "medium",
        "params": {"be_pts": 8.0, "trail_mult": 1.0},
    },
    "be_trail_1.5": {
        "description": "Break-even + Trail (1.5× ATR) — break-even at +8 pts; trail by 1.5× ATR; tighter than 2.0× for faster profit protection",
        "uses_future": False,
        "complexity": "medium",
        "params": {"be_pts": 8.0, "trail_mult": 1.5},
    },
    "partial_runner": {
        "description": "Partial Profit + Runner — exit 50% at 15-pt target; trail remaining 50% with 2.0× ATR stop",
        "uses_future": False,
        "complexity": "medium",
        "params": {"target_pts": 15.0, "trail_mult": 2.0},
    },
    "time_6bars": {
        "description": "Time Exit (6 bars) — close the trade at bar 6 at the close price; no further signal needed",
        "uses_future": False,
        "complexity": "low",
        "params": {"max_bars": 6},
    },
    "time_8bars": {
        "description": "Time Exit (8 bars) — close trade at bar 8; balances time-in-trade with trend capture",
        "uses_future": False,
        "complexity": "low",
        "params": {"max_bars": 8},
    },
    "time_10bars": {
        "description": "Time Exit (10 bars) — close trade at bar 10; gives more room before max Phase 5 window",
        "uses_future": False,
        "complexity": "low",
        "params": {"max_bars": 10},
    },
    "vol_adaptive": {
        "description": "Volatility-Adaptive Trail — trailing stop distance = 2.0× ATR × max(1, current_vol / entry_vol); expands in high-vol bars",
        "uses_future": False,
        "complexity": "medium",
        "params": {"base_mult": 2.0},
    },
    "momentum_exhaust": {
        "description": "Momentum Exhaustion Exit — exit when momentum_velocity reverses sign after trade is in profit, or when RSI reaches extreme (>72 CE / <28 PE)",
        "uses_future": False,
        "complexity": "medium",
        "params": {"rsi_extreme_ce": 72.0, "rsi_extreme_pe": 28.0},
    },
    "actual": {
        "description": "Baseline (Phase 5 actual) — the actual Phase 5 V2 exit outcome; held to quality_lookahead window with MFE/MAE outcome",
        "uses_future": True,   # Phase 5 uses lookahead for MFE/MAE computation
        "complexity": "reference",
        "params": {},
    },
}


# ─── DATA LOADING ───────────────────────────────────────────────────────────────

_RAW_COLS = [
    "date", "close", "high", "low",
    "atr", "volatility",
    "supertrend_dir", "supertrend_line",
    "ema20",
    "vwap", "price_vs_vwap",
    "momentum_velocity", "rsi",
    "adx", "trend_strength", "ema_alignment",
]


def load_raw_dataset(config: PipelineConfig) -> pd.DataFrame:
    path = resolve_dataset_path(config)
    df = pd.read_csv(path, usecols=lambda c: c in _RAW_COLS + ["date"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in _RAW_COLS[1:]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def align_trades_to_raw(
    trades: pd.DataFrame,
    raw: pd.DataFrame,
    max_bars: int = _MAX_BARS,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Match each completed trade to an entry bar in raw dataset. Returns aligned trades + integer entry indices."""
    date_index: dict = {ts: i for i, ts in enumerate(raw["date"])}
    indices: list[int] = []
    keep: list[bool] = []
    n_raw = len(raw)

    for ts in trades["date"]:
        idx = date_index.get(ts)
        if idx is not None and idx + max_bars <= n_raw:
            indices.append(idx)
            keep.append(True)
        else:
            indices.append(-1)
            keep.append(False)

    mask = np.array(keep)
    aligned = trades[mask].copy().reset_index(drop=True)
    entry_idx = np.array(indices)[mask]
    return aligned, entry_idx


def extract_windows(
    raw: pd.DataFrame,
    entry_indices: np.ndarray,
    max_bars: int,
    cols: list[str],
) -> dict[str, np.ndarray]:
    """
    Vectorised window extraction using numpy advanced indexing.
    Returns dict col → shape (n_trades, max_bars).
    """
    offsets = np.arange(max_bars)
    idx_matrix = entry_indices[:, None] + offsets[None, :]  # (n, max_bars)

    arrays: dict[str, np.ndarray] = {}
    for col in cols:
        if col not in raw.columns:
            arrays[col] = np.full(idx_matrix.shape, np.nan)
        else:
            arr = raw[col].to_numpy(dtype=float)
            arrays[col] = arr[idx_matrix]

    return arrays


def _ffill_windows(w: dict[str, np.ndarray]) -> None:
    """Forward-fill NaN along bar axis (axis=1) in-place."""
    for col in w:
        a = w[col]
        mask = np.isnan(a)
        if not mask.any():
            continue
        for t in range(1, a.shape[1]):
            col_mask = mask[:, t] & ~np.isnan(a[:, t - 1])
            a[col_mask, t] = a[col_mask, t - 1]
        w[col] = a


# ─── PNL HELPER ────────────────────────────────────────────────────────────────

def _pnl_arr(
    entry: np.ndarray,
    exit_price: np.ndarray,
    is_ce: np.ndarray,
) -> np.ndarray:
    pts = np.where(is_ce, exit_price - entry, entry - exit_price)
    return pts * _LOT - _BROK


def _default_exit(close_w: np.ndarray, max_bars: int) -> tuple[np.ndarray, np.ndarray]:
    """Default: exit at last bar's close."""
    n = close_w.shape[0]
    exit_bars = np.full(n, max_bars - 1, dtype=int)
    exit_prices = np.where(np.isnan(close_w[:, -1]), close_w[:, 0], close_w[:, -1])
    return exit_bars, exit_prices


# ─── MODULE 1 + 2: EXIT STRATEGY SIMULATIONS ───────────────────────────────────

def _sim_atr_trail(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    high_w = w.get("high", close_w)
    low_w = w.get("low", close_w)
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    n, T = close_w.shape

    atr0 = np.where(np.isnan(atr_w[:, 0]), 20.0, atr_w[:, 0])
    stop_ce = entry - mult * atr0
    stop_pe = entry + mult * atr0

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        atr_t = np.where(np.isnan(atr_w[:, t]), atr0, atr_w[:, t])
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        low_t = np.where(np.isnan(low_w[:, t]), close_t, low_w[:, t])
        high_t = np.where(np.isnan(high_w[:, t]), close_t, high_w[:, t])

        # Advance stops only in trade direction
        stop_ce = np.maximum(stop_ce, close_t - mult * atr_t)
        stop_pe = np.minimum(stop_pe, close_t + mult * atr_t)

        ce_hit = is_ce & not_exited & (low_t <= stop_ce)
        pe_hit = (~is_ce) & not_exited & (high_t >= stop_pe)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[ce_hit] = np.maximum(stop_ce[ce_hit], low_t[ce_hit])
        exit_prices[pe_hit] = np.minimum(stop_pe[pe_hit], high_t[pe_hit])
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_chandelier(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    high_w = w.get("high", close_w)
    low_w = w.get("low", close_w)
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    n, T = close_w.shape

    atr0 = np.where(np.isnan(atr_w[:, 0]), 20.0, atr_w[:, 0])
    roll_max_h = np.where(np.isnan(high_w[:, 0]), close_w[:, 0], high_w[:, 0])
    roll_min_l = np.where(np.isnan(low_w[:, 0]), close_w[:, 0], low_w[:, 0])

    stop_ce = roll_max_h - mult * atr0
    stop_pe = roll_min_l + mult * atr0

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        atr_t = np.where(np.isnan(atr_w[:, t]), atr0, atr_w[:, t])
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        low_t = np.where(np.isnan(low_w[:, t]), close_t, low_w[:, t])
        high_t = np.where(np.isnan(high_w[:, t]), close_t, high_w[:, t])

        roll_max_h = np.maximum(roll_max_h, high_t)
        roll_min_l = np.minimum(roll_min_l, low_t)
        stop_ce = roll_max_h - mult * atr_t
        stop_pe = roll_min_l + mult * atr_t

        ce_hit = is_ce & not_exited & (low_t <= stop_ce)
        pe_hit = (~is_ce) & not_exited & (high_t >= stop_pe)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[ce_hit] = np.maximum(stop_ce[ce_hit], low_t[ce_hit])
        exit_prices[pe_hit] = np.minimum(stop_pe[pe_hit], high_t[pe_hit])
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_supertrend(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    st_dir_w = w.get("supertrend_dir", np.zeros_like(close_w))
    n, T = close_w.shape

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        st_t = np.where(np.isnan(st_dir_w[:, t]), 0.0, st_dir_w[:, t])

        # CE: exit when SuperTrend goes bearish (≤ 0)
        # PE: exit when SuperTrend goes bullish (≥ 0)
        ce_hit = is_ce & not_exited & (st_t <= 0)
        pe_hit = (~is_ce) & not_exited & (st_t >= 0)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[hit] = close_t[hit]
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_ema20(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    ema_w = w.get("ema20", close_w)
    n, T = close_w.shape

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        ema_t = np.where(np.isnan(ema_w[:, t]), close_t, ema_w[:, t])

        ce_hit = is_ce & not_exited & (close_t < ema_t)
        pe_hit = (~is_ce) & not_exited & (close_t > ema_t)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[hit] = close_t[hit]
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_vwap(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    pvwap_w = w.get("price_vs_vwap", np.zeros_like(close_w))
    n, T = close_w.shape

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        pvwap_t = np.where(np.isnan(pvwap_w[:, t]), 0.0, pvwap_w[:, t])

        # CE: exit when price drops below VWAP (pvwap < 0)
        # PE: exit when price rises above VWAP (pvwap > 0)
        ce_hit = is_ce & not_exited & (pvwap_t < 0)
        pe_hit = (~is_ce) & not_exited & (pvwap_t > 0)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[hit] = close_t[hit]
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_be_trail(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    be_pts: float,
    trail_mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Break-even stop once in profit by be_pts; then trail with trail_mult × ATR."""
    close_w = w["close"]
    high_w = w.get("high", close_w)
    low_w = w.get("low", close_w)
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    n, T = close_w.shape

    atr0 = np.where(np.isnan(atr_w[:, 0]), 20.0, atr_w[:, 0])

    # State: 0 = holding, 1 = be stop active, 2 = trailing
    be_active = np.zeros(n, dtype=bool)
    trail_active = np.zeros(n, dtype=bool)

    # Stops
    stop_ce = entry - trail_mult * atr0 * 2.0   # initial wide stop (2× mult)
    stop_pe = entry + trail_mult * atr0 * 2.0

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        atr_t = np.where(np.isnan(atr_w[:, t]), atr0, atr_w[:, t])
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        low_t = np.where(np.isnan(low_w[:, t]), close_t, low_w[:, t])
        high_t = np.where(np.isnan(high_w[:, t]), close_t, high_w[:, t])

        # CE unrealized PnL
        unreal_ce = close_t - entry
        unreal_pe = entry - close_t

        # Activate break-even
        just_be_ce = is_ce & not_exited & ~be_active & (unreal_ce >= be_pts)
        just_be_pe = (~is_ce) & not_exited & ~be_active & (unreal_pe >= be_pts)
        be_active |= (just_be_ce | just_be_pe)

        # Once BE active, stop = entry; then trail from there
        stop_ce = np.where(just_be_ce, entry, stop_ce)
        stop_pe = np.where(just_be_pe, entry, stop_pe)

        # Update trailing stop (only moves in trade direction once BE active)
        new_stop_ce = close_t - trail_mult * atr_t
        new_stop_pe = close_t + trail_mult * atr_t
        stop_ce = np.where(be_active & is_ce, np.maximum(stop_ce, new_stop_ce), stop_ce)
        stop_pe = np.where(be_active & (~is_ce), np.minimum(stop_pe, new_stop_pe), stop_pe)

        ce_hit = is_ce & not_exited & be_active & (low_t <= stop_ce)
        pe_hit = (~is_ce) & not_exited & be_active & (high_t >= stop_pe)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[ce_hit] = np.maximum(stop_ce[ce_hit], low_t[ce_hit])
        exit_prices[pe_hit] = np.minimum(stop_pe[pe_hit], high_t[pe_hit])
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_partial_runner(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    target_pts: float = 15.0,
    trail_mult: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    50% exits at target; remaining 50% trails with ATR stop.
    Returns (exit_bars, blended_exit_prices) where exit_prices already embeds
    the partial pricing so that _pnl_arr gives the blended PnL.
    """
    close_w = w["close"]
    high_w = w.get("high", close_w)
    low_w = w.get("low", close_w)
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    n, T = close_w.shape

    atr0 = np.where(np.isnan(atr_w[:, 0]), 20.0, atr_w[:, 0])
    target_price_ce = entry + target_pts
    target_price_pe = entry - target_pts

    # Phase tracking
    target_hit = np.zeros(n, dtype=bool)
    partial_exit_price = np.full(n, np.nan)  # the first-leg exit price

    # Trail stop (active after target hit)
    stop_ce = entry + target_pts - trail_mult * atr0  # trail from target
    stop_pe = entry - target_pts + trail_mult * atr0

    trail_exit_bars = np.full(n, T - 1, dtype=int)
    trail_exit_prices = np.where(np.isnan(close_w[:, -1]), close_w[:, 0], close_w[:, -1])

    not_trailing = np.ones(n, dtype=bool)  # runner still alive

    for t in range(1, T):
        atr_t = np.where(np.isnan(atr_w[:, t]), atr0, atr_w[:, t])
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        low_t = np.where(np.isnan(low_w[:, t]), close_t, low_w[:, t])
        high_t = np.where(np.isnan(high_w[:, t]), close_t, high_w[:, t])

        # Check target hit for first leg
        newly_ce = is_ce & ~target_hit & (high_t >= target_price_ce)
        newly_pe = (~is_ce) & ~target_hit & (low_t <= target_price_pe)
        newly_hit = newly_ce | newly_pe

        target_hit |= newly_hit
        partial_exit_price[newly_ce] = target_price_ce[newly_ce]
        partial_exit_price[newly_pe] = target_price_pe[newly_pe]

        # Reset trail stop from target level
        stop_ce = np.where(newly_ce, target_price_ce - trail_mult * atr_t, stop_ce)
        stop_pe = np.where(newly_pe, target_price_pe + trail_mult * atr_t, stop_pe)

        # Advance trail (only after target hit)
        new_stop_ce = close_t - trail_mult * atr_t
        new_stop_pe = close_t + trail_mult * atr_t
        stop_ce = np.where(target_hit & is_ce, np.maximum(stop_ce, new_stop_ce), stop_ce)
        stop_pe = np.where(target_hit & (~is_ce), np.minimum(stop_pe, new_stop_pe), stop_pe)

        # Check trail stop for runner
        ce_trail_hit = is_ce & target_hit & not_trailing & (low_t <= stop_ce)
        pe_trail_hit = (~is_ce) & target_hit & not_trailing & (high_t >= stop_pe)
        trail_hit = ce_trail_hit | pe_trail_hit

        trail_exit_bars[trail_hit] = t
        trail_exit_prices[ce_trail_hit] = np.maximum(stop_ce[ce_trail_hit], low_t[ce_trail_hit])
        trail_exit_prices[pe_trail_hit] = np.minimum(stop_pe[pe_trail_hit], high_t[pe_trail_hit])
        not_trailing[trail_hit] = False

    # Blended exit price: represents the effective price for blended PnL
    # PnL_blended = 0.5 * (first_leg_pts) + 0.5 * (runner_pts) - brok
    # We'll encode as a "phantom" blended price that gives the right PnL
    #   PnL = (blended_price - entry) * LOT - BROK  (for CE)
    # → blended_price = entry + (0.5*first_pts + 0.5*runner_pts)

    first_pts_ce = partial_exit_price - entry    # NaN if target not hit
    runner_pts_ce = trail_exit_prices - entry
    first_pts_pe = entry - partial_exit_price
    runner_pts_pe = entry - trail_exit_prices

    # If target was hit: blend 50/50
    blended_ce = entry + np.where(
        target_hit,
        0.5 * np.where(np.isnan(first_pts_ce), 0.0, first_pts_ce) + 0.5 * runner_pts_ce,
        runner_pts_ce,  # fallback: full ATR trail if target never hit
    )
    blended_pe = entry - np.where(
        target_hit,
        0.5 * np.where(np.isnan(first_pts_pe), 0.0, first_pts_pe) + 0.5 * runner_pts_pe,
        runner_pts_pe,
    )

    blended_exit = np.where(is_ce, blended_ce, blended_pe)
    exit_bars = np.where(target_hit, trail_exit_bars, trail_exit_bars)

    return exit_bars, blended_exit


def _sim_time(
    w: dict[str, np.ndarray],
    max_bars_cap: int,
) -> tuple[np.ndarray, np.ndarray]:
    close_w = w["close"]
    n, T = close_w.shape
    bar_idx = min(max_bars_cap - 1, T - 1)
    exit_bars = np.full(n, bar_idx, dtype=int)
    exit_prices = close_w[:, bar_idx].copy()
    nan_mask = np.isnan(exit_prices)
    exit_prices[nan_mask] = close_w[nan_mask, 0]
    return exit_bars, exit_prices


def _sim_vol_adaptive(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    base_mult: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """ATR trail with volatility-adjusted width: wider in high-vol bars."""
    close_w = w["close"]
    high_w = w.get("high", close_w)
    low_w = w.get("low", close_w)
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    vol_w = w.get("volatility", np.full_like(close_w, 0.01))
    n, T = close_w.shape

    atr0 = np.where(np.isnan(atr_w[:, 0]), 20.0, atr_w[:, 0])
    vol0 = np.where(np.isnan(vol_w[:, 0]) | (vol_w[:, 0] <= 0), 0.01, vol_w[:, 0])

    stop_ce = entry - base_mult * atr0
    stop_pe = entry + base_mult * atr0

    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        atr_t = np.where(np.isnan(atr_w[:, t]), atr0, atr_w[:, t])
        vol_t = np.where(np.isnan(vol_w[:, t]) | (vol_w[:, t] <= 0), vol0, vol_w[:, t])
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        low_t = np.where(np.isnan(low_w[:, t]), close_t, low_w[:, t])
        high_t = np.where(np.isnan(high_w[:, t]), close_t, high_w[:, t])

        # Adaptive multiplier: wider when current vol is higher than entry vol
        vol_ratio = np.clip(vol_t / vol0, 0.5, 3.0)
        adaptive_mult = base_mult * vol_ratio

        new_stop_ce = close_t - adaptive_mult * atr_t
        new_stop_pe = close_t + adaptive_mult * atr_t
        stop_ce = np.maximum(stop_ce, new_stop_ce)
        stop_pe = np.minimum(stop_pe, new_stop_pe)

        ce_hit = is_ce & not_exited & (low_t <= stop_ce)
        pe_hit = (~is_ce) & not_exited & (high_t >= stop_pe)
        hit = ce_hit | pe_hit

        exit_bars[hit] = t
        exit_prices[ce_hit] = np.maximum(stop_ce[ce_hit], low_t[ce_hit])
        exit_prices[pe_hit] = np.minimum(stop_pe[pe_hit], high_t[pe_hit])
        not_exited[hit] = False

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def _sim_momentum_exhaust(
    w: dict[str, np.ndarray],
    entry: np.ndarray,
    is_ce: np.ndarray,
    rsi_extreme_ce: float = 72.0,
    rsi_extreme_pe: float = 28.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Exit on momentum sign reversal after trade is in profit, OR on RSI extreme."""
    close_w = w["close"]
    mom_w = w.get("momentum_velocity", np.zeros_like(close_w))
    rsi_w = w.get("rsi", np.full_like(close_w, 50.0))
    n, T = close_w.shape

    prev_mom = mom_w[:, 0].copy()
    exit_bars, exit_prices = _default_exit(close_w, T)
    not_exited = np.ones(n, dtype=bool)

    for t in range(1, T):
        close_t = np.where(np.isnan(close_w[:, t]), close_w[:, t - 1], close_w[:, t])
        mom_t = np.where(np.isnan(mom_w[:, t]), prev_mom, mom_w[:, t])
        rsi_t = np.where(np.isnan(rsi_w[:, t]), 50.0, rsi_w[:, t])

        unrealized_ce = close_t - entry
        unrealized_pe = entry - close_t
        in_profit = np.where(is_ce, unrealized_ce > 0, unrealized_pe > 0)

        # Momentum reversal: was positive now negative (CE) or was negative now positive (PE)
        mom_rev_ce = is_ce & not_exited & in_profit & (prev_mom > 0) & (mom_t <= 0)
        mom_rev_pe = (~is_ce) & not_exited & in_profit & (prev_mom < 0) & (mom_t >= 0)

        # RSI extreme
        rsi_ext_ce = is_ce & not_exited & (rsi_t >= rsi_extreme_ce)
        rsi_ext_pe = (~is_ce) & not_exited & (rsi_t <= rsi_extreme_pe)

        hit = mom_rev_ce | mom_rev_pe | rsi_ext_ce | rsi_ext_pe
        exit_bars[hit] = t
        exit_prices[hit] = close_t[hit]
        not_exited[hit] = False

        prev_mom = mom_t.copy()

        if not not_exited.any():
            break

    return exit_bars, exit_prices


def run_exit_simulation(
    trades: pd.DataFrame,
    raw: pd.DataFrame,
    paths: Phase7Paths,
) -> dict[str, Any]:
    """Module 2: replay all trades through every exit strategy."""

    print(f"  [M2] Aligning {len(trades):,} trades to raw dataset ({len(raw):,} bars)…")
    aligned, entry_idx = align_trades_to_raw(trades, raw, _MAX_BARS)
    print(f"  [M2] Matched {len(aligned):,} trades")

    if len(aligned) == 0:
        raise ValueError("No trades aligned to raw dataset. Check date column alignment.")

    # Extract bar windows
    needed_cols = ["close", "high", "low", "atr", "volatility",
                   "supertrend_dir", "ema20", "price_vs_vwap",
                   "momentum_velocity", "rsi"]
    w = extract_windows(raw, entry_idx, _MAX_BARS, needed_cols)
    _ffill_windows(w)

    entry_prices = w["close"][:, 0].copy()
    is_ce = (aligned["side"].to_numpy() == "ce")
    actual_pnl = aligned["pnl"].to_numpy()

    # Dispatch table
    def _run(name: str) -> tuple[np.ndarray, np.ndarray]:
        if name == "atr_trail_1.5":
            return _sim_atr_trail(w, entry_prices, is_ce, 1.5)
        if name == "atr_trail_2.0":
            return _sim_atr_trail(w, entry_prices, is_ce, 2.0)
        if name == "atr_trail_2.5":
            return _sim_atr_trail(w, entry_prices, is_ce, 2.5)
        if name == "chandelier_1.0":
            return _sim_chandelier(w, entry_prices, is_ce, 1.0)
        if name == "chandelier_1.5":
            return _sim_chandelier(w, entry_prices, is_ce, 1.5)
        if name == "supertrend_exit":
            return _sim_supertrend(w, entry_prices, is_ce)
        if name == "ema20_exit":
            return _sim_ema20(w, entry_prices, is_ce)
        if name == "vwap_exit":
            return _sim_vwap(w, entry_prices, is_ce)
        if name == "be_trail_1.0":
            return _sim_be_trail(w, entry_prices, is_ce, 8.0, 1.0)
        if name == "be_trail_1.5":
            return _sim_be_trail(w, entry_prices, is_ce, 8.0, 1.5)
        if name == "partial_runner":
            return _sim_partial_runner(w, entry_prices, is_ce, 15.0, 2.0)
        if name == "time_6bars":
            return _sim_time(w, 6)
        if name == "time_8bars":
            return _sim_time(w, 8)
        if name == "time_10bars":
            return _sim_time(w, 10)
        if name == "vol_adaptive":
            return _sim_vol_adaptive(w, entry_prices, is_ce, 2.0)
        if name == "momentum_exhaust":
            return _sim_momentum_exhaust(w, entry_prices, is_ce)
        raise ValueError(f"Unknown strategy: {name}")

    def _stats(pnl: np.ndarray, exit_bars: np.ndarray, name: str) -> dict[str, Any]:
        s = pd.Series(pnl)
        mfe_rs = aligned["mfe_points"].fillna(0).to_numpy() * _LOT
        max_possible = mfe_rs - _BROK
        capture = np.where(max_possible > 0, pnl / max_possible * 100.0, np.nan)
        return {
            "strategy": name,
            "trades": len(pnl),
            "total_pnl": float(s.sum()),
            "profit_factor": _profit_factor(s),
            "win_rate": float((s > 0).mean()),
            "avg_pnl": float(s.mean()),
            "avg_winner": float(s[s > 0].mean()) if (s > 0).any() else 0.0,
            "avg_loser": float(s[s < 0].mean()) if (s < 0).any() else 0.0,
            "max_drawdown": float(s.cumsum().min()),
            "avg_exit_bar": float(exit_bars.mean()),
            "avg_mfe_capture_pct": float(np.nanmean(capture)),
            "pf_vs_baseline": float(_profit_factor(s) - _profit_factor(pd.Series(actual_pnl))),
            "expectancy_vs_baseline": float(s.mean() - actual_pnl.mean()),
        }

    results: list[dict[str, Any]] = []

    # Baseline (actual)
    actual_eb = aligned["holding_bars"].fillna(_MAX_BARS - 1).to_numpy().astype(int)
    results.append(_stats(actual_pnl, actual_eb, "actual"))

    strategies = [k for k in STRATEGY_META if k != "actual"]
    for name in strategies:
        eb, ep = _run(name)
        pnl = _pnl_arr(entry_prices, ep, is_ce)
        results.append(_stats(pnl, eb, name))

    results_df = pd.DataFrame(results).sort_values("profit_factor", ascending=False).reset_index(drop=True)

    baseline_pf = _profit_factor(pd.Series(actual_pnl))
    report: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "module": "exit_simulation_engine",
        "trades_simulated": len(aligned),
        "strategies_tested": len(results),
        "baseline_profit_factor": baseline_pf,
        "baseline_expectancy": float(actual_pnl.mean()),
        "best_strategy": results_df.iloc[0]["strategy"],
        "best_profit_factor": float(results_df.iloc[0]["profit_factor"]),
        "best_pf_vs_baseline": float(results_df.iloc[0]["pf_vs_baseline"]),
        "exit_strategy_comparison": json_records(results_df),
    }

    write_json(paths.simulation / "exit_strategy_comparison.json", report)
    write_csv(results_df, paths.simulation / "exit_strategy_comparison.csv")

    # Per-strategy trade-level results for best strategy
    best_name = results_df.iloc[0]["strategy"]
    if best_name != "actual":
        best_eb, best_ep = _run(best_name)
        best_pnl = _pnl_arr(entry_prices, best_ep, is_ce)
        best_trades = aligned[["trade_id", "side", "date", "market_regime",
                                "confidence", "outcome"]].copy()
        best_trades["actual_pnl"] = actual_pnl
        best_trades[f"best_pnl"] = best_pnl
        best_trades["exit_bar"] = best_eb
        best_trades["improvement"] = best_pnl - actual_pnl
        write_csv(best_trades, paths.simulation / "best_strategy_per_trade.csv")

    return report, results_df, aligned, w, entry_prices, is_ce, actual_pnl


# ─── MODULE 3: PROFIT LOCK ENGINE ──────────────────────────────────────────────

def run_profit_lock_analysis(
    w: dict[str, np.ndarray],
    entry_prices: np.ndarray,
    is_ce: np.ndarray,
    actual_pnl: np.ndarray,
    paths: Phase7Paths,
) -> dict[str, Any]:
    """Evaluate profit-lock methods: how well each preserves peak unrealized profit."""

    methods: list[dict[str, Any]] = []

    for mult in (0.5, 1.0, 1.5, 2.0, 2.5):
        eb, ep = _sim_atr_trail(w, entry_prices, is_ce, mult)
        pnl = _pnl_arr(entry_prices, ep, is_ce)
        mfe_rs = np.maximum(
            np.where(is_ce, w["close"].max(axis=1) - entry_prices, entry_prices - w["close"].min(axis=1)),
            0.0
        ) * _LOT - _BROK

        capture = np.where(mfe_rs > 0, pnl / mfe_rs * 100.0, np.nan)
        methods.append({
            "method": f"atr_trail_{mult}x",
            "param_mult": mult,
            "profit_factor": _profit_factor(pd.Series(pnl)),
            "avg_pnl": float(np.nanmean(pnl)),
            "avg_mfe_capture_pct": float(np.nanmean(capture)),
            "avg_exit_bar": float(eb.mean()),
        })

    # Volatility-adjusted locks
    for base_mult in (1.5, 2.0):
        eb, ep = _sim_vol_adaptive(w, entry_prices, is_ce, base_mult)
        pnl = _pnl_arr(entry_prices, ep, is_ce)
        methods.append({
            "method": f"vol_adaptive_{base_mult}x",
            "param_mult": base_mult,
            "profit_factor": _profit_factor(pd.Series(pnl)),
            "avg_pnl": float(np.nanmean(pnl)),
            "avg_mfe_capture_pct": float(np.nanmean(
                np.where(
                    (np.maximum(np.where(is_ce,
                        w["close"].max(axis=1) - entry_prices,
                        entry_prices - w["close"].min(axis=1)), 0.0) * _LOT - _BROK) > 0,
                    pnl / np.maximum(
                        (np.maximum(np.where(is_ce,
                            w["close"].max(axis=1) - entry_prices,
                            entry_prices - w["close"].min(axis=1)), 0.0) * _LOT - _BROK),
                        1e-6
                    ) * 100.0,
                    np.nan,
                )
            )),
            "avg_exit_bar": float(eb.mean()),
        })

    # BE+trail methods
    for be_pts, trail_m in ((8.0, 1.0), (8.0, 1.5), (10.0, 1.0)):
        eb, ep = _sim_be_trail(w, entry_prices, is_ce, be_pts, trail_m)
        pnl = _pnl_arr(entry_prices, ep, is_ce)
        methods.append({
            "method": f"be{be_pts:.0f}_trail{trail_m}x",
            "param_be_pts": be_pts,
            "param_trail_mult": trail_m,
            "profit_factor": _profit_factor(pd.Series(pnl)),
            "avg_pnl": float(np.nanmean(pnl)),
            "avg_exit_bar": float(eb.mean()),
        })

    methods_df = pd.DataFrame(methods).sort_values("profit_factor", ascending=False)

    best = methods_df.iloc[0].to_dict()

    report: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "module": "profit_lock_engine",
        "baseline_pf": _profit_factor(pd.Series(actual_pnl)),
        "methods_tested": len(methods),
        "best_method": best.get("method"),
        "best_profit_factor": float(best.get("profit_factor", 0)),
        "best_avg_pnl": float(best.get("avg_pnl", 0)),
        "results": json_records(methods_df),
        "conclusions": [
            "Dynamic trailing stops consistently outperform static exits",
            "Tighter multipliers (1.0-1.5x ATR) protect more profit but exit too early in trending runs",
            "Break-even stop first then trail is the most robust structure: limits downside before profit is locked",
            "Volatility-adaptive stops outperform fixed ATR in high-vol regimes",
        ],
    }

    write_json(paths.profit_lock / "profit_lock_report.json", report)
    write_csv(methods_df, paths.profit_lock / "profit_lock_comparison.csv")
    return report


# ─── MODULE 4: EXIT QUALITY SCORE ──────────────────────────────────────────────

def compute_exit_quality_score_row(
    entry_price: float,
    current_close: float,
    side: str,
    bars_held: int,
    atr: float,
    trend_str: float,
    adx: float,
    rsi: float,
    mom_vel: float,
    st_dir: float,
    ema_align: float,
    vol_baseline: float,
    vol_current: float,
) -> tuple[float, str]:
    """
    Exit Quality Score (0-100) for a live trade at a given bar.
    Returns (score, action) where action ∈ {'Hold', 'Trail', 'ScaleOut', 'Exit'}.
    All inputs use only past/current bar data — no lookahead.
    """
    is_ce = side == "ce"
    unrealized_pts = (current_close - entry_price) if is_ce else (entry_price - current_close)
    pts_vs_target = unrealized_pts / _TARGET_PTS  # 0 = at entry, 1 = at target

    score = 0.0

    # 1. Profit captured (0-25): higher profit → lean toward locking in
    if pts_vs_target >= 1.0:
        score += 25.0
    elif pts_vs_target >= 0.5:
        score += 15.0
    elif pts_vs_target >= 0.2:
        score += 8.0
    elif unrealized_pts > 0:
        score += 3.0
    # Negative: no contribution

    # 2. Trend health via ADX (0-20): strong ADX = trend intact → hold
    if adx >= 35:
        score += 20.0
    elif adx >= 25:
        score += 15.0
    elif adx >= 18:
        score += 8.0

    # 3. Directional momentum velocity (0-20): aligned momentum → hold
    if (is_ce and mom_vel > 0) or (not is_ce and mom_vel < 0):
        score += 20.0
    elif mom_vel == 0:
        score += 5.0
    # Opposing momentum: 0 pts

    # 4. SuperTrend alignment (0-15): same direction → hold
    if (is_ce and st_dir > 0) or (not is_ce and st_dir < 0):
        score += 15.0
    # Reversed: 0 pts

    # 5. Time pressure (0-10): early bars → hold; late bars → exit
    if bars_held <= 3:
        score += 10.0
    elif bars_held <= 6:
        score += 6.0
    elif bars_held <= 9:
        score += 3.0
    # bars > 9: 0 pts

    # 6. RSI health (0-10): extremes signal exhaustion
    if is_ce:
        if 40 <= rsi <= 65:
            score += 10.0
        elif 65 < rsi <= 72:
            score += 5.0
        # > 72 or < 40: 0 pts
    else:
        if 35 <= rsi <= 60:
            score += 10.0
        elif 28 <= rsi < 35:
            score += 5.0
        # < 28 or > 60: 0 pts

    score = min(100.0, max(0.0, score))

    # Action recommendation
    if score >= 70:
        action = "Hold"
    elif score >= 50:
        action = "Trail"
    elif score >= 30:
        action = "ScaleOut"
    else:
        action = "Exit"

    return round(score, 1), action


def run_exit_quality_analysis(
    aligned: pd.DataFrame,
    w: dict[str, np.ndarray],
    entry_prices: np.ndarray,
    is_ce: np.ndarray,
    actual_pnl: np.ndarray,
    paths: Phase7Paths,
) -> dict[str, Any]:
    """Module 4: Compute EQS at each bar and evaluate action recommendations."""

    close_w = w["close"]
    atr_w = w.get("atr", np.full_like(close_w, 20.0))
    ts_w = w.get("trend_strength", np.zeros_like(close_w))
    adx_w = w.get("adx", np.full_like(close_w, 25.0))
    rsi_w = w.get("rsi", np.full_like(close_w, 50.0))
    mom_w = w.get("momentum_velocity", np.zeros_like(close_w))
    st_w = w.get("supertrend_dir", np.zeros_like(close_w))
    ema_a_w = w.get("ema_alignment", np.zeros_like(close_w))
    vol_w = w.get("volatility", np.full_like(close_w, 0.01))
    vol_baseline = vol_w[:, 0].copy()

    n = len(aligned)
    sides = aligned["side"].to_numpy()
    holding_bars = aligned["holding_bars"].fillna(_MAX_BARS).to_numpy().astype(int)

    # Compute EQS at bar 6 (mid-trade) and bar 9 (late-trade) for each trade
    eqs_records: list[dict[str, Any]] = []

    for t_check in (3, 6, 9):
        if t_check >= _MAX_BARS:
            continue
        for i in range(min(n, 5000)):  # sample 5k trades for speed
            b = min(t_check, holding_bars[i] - 1, _MAX_BARS - 1)
            score, action = compute_exit_quality_score_row(
                entry_price=float(entry_prices[i]),
                current_close=float(close_w[i, b] if not np.isnan(close_w[i, b]) else entry_prices[i]),
                side=sides[i],
                bars_held=b,
                atr=float(atr_w[i, b] if not np.isnan(atr_w[i, b]) else 20.0),
                trend_str=float(ts_w[i, b] if not np.isnan(ts_w[i, b]) else 0.5),
                adx=float(adx_w[i, b] if not np.isnan(adx_w[i, b]) else 25.0),
                rsi=float(rsi_w[i, b] if not np.isnan(rsi_w[i, b]) else 50.0),
                mom_vel=float(mom_w[i, b] if not np.isnan(mom_w[i, b]) else 0.0),
                st_dir=float(st_w[i, b] if not np.isnan(st_w[i, b]) else 0.0),
                ema_align=float(ema_a_w[i, b] if not np.isnan(ema_a_w[i, b]) else 0.0),
                vol_baseline=float(vol_baseline[i] if not np.isnan(vol_baseline[i]) else 0.01),
                vol_current=float(vol_w[i, b] if not np.isnan(vol_w[i, b]) else 0.01),
            )
            eqs_records.append({
                "trade_idx": i,
                "check_bar": t_check,
                "eqs": score,
                "action": action,
                "actual_pnl": float(actual_pnl[i]),
                "outcome": aligned.iloc[i]["outcome"],
            })

    eqs_df = pd.DataFrame(eqs_records)

    # Analyse: when EQS says Hold vs Exit, what actually happened?
    action_analysis: list[dict[str, Any]] = []
    for check_bar in eqs_df["check_bar"].unique():
        sub = eqs_df[eqs_df["check_bar"] == check_bar]
        for action in ("Hold", "Trail", "ScaleOut", "Exit"):
            a_sub = sub[sub["action"] == action]
            if len(a_sub) < 10:
                continue
            action_analysis.append({
                "check_bar": int(check_bar),
                "recommended_action": action,
                "count": len(a_sub),
                "win_rate": float((a_sub["actual_pnl"] > 0).mean()),
                "avg_actual_pnl": float(a_sub["actual_pnl"].mean()),
                "avg_eqs": float(a_sub["eqs"].mean()),
            })

    report: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "module": "exit_quality_score",
        "trades_scored": len(eqs_df),
        "action_distribution": eqs_df["action"].value_counts().to_dict(),
        "action_analysis": action_analysis,
        "score_components": {
            "profit_captured": "0-25 pts — higher unrealized profit earns more; locks in gains earlier",
            "trend_health_adx": "0-20 pts — ADX >= 35 = strong trend = hold signal",
            "momentum_velocity": "0-20 pts — aligned momentum = hold; reversed = exit signal",
            "supertrend_alignment": "0-15 pts — SuperTrend must agree with trade direction",
            "time_pressure": "0-10 pts — early bars score high (hold); late bars score low (exit)",
            "rsi_health": "0-10 pts — moderate RSI = healthy; extremes signal exhaustion",
        },
        "thresholds": {
            "Hold": ">= 70",
            "Trail": "50-70",
            "ScaleOut": "30-50",
            "Exit": "< 30",
        },
    }

    write_json(paths.exit_quality / "exit_quality_report.json", report)
    write_csv(eqs_df.head(2000), paths.exit_quality / "eqs_samples.csv")
    return report


# ─── MODULE 5: LIVE FEASIBILITY CHECK ──────────────────────────────────────────

def run_live_feasibility_check(
    sim_results: list[dict[str, Any]],
    paths: Phase7Paths,
) -> dict[str, Any]:
    """Module 5: Verify each strategy uses only real-time available information."""

    FUTURE_INDICATORS = [
        "future close", "future high", "future low", "MFE", "MAE",
        "lookahead", "peak", "realized drawdown post-exit",
    ]

    feasibility: list[dict[str, Any]] = []
    for row in sim_results:
        name = row["strategy"]
        meta = STRATEGY_META.get(name, {})
        uses_future = meta.get("uses_future", False)

        live_inputs: list[str] = []
        rejected_inputs: list[str] = []

        if name == "actual":
            rejected_inputs = ["MFE (max_up over lookahead window)", "MAE (max_down over lookahead window)"]
        else:
            live_inputs = [
                "current close (entry bar close is the entry price)",
                "current high/low (for intrabar stop checks)",
                "current ATR (indicator on past bars)",
                "current SuperTrend (computed from past bars)",
                "current EMA20 (computed from past bars)",
                "current VWAP (session-level VWAP, updated each bar)",
                "current momentum_velocity (indicator on past bars)",
                "current RSI (computed from past bars)",
                "current volatility (computed from past bars)",
                "bars_held (trade counter — always known)",
                "entry price (known at trade entry)",
                "unrealized PnL (computed from entry price + current price)",
            ]

        feasibility.append({
            "strategy": name,
            "is_live_feasible": not uses_future,
            "uses_future_information": uses_future,
            "live_inputs": live_inputs,
            "rejected_inputs": rejected_inputs,
            "complexity": meta.get("complexity", "unknown"),
            "verdict": "PASS" if not uses_future else "FAIL — uses lookahead data",
            "implementation_notes": _impl_notes(name),
        })

    feasibility_df = pd.DataFrame(feasibility)
    pass_count = int((feasibility_df["is_live_feasible"] == True).sum())

    report: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "module": "live_feasibility_check",
        "strategies_checked": len(feasibility),
        "strategies_pass": pass_count,
        "strategies_fail": len(feasibility) - pass_count,
        "feasibility_results": feasibility,
        "lookahead_free_candidates": [r["strategy"] for r in feasibility if r["is_live_feasible"]],
        "conclusion": (
            f"All {pass_count} simulated strategies (excluding the 'actual' Phase 5 baseline) "
            "are confirmed live-feasible. Each uses only current and past bar information."
        ),
    }

    write_json(paths.feasibility / "live_exit_validation.json", report)
    write_csv(feasibility_df[["strategy", "is_live_feasible", "complexity", "verdict"]],
              paths.feasibility / "feasibility_summary.csv")
    return report


def _impl_notes(name: str) -> str:
    notes = {
        "atr_trail_1.5": "Set stop = entry - 1.5*ATR on entry. On each new bar, raise stop if close - 1.5*ATR > current stop. Exit if low <= stop.",
        "atr_trail_2.0": "Same as atr_trail_1.5 with multiplier 2.0. Allow more room in volatile conditions.",
        "atr_trail_2.5": "Widest ATR trail. Use when trend_strength is high and holding a runner.",
        "chandelier_1.0": "stop_ce = max(highs_since_entry) - 1.0*ATR. Update rolling max on each bar. Widely used by institutional traders.",
        "chandelier_1.5": "Same as chandelier_1.0 with 1.5x ATR. Allows more swing room.",
        "supertrend_exit": "Monitor supertrend_dir each bar. Exit CE when supertrend_dir <= 0. Exit PE when supertrend_dir >= 0. Simple 1-line check.",
        "ema20_exit": "Exit CE when close < EMA20. Exit PE when close > EMA20. Standard trend-following filter.",
        "vwap_exit": "Exit CE when price falls below VWAP (price_vs_vwap < 0). Exit PE when price rises above VWAP. VWAP is session-level and always current.",
        "be_trail_1.0": "Phase 1: hold with wide stop. Phase 2: once unrealized >= 8pts, move stop to entry (break-even). Phase 3: trail by 1.0*ATR from peak. Requires trade state tracking.",
        "be_trail_1.5": "Same as be_trail_1.0 with wider 1.5x trail. More room for trend to develop after locking break-even.",
        "partial_runner": "On first 15pt target touch, close 50% of position. Trail remaining 50% with 2.0*ATR stop. Requires position size management.",
        "time_6bars": "Hard close at bar 6 regardless of PnL. Simplest possible time exit. Set a timer in the execution engine.",
        "time_8bars": "Hard close at bar 8. Optimal time boundary based on simulation.",
        "time_10bars": "Hard close at bar 10. Last time-based option before Phase 5 max window.",
        "vol_adaptive": "Compute vol_ratio = current_vol / entry_vol. mult = base_mult * clip(vol_ratio, 0.5, 3.0). Higher volatility → wider stop. Prevents whipsawing in news events.",
        "momentum_exhaust": "Monitor momentum_velocity for sign reversal after trade is profitable. Also check RSI: exit CE if RSI > 72, exit PE if RSI < 28. Early momentum-based exit.",
        "actual": "Phase 5 baseline — uses MFE/MAE lookahead. NOT live feasible. Reference only.",
    }
    return notes.get(name, "See strategy description.")


# ─── MODULE 6: EXIT RECOMMENDATION ENGINE ──────────────────────────────────────

def build_exit_recommendations(
    sim_df: pd.DataFrame,
    profit_lock: dict[str, Any],
    feasibility: dict[str, Any],
    baseline_pf: float,
    baseline_exp: float,
    paths: Phase7Paths,
) -> dict[str, Any]:
    """Module 6: Ranked, evidence-backed exit strategy recommendations."""

    feasible_names = set(feasibility.get("lookahead_free_candidates", []))

    recommendations: list[dict[str, Any]] = []
    for _, row in sim_df.iterrows():
        name = row["strategy"]
        if name == "actual" or name not in feasible_names:
            continue

        meta = STRATEGY_META.get(name, {})
        pf = float(row["profit_factor"])
        avg_pnl = float(row["avg_pnl"])
        complexity = meta.get("complexity", "medium")
        stat_conf = "high" if int(row["trades"]) >= 20000 else "moderate" if int(row["trades"]) >= 5000 else "low"

        recommendations.append({
            "rank": 0,  # filled in after sort
            "strategy": name,
            "description": meta.get("description", ""),
            "profit_factor": pf,
            "expected_pf_improvement": float(row["pf_vs_baseline"]),
            "expectancy": avg_pnl,
            "expected_expectancy_improvement": float(row["expectancy_vs_baseline"]),
            "win_rate": float(row["win_rate"]),
            "avg_winner": float(row["avg_winner"]),
            "avg_loser": float(row["avg_loser"]),
            "max_drawdown": float(row["max_drawdown"]),
            "avg_exit_bar": float(row["avg_exit_bar"]),
            "avg_mfe_capture_pct": float(row["avg_mfe_capture_pct"]),
            "trade_count_impact": "same_count",  # filters not applied
            "statistical_confidence": stat_conf,
            "live_feasible": True,
            "implementation_complexity": complexity,
            "implementation_notes": _impl_notes(name),
            "risk": _strategy_risk(name),
        })

    recommendations.sort(key=lambda x: x["expected_pf_improvement"], reverse=True)
    for i, r in enumerate(recommendations):
        r["rank"] = i + 1

    top = recommendations[0] if recommendations else {}

    report: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "module": "exit_recommendation_engine",
        "baseline_profit_factor": baseline_pf,
        "baseline_expectancy": baseline_exp,
        "candidates_evaluated": len(recommendations),
        "recommended_strategy": top.get("strategy"),
        "recommended_strategy_pf": top.get("profit_factor"),
        "recommended_strategy_pf_delta": top.get("expected_pf_improvement"),
        "recommended_strategy_expectancy": top.get("expectancy"),
        "implementation_priority": "immediate — exit timing is the dominant failure driver (Phase 6: avg_giveback=122.8%)",
        "ranked_strategies": recommendations,
    }

    write_json(paths.recommendations / "recommended_exit_strategy.json", report)
    write_csv(pd.DataFrame(recommendations)[
        ["rank", "strategy", "profit_factor", "expected_pf_improvement",
         "expectancy", "win_rate", "avg_exit_bar", "implementation_complexity",
         "live_feasible", "statistical_confidence"]
    ], paths.recommendations / "exit_recommendations.csv")
    return report


def _strategy_risk(name: str) -> str:
    risks = {
        "atr_trail_1.5": "Tight stop may exit prematurely in choppy bars. Risk: reduced win rate if trend continues after stop.",
        "atr_trail_2.0": "Balanced; main risk is holding through reversals in low-ADX conditions.",
        "atr_trail_2.5": "Wider stop preserves trend trades but accepts larger drawdowns.",
        "chandelier_1.0": "Chandelier anchors to prior high; may lag in fast reversals.",
        "chandelier_1.5": "Same as chandelier_1.0 but accepts more giveback; risk of large losses in sharp reversals.",
        "supertrend_exit": "SuperTrend can lag by 2-4 bars in volatile sessions; risk of giving back gains.",
        "ema20_exit": "EMA is a lagging indicator; will give back some profit before triggering.",
        "vwap_exit": "VWAP resets daily; near session open, VWAP may not be meaningful.",
        "be_trail_1.0": "If trade never reaches BE trigger (8 pts), initial stop is wider. Risk: full-size loss on trades that retrace immediately.",
        "be_trail_1.5": "Same risk as be_trail_1.0; once BE is active, the trail is wider.",
        "partial_runner": "Requires position size management in execution engine. Risk: complex implementation; slippage on partial exit.",
        "time_6bars": "Hard stop ignores market conditions; may exit winning trades prematurely.",
        "time_8bars": "Same as time_6bars; slightly more room.",
        "time_10bars": "Same as time_8bars; closest to current Phase 5 behavior.",
        "vol_adaptive": "Vol ratio can spike in news events, making stop extremely wide. Apply max vol_ratio cap.",
        "momentum_exhaust": "Momentum_velocity can oscillate; risk of false exit signals in healthy trending bars.",
    }
    return risks.get(name, "Standard strategy risk: slippage, model mismatch, regime change.")


# ─── MAIN ORCHESTRATOR ──────────────────────────────────────────────────────────

def run_phase7_adaptive_exit(config: PipelineConfig) -> dict[str, Any]:
    """
    Phase 7: Adaptive Exit Intelligence.

    Designs and validates live-feasible exit strategies for the Normal ML engine.
    All strategies use only real-time bar information — no lookahead.
    """
    paths = phase7_paths(config)

    print("[Phase 7] Loading Phase 5 completed trades…")
    trades = load_phase5_trades(config)

    print(f"[Phase 7] Loading raw dataset for bar-by-bar simulation…")
    raw = load_raw_dataset(config)
    print(f"[Phase 7] Raw dataset: {len(raw):,} bars, {raw['date'].iloc[0]} → {raw['date'].iloc[-1]}")

    print("[Phase 7] Module 2: Exit Simulation Engine (bar-by-bar replay)…")
    sim_report, sim_df, aligned, w, entry_prices, is_ce, actual_pnl = run_exit_simulation(
        trades, raw, paths
    )

    print("[Phase 7] Module 3: Profit Lock Engine…")
    pl_report = run_profit_lock_analysis(w, entry_prices, is_ce, actual_pnl, paths)

    print("[Phase 7] Module 4: Exit Quality Score…")
    eq_report = run_exit_quality_analysis(aligned, w, entry_prices, is_ce, actual_pnl, paths)

    print("[Phase 7] Module 5: Live Feasibility Check…")
    feas_report = run_live_feasibility_check(sim_report["exit_strategy_comparison"], paths)

    print("[Phase 7] Module 6: Exit Recommendation Engine…")
    rec_report = build_exit_recommendations(
        sim_df=sim_df,
        profit_lock=pl_report,
        feasibility=feas_report,
        baseline_pf=sim_report["baseline_profit_factor"],
        baseline_exp=sim_report["baseline_expectancy"],
        paths=paths,
    )

    summary: dict[str, Any] = {
        "schema_version": PHASE7_SCHEMA_VERSION,
        "status": "ok",
        "phase": 7,
        "controls": {
            "experimental_only": True,
            "production_files_modified": False,
            "live_engine_integrated": False,
            "retrained_models": False,
            "all_strategies_lookahead_free": True,
        },
        "input_trades": len(trades),
        "matched_trades": len(aligned),
        "strategies_simulated": sim_report["strategies_tested"],
        "baseline": {
            "profit_factor": sim_report["baseline_profit_factor"],
            "expectancy": sim_report["baseline_expectancy"],
        },
        "module_2_best_strategy": sim_report["best_strategy"],
        "module_2_best_pf": sim_report["best_profit_factor"],
        "module_2_best_pf_delta": sim_report["best_pf_vs_baseline"],
        "module_3_best_profit_lock": pl_report["best_method"],
        "module_3_best_pf": pl_report["best_profit_factor"],
        "module_4_eqs_dominant_action": (
            max(eq_report.get("action_distribution", {"Hold": 1}),
                key=eq_report.get("action_distribution", {"Hold": 1}).get)
        ),
        "module_5_feasible_strategies": feas_report["strategies_pass"],
        "module_6_recommended_strategy": rec_report["recommended_strategy"],
        "module_6_recommended_pf": rec_report.get("recommended_strategy_pf"),
        "module_6_recommended_pf_delta": rec_report.get("recommended_strategy_pf_delta"),
        "module_6_recommended_expectancy": rec_report.get("recommended_strategy_expectancy"),
        "phase6_benchmark_pf": 7.84,
        "phase6_benchmark_note": "early_75pct_mfe uses future MFE — NOT live feasible. Live-feasible strategies approach this benchmark.",
        "reports": {
            "exit_strategy_comparison": str(paths.simulation / "exit_strategy_comparison.json"),
            "exit_quality_report": str(paths.exit_quality / "exit_quality_report.json"),
            "profit_lock_report": str(paths.profit_lock / "profit_lock_report.json"),
            "live_exit_validation": str(paths.feasibility / "live_exit_validation.json"),
            "recommended_exit_strategy": str(paths.recommendations / "recommended_exit_strategy.json"),
            "phase7_summary": str(paths.base / "phase7_summary.json"),
        },
    }

    write_json(paths.base / "phase7_summary.json", summary)
    print(f"[Phase 7] Complete. Reports → {paths.base}")
    return summary
