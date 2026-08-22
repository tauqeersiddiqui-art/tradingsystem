# ml/dataset_builder.py
# Direction-PREDICTING dataset (first-touch barrier labels).
#
# Merged from dataset_builder_v3 (labels) + dataset_builder_v2 (feature
# computation — single source of truth for indicators).
#
# WHY the v3 label scheme exists — the wrong-direction root cause:
#   v2 labelled CE only on Supertrend=UP bars and PE only on Supertrend=DOWN
#   bars, then trained each model on its own slice. The model therefore never
#   learned to PREDICT direction — it only learned "given the trend is already
#   up, will it continue?". At reversals (local tops/bottoms) the 1m Supertrend
#   flips the WRONG way, the model rubber-stamps it, and the trade goes
#   immediately negative. That is the "wrong direction most of the time" bug.
#
# v3 FIX (kept):
#   * Label EVERY active-session bar (no direction-eligibility filter).
#   * First-touch barrier labels: for each bar, does price reach +TARGET
#     (CE wins) or -TARGET (PE wins) FIRST within LOOKAHEAD? This is a true
#     directional label — the model learns which way price breaks next.
#   * Supertrend / VWAP / ADX stay as FEATURES so the model can learn to
#     trust or distrust them, instead of being gated by them.
#
# Output is drop-in compatible with the live feature pipeline (same 36
# FEATURE_COLUMNS) so predictor_champion.py needs no change.
#
# RUN:
#   python ml/dataset_builder.py
#   -> ml/models/training_dataset.csv

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from ml.indicators import supertrend as compute_supertrend, adx as compute_adx, vwap_session

DATA_PATH = "data/historical/nifty_1m_full.csv"
OUTPUT    = "ml/models/training_dataset.csv"

ACTIVE_WINDOWS = [
    (9 * 60 + 30,  11 * 60),      # 9:30–11:00
    (14 * 60,      15 * 60 + 15), # 14:00–15:15
]


def _in_active_session(mins_from_midnight: float) -> bool:
    for start, end in ACTIVE_WINDOWS:
        if start <= mins_from_midnight < end:
            return True
    return False


def _compute_rsi(series: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(series)
    rsi = np.full(n, 50.0)
    delta = np.diff(series, prepend=series[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.zeros(n)
    avg_loss = np.zeros(n)
    if n >= period:
        avg_gain[period - 1] = gains[1:period].mean()
        avg_loss[period - 1] = losses[1:period].mean()
        for i in range(period, n):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
    return rsi


def _ema(series: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    result = np.zeros(len(series))
    result[0] = series[0]
    for i in range(1, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def compute_all_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns to df (modifies in place, returns df)."""
    high   = df["high"].values.astype(float)
    low    = df["low"].values.astype(float)
    close  = df["close"].values.astype(float)
    open_  = df["open"].values.astype(float)
    vol    = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(len(df))

    # ── Supertrend ────────────────────────────────────────────────────
    st_dir, st_line = compute_supertrend(high, low, close, SUPERTREND_PERIOD, SUPERTREND_MULT)
    df["supertrend_dir"]  = st_dir.astype(float)
    df["supertrend_line"] = st_line
    df["supertrend_dist"] = np.where(close != 0, (close - st_line) / close, 0.0)
    df["supertrend_dist"] = df["supertrend_dist"].clip(-0.05, 0.05)

    # ── VWAP ─────────────────────────────────────────────────────────
    vwap = vwap_session(high, low, close, vol, df["date"].values)
    df["vwap"]          = vwap
    df["price_vs_vwap"] = np.where(close != 0, (close - vwap) / close, 0.0)
    df["price_vs_vwap"] = df["price_vs_vwap"].clip(-0.05, 0.05)

    # ── ADX ──────────────────────────────────────────────────────────
    adx_arr, di_plus, di_minus = compute_adx(high, low, close, ADX_PERIOD)
    df["adx"]       = adx_arr.clip(0, 100)
    df["di_plus"]   = di_plus
    df["di_minus"]  = di_minus
    df["di_spread"] = (di_plus - di_minus).clip(-60, 60)

    # ── EMAs ─────────────────────────────────────────────────────────
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    df["ema20"]         = ema20
    df["ema50"]         = ema50
    df["macd"]          = ema20 - ema50
    df["trend_strength"]= np.where(close != 0, (ema20 - ema50) / close, 0.0)
    df["ema_alignment"] = np.where(ema20 > ema50, 1.0, -1.0)

    # ── RSI ──────────────────────────────────────────────────────────
    df["rsi"] = _compute_rsi(close)

    # ── ATR ──────────────────────────────────────────────────────────
    from ml.indicators import atr_wilder
    df["atr"] = atr_wilder(high, low, close, 14).clip(0.5, None)

    # ── Returns / volatility ─────────────────────────────────────────
    df["returns"]  = pd.Series(close).pct_change().fillna(0).values
    df["return_1"] = df["returns"]
    df["return_3"] = pd.Series(close).pct_change(3).fillna(0).values
    df["volatility"]= pd.Series(df["returns"].values).rolling(20).std().fillna(0.001).clip(0, 0.02).values

    # ── Volume ratio ──────────────────────────────────────────────────
    avg_vol = pd.Series(vol).rolling(20, min_periods=1).mean().values
    df["volume_ratio"] = np.where(avg_vol > 0, (vol / avg_vol).clip(0, 10), 1.0)

    # ── Time features ─────────────────────────────────────────────────
    df["hour"]    = df["date"].dt.hour
    df["weekday"] = df["date"].dt.weekday

    mkt_open  = df["date"].dt.normalize() + pd.Timedelta(hours=9, minutes=15)
    mkt_close = df["date"].dt.normalize() + pd.Timedelta(hours=15, minutes=30)
    df["mins_since_open"] = ((df["date"] - mkt_open).dt.total_seconds() / 60).clip(0, 375)
    df["mins_to_close"]   = ((mkt_close - df["date"]).dt.total_seconds() / 60).clip(0, 375)
    df["session_open"]    = (df["mins_since_open"] < 30).astype(int)
    df["session_close"]   = (df["mins_to_close"] < 60).astype(int)
    df["time_to_expiry_min"] = df["mins_to_close"].clip(0, 375)

    # ── Moneyness ─────────────────────────────────────────────────────
    df["moneyness"] = np.where(close != 0, (close - ema20) / close, 0.0).clip(-0.02, 0.02)

    # ── Candle structure ──────────────────────────────────────────────
    hl   = high - low
    body = np.abs(close - open_)
    wick = hl - body

    df["candle_body_pct"] = np.where(hl > 0, body / hl, 0.5)
    df["body_efficiency"] = np.where(hl > 0, body / hl, 0.5)
    df["wick_ratio"]      = np.where(body > 0, (wick / body).clip(0, 10), 0.0)
    df["upper_wick"]      = (high - np.maximum(close, open_)) / (df["atr"].values + 1e-6)
    df["lower_wick"]      = (np.minimum(close, open_) - low)  / (df["atr"].values + 1e-6)
    df["close_position"]  = np.where(hl > 0, (close - low) / hl, 0.5)

    roll_high = pd.Series(high).rolling(10).max().values
    df["range_break_strength"] = ((close - roll_high) / (df["atr"].values + 1e-6)).clip(-5, 5)

    # ── Momentum / compression ────────────────────────────────────────
    df["momentum_velocity"] = pd.Series(df["returns"].values).diff().fillna(0).values
    df["mom3_strength"]     = np.abs(pd.Series(close).pct_change(3).fillna(0).values)

    r5  = pd.Series(high).rolling(5).max().values  - pd.Series(low).rolling(5).min().values
    r15 = pd.Series(high).rolling(15).max().values - pd.Series(low).rolling(15).min().values
    df["range_compression"] = (r5 / (r15 + 1e-6)).clip(0, 2)

    return df


# ── Label parameters ──────────────────────────────────────────────────
LOOKAHEAD          = 12      # candles to look forward
TARGET_SPOT_POINTS = 50      # barrier distance in spot points
START_DATE         = os.getenv("V3_START_DATE", "2021-01-01")  # regime relevance


def create_first_touch_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    First-touch barrier labels on EVERY active-session bar.

    For bar i, look at the next LOOKAHEAD bars:
      - up_hit   = first bar whose HIGH reaches close[i] + TARGET
      - down_hit = first bar whose LOW  reaches close[i] - TARGET
      label_ce = 1 if up_hit occurs first (CE would profit)
      label_pe = 1 if down_hit occurs first (PE would profit)
    A bar where neither barrier is touched gets both labels = 0 (chop/no-trade)
    so the model learns to output LOW probability there.
    """
    n      = len(df)
    close  = df["close"].values.astype(float)
    high   = df["high"].values.astype(float)
    low    = df["low"].values.astype(float)

    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    in_session = mins_from_midnight.apply(_in_active_session).values

    label_ce = np.zeros(n, dtype=np.int8)
    label_pe = np.zeros(n, dtype=np.int8)

    tgt = TARGET_SPOT_POINTS
    for i in range(n - LOOKAHEAD):
        if not in_session[i]:
            continue
        up_target = close[i] + tgt
        dn_target = close[i] - tgt
        up_hit = dn_hit = None
        for j in range(i + 1, i + LOOKAHEAD + 1):
            if up_hit is None and high[j] >= up_target:
                up_hit = j
            if dn_hit is None and low[j] <= dn_target:
                dn_hit = j
            if up_hit is not None and dn_hit is not None:
                break
        if up_hit is not None and (dn_hit is None or up_hit <= dn_hit):
            label_ce[i] = 1
        elif dn_hit is not None and (up_hit is None or dn_hit < up_hit):
            label_pe[i] = 1

    df["label_ce"] = label_ce
    df["label_pe"] = label_pe
    # Kept for compatibility with any downstream code; in v3 every bar is
    # eligible for both directions (the model decides).
    df["ce_eligible"] = in_session
    df["pe_eligible"] = in_session
    return df


def main():
    print("=" * 64)
    print("  DIRECTIONAL DATASET BUILDER  (first-touch barrier labels)")
    print("=" * 64)
    print(f"  Target={TARGET_SPOT_POINTS}pt  Lookahead={LOOKAHEAD}  Start={START_DATE}")

    print(f"\n[DATA] Loading {DATA_PATH} ...")
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[df["date"] >= pd.Timestamp(START_DATE)].reset_index(drop=True)
    print(f"[DATA] {len(df):,} rows | {df['date'].min().date()} -> {df['date'].max().date()}")

    print("[FEATURES] Computing indicators ...")
    df = compute_all_features(df)

    print("[LABELS] First-touch directional labels (this is the slow part) ...")
    df = create_first_touch_labels(df)

    df = df.dropna().reset_index(drop=True)

    ce_rate = df["label_ce"].mean()
    pe_rate = df["label_pe"].mean()
    flat    = 1.0 - ce_rate - pe_rate
    print(f"\n  Bars: {len(df):,}")
    print(f"  label_ce=1 : {ce_rate:.1%}   label_pe=1 : {pe_rate:.1%}   neither(flat): {flat:.1%}")
    print("  (CE+PE should be roughly balanced; large skew = directional bias in data)")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print(f"\n[SAVED] {OUTPUT}  ({len(df):,} rows)")
    print("  Next: python ml/trainer.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
