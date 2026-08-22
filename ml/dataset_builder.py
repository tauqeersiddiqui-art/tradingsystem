# ml/dataset_builder.py
# ENTRY-QUALITY dataset (replaces the v3 first-touch DIRECTIONAL labels).
#
# LABEL SEMANTICS (current):
#   For each active-session bar i, with H = ENTRY_HORIZON_BARS (5 bars,
#   matching actual scalp hold times):
#     future_max_up   = max(high[i+1 .. i+H]) - close[i]
#     future_max_down = close[i] - min(low[i+1 .. i+H])
#     label_ce = 1 if future_max_up   >= QUALITY_THRESHOLD_PTS (25 spot pts)
#     label_pe = 1 if future_max_down >= QUALITY_THRESHOLD_PTS
#   i.e. the models learn ENTRY QUALITY — "is a CE/PE entered NOW likely to
#   see >= 25 favorable spot points within the next 5 minutes?" — NOT which
#   direction price breaks first.
#   BAD_ENTRY guard: if the prior 20-bar move is already extended
#   (> EXTENDED_MOVE_PCT of price), the entry is late — the corresponding
#   label is forced to 0 and bad_entry_ce / bad_entry_pe is set to 1
#   (kept in the CSV for auditing).
#   Bars with fewer than H forward bars (day end) get NaN labels and are
#   dropped — never NaN-filled.
#
# WHY the change — direction labels trained the models to predict break
# direction, but live scalps are entered AFTER a move; the real question is
# whether the entry still has enough favorable excursion left. Entry-quality
# labels encode exactly that.
#
# Supertrend / VWAP / ADX stay as FEATURES so the model can learn to trust
# or distrust them, instead of being gated by them.
#
# Output is drop-in compatible with the live feature pipeline (same 36
# FEATURE_COLUMNS) so predictor_champion.py needs no change.
#
# Also exposes validate_training_csv() — the shared fail-hard precondition
# used by trainer.py and feedback_trainer.py before any training run.
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
from ml.feature_config import FEATURE_COLUMNS

DATA_PATH = "data/historical/nifty_1m_full.csv"
OUTPUT    = "ml/models/training_dataset.csv"

ACTIVE_WINDOWS = [
    (9 * 60 + 30,  11 * 60),      # 9:30–11:00
    (14 * 60,      15 * 60 + 15), # 14:00–15:15
]

# Indicator periods (match ml.indicators defaults)
SUPERTREND_PERIOD = 10
SUPERTREND_MULT   = 3.0
ADX_PERIOD        = 14


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


# ── Label parameters (ENTRY-QUALITY scheme) ───────────────────────────
ENTRY_HORIZON_BARS    = int(os.getenv("ENTRY_HORIZON_BARS", "5"))    # H forward bars — matches scalp hold times
QUALITY_THRESHOLD_PTS = float(os.getenv("QUALITY_THRESHOLD_PTS", "25.0"))  # favorable spot points required
EXTENDED_LOOKBACK     = 20     # bars used for the BAD_ENTRY extension check
EXTENDED_MOVE_PCT     = 0.004  # prior move above this fraction of price = entry already extended
START_DATE            = os.getenv("V3_START_DATE", "2021-01-01")  # regime relevance

# ── Shared validation constants (see validate_training_csv) ──────────
REQUIRED_LABEL_COLUMNS = ["label_ce", "label_pe"]
MIN_TRAINING_ROWS      = 1000
MAX_NAN_PCT            = 0.02


def validate_training_csv(path: str) -> pd.DataFrame:
    """Shared fail-hard precondition used by trainer.py and
    feedback_trainer.py before any training run.

    Raises:
      FileNotFoundError — dataset file missing
      RuntimeError      — rows <= MIN_TRAINING_ROWS, required columns
                          missing, or > MAX_NAN_PCT NaN in any required
                          column (FEATURE_COLUMNS + label columns)
    Logs the NaN % of every required column before returning the frame.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Training dataset missing: {path} — run 'python ml/dataset_builder.py' first.")
    df = pd.read_csv(path)
    required = list(FEATURE_COLUMNS) + REQUIRED_LABEL_COLUMNS
    if len(df) <= MIN_TRAINING_ROWS:
        raise RuntimeError(
            f"{path} has only {len(df):,} rows (need > {MIN_TRAINING_ROWS:,}). "
            "Rebuild the dataset with 'python ml/dataset_builder.py'.")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{path} is missing required columns: {missing}")
    nan_pct = df[required].isna().mean()
    print(f"[VALIDATE] {path} — {len(df):,} rows, NaN % per required column:")
    for c in required:
        print(f"  {c}: {nan_pct[c]:.3%}")
    bad = nan_pct[nan_pct > MAX_NAN_PCT]
    if not bad.empty:
        worst = ", ".join(f"{c}={v:.2%}" for c, v in bad.items())
        raise RuntimeError(
            f"{path} exceeds {MAX_NAN_PCT:.0%} NaN in required columns: {worst}")
    return df


def create_entry_quality_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Entry-quality labels on EVERY bar (labels only meaningful in-session).

    For bar i, with H = ENTRY_HORIZON_BARS:
      future_max_up   = max(high[i+1..i+H]) - close[i]
      future_max_down = close[i] - min(low[i+1..i+H])
      label_ce = 1 if future_max_up   >= QUALITY_THRESHOLD_PTS else 0
      label_pe = 1 if future_max_down >= QUALITY_THRESHOLD_PTS else 0

    BAD_ENTRY guard: if the prior move over the last EXTENDED_LOOKBACK bars
    is already extended (> EXTENDED_MOVE_PCT of price), the entry is late —
    force the corresponding label to 0 and flag bad_entry_ce / bad_entry_pe.

    Bars with fewer than H forward bars (day end) get NaN labels and are
    dropped downstream — never NaN-filled. Forward/backward windows never
    cross a day boundary.
    """
    close = df["close"].to_numpy(dtype=float)
    day   = df["date"].dt.normalize()

    # Future extrema over the next H bars (per day — no cross-day leak)
    fwd_high = df.groupby(day)["high"].transform(
        lambda s: s.shift(-1).rolling(ENTRY_HORIZON_BARS,
                                      min_periods=ENTRY_HORIZON_BARS).max())
    fwd_low = df.groupby(day)["low"].transform(
        lambda s: s.shift(-1).rolling(ENTRY_HORIZON_BARS,
                                      min_periods=ENTRY_HORIZON_BARS).min())
    future_max_up   = fwd_high.to_numpy(dtype=float) - close
    future_max_down = close - fwd_low.to_numpy(dtype=float)
    has_future      = fwd_high.notna().to_numpy()

    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    in_session = mins_from_midnight.apply(_in_active_session).to_numpy()

    # BAD_ENTRY: prior move already extended (window includes bar i)
    win = EXTENDED_LOOKBACK + 1
    back_low = df.groupby(day)["low"].transform(
        lambda s: s.rolling(win, min_periods=1).min()).to_numpy(dtype=float)
    back_high = df.groupby(day)["high"].transform(
        lambda s: s.rolling(win, min_periods=1).max()).to_numpy(dtype=float)
    move_pct_up   = np.where(close > 0, (close - back_low) / close, 0.0)
    move_pct_down = np.where(close > 0, (back_high - close) / close, 0.0)

    bad_entry_ce = in_session & (move_pct_up   > EXTENDED_MOVE_PCT)
    bad_entry_pe = in_session & (move_pct_down > EXTENDED_MOVE_PCT)

    label_ce = np.where(future_max_up   >= QUALITY_THRESHOLD_PTS, 1.0, 0.0)
    label_pe = np.where(future_max_down >= QUALITY_THRESHOLD_PTS, 1.0, 0.0)
    label_ce[bad_entry_ce] = 0.0   # late entry — CE quality forced to 0
    label_pe[bad_entry_pe] = 0.0   # late entry — PE quality forced to 0
    # Bars outside active windows carry no tradable entry (same as v3)
    label_ce[~in_session] = 0.0
    label_pe[~in_session] = 0.0
    # Day-end bars without H forward bars: labels dropped, never NaN-filled
    label_ce[~has_future] = np.nan
    label_pe[~has_future] = np.nan

    df["label_ce"] = label_ce
    df["label_pe"] = label_pe
    df["bad_entry_ce"] = bad_entry_ce.astype(np.int8)
    df["bad_entry_pe"] = bad_entry_pe.astype(np.int8)
    df["ce_eligible"] = in_session
    df["pe_eligible"] = in_session
    return df


def main():
    print("=" * 64)
    print("  ENTRY-QUALITY DATASET BUILDER")
    print("=" * 64)
    print(f"  Horizon={ENTRY_HORIZON_BARS} bars  Quality>={QUALITY_THRESHOLD_PTS}pt  "
          f"ExtendedMove>{EXTENDED_MOVE_PCT:.1%}/{EXTENDED_LOOKBACK}bars  Start={START_DATE}")

    print(f"\n[DATA] Loading {DATA_PATH} ...")
    df = pd.read_csv(DATA_PATH)
    # History file mixes "YYYY-MM-DD HH:MM:SS" and ISO-T rows — format="mixed"
    # parses BOTH instead of silently coercing (and dropping) the ISO-T rows.
    df["date"] = pd.to_datetime(df["date"], format="mixed")
    df = df.sort_values("date").reset_index(drop=True)
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[df["date"] >= pd.Timestamp(START_DATE)].reset_index(drop=True)
    print(f"[DATA] {len(df):,} rows | {df['date'].min().date()} -> {df['date'].max().date()}")

    print("[FEATURES] Computing indicators ...")
    df = compute_all_features(df)

    print("[LABELS] Entry-quality labels ...")
    df = create_entry_quality_labels(df)

    # ── NaN audit (replaces the old silent blanket dropna) ────────────
    total = len(df)
    nan_counts = df.isna().sum()
    nan_cols = nan_counts[nan_counts > 0]
    print(f"\n[NAN AUDIT] {total:,} rows, NaN % per column:")
    if nan_cols.empty:
        print("  (no NaNs)")
    for c in nan_cols.index:
        print(f"  {c}: {nan_counts[c]:,} NaN ({nan_counts[c] / total:.3%})")
    rows_dropped = int(df.isna().any(axis=1).sum())
    if total > 0 and rows_dropped / total > MAX_NAN_PCT:
        raise RuntimeError(
            f"Refusing to build dataset: {rows_dropped:,} rows "
            f"({rows_dropped / total:.2%}) contain NaN — exceeds the "
            f"{MAX_NAN_PCT:.0%} limit. Inspect the NaN audit above.")
    df = df.dropna().reset_index(drop=True)
    print(f"[NAN AUDIT] dropped {rows_dropped:,} rows "
          f"({(rows_dropped / total if total else 0.0):.3%}) -> {len(df):,} rows kept")

    session_mask = df["ce_eligible"].astype(bool)
    ce_rate = df.loc[session_mask, "label_ce"].mean()
    pe_rate = df.loc[session_mask, "label_pe"].mean()
    print(f"\n  Bars: {len(df):,}  (active-session bars: {int(session_mask.sum()):,})")
    print(f"  label_ce=1 : {ce_rate:.1%}   label_pe=1 : {pe_rate:.1%}  "
          f"(among active-session bars)")
    print(f"  bad_entry_ce=1 : {int(df['bad_entry_ce'].sum()):,}   "
          f"bad_entry_pe=1 : {int(df['bad_entry_pe'].sum()):,}")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print(f"\n[SAVED] {OUTPUT}  ({len(df):,} rows)")
    print("  Next: python ml/trainer.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
