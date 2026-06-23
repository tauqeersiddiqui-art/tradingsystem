# ml/dataset_builder_v2.py
# Institutional dataset builder with direction-filtered labels.
#
# KEY CHANGES vs original dataset_builder.py + relabel_dual_champion.py:
#   1. Direction filter: CE labels only when Supertrend=UP AND price>VWAP
#      PE labels only when Supertrend=DOWN AND price<VWAP
#   2. ce_eligible / pe_eligible flags — trainer filters training rows
#      so each model only sees market conditions where it should trade
#   3. Better label threshold: 15 spot points in forward 12 candles
#      (was 0.08% / 0.06% pct — equivalent at NIFTY 24000 ≈ 19/14 pts)
#   4. Session filter: only label candles in active windows (9:30-11:00,
#      14:00-15:15) to avoid teaching the model to trade during lunch chop
#   5. ADX gate: labels from strongly trending bars (ADX>20) only —
#      teaches model to identify high-quality entries, not random walks
#
# HOW TO USE:
#   /c/Users/PC/AppData/Local/Programs/Python/Python312/python.exe ml/dataset_builder_v2.py
#   → saves ml/models/training_dataset_v2.csv

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from ml.indicators import supertrend as compute_supertrend, adx as compute_adx, vwap_session

DATA_PATH = "data/historical/banknifty_1m_full.csv"
OUTPUT    = "ml/models/training_dataset_v2.csv"

# ── Label parameters ──────────────────────────────────────────────────
LOOKAHEAD           = 12     # candles to look forward for label
TARGET_SPOT_POINTS  = 15     # spot points needed for label=1
MIN_HOLD            = 2      # first N candles excluded (avoid noise)
ADX_TREND_THRESHOLD = 20     # minimum ADX for labeling (trending market)
SUPERTREND_PERIOD   = 10
SUPERTREND_MULT     = 3.0
ADX_PERIOD          = 14

# ── Session windows for label eligibility ─────────────────────────────
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


def create_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add direction-filtered labels.

    ce_eligible : True when Supertrend=UP AND price>VWAP AND in active session
    pe_eligible : True when Supertrend=DOWN AND price<VWAP AND in active session
    label_ce    : 1 if spot moved up >= TARGET_SPOT_POINTS in LOOKAHEAD candles
    label_pe    : 1 if spot moved down >= TARGET_SPOT_POINTS in LOOKAHEAD candles
    """
    n       = len(df)
    close   = df["close"].values.astype(float)
    st_dir  = df["supertrend_dir"].values
    pvwap   = df["price_vs_vwap"].values
    adx_arr = df["adx"].values

    # Session membership
    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    in_session = mins_from_midnight.apply(_in_active_session).values

    ce_eligible = np.zeros(n, dtype=bool)
    pe_eligible = np.zeros(n, dtype=bool)
    label_ce    = np.zeros(n, dtype=np.int8)
    label_pe    = np.zeros(n, dtype=np.int8)

    for i in range(n - LOOKAHEAD):
        if not in_session[i]:
            continue

        # Direction eligibility
        is_bullish = (st_dir[i] == 1) and (pvwap[i] > 0)
        is_bearish = (st_dir[i] == -1) and (pvwap[i] < 0)

        # ADX gate: only label from trending bars
        adx_ok = adx_arr[i] >= ADX_TREND_THRESHOLD

        if not adx_ok:
            continue

        future = close[i + 1: i + LOOKAHEAD + 1]
        if len(future) == 0:
            continue

        if is_bullish:
            ce_eligible[i] = True
            # Label CE=1 if price moved up enough in future without first dropping hard
            max_up    = float(future.max() - close[i])
            max_down  = float(close[i] - future.min())
            # Must move up by target AND not first hit -2×target (would have SL hit)
            if max_up >= TARGET_SPOT_POINTS and max_down < TARGET_SPOT_POINTS * 2:
                label_ce[i] = 1

        if is_bearish:
            pe_eligible[i] = True
            max_down = float(close[i] - future.min())
            max_up   = float(future.max() - close[i])
            if max_down >= TARGET_SPOT_POINTS and max_up < TARGET_SPOT_POINTS * 2:
                label_pe[i] = 1

    df["ce_eligible"] = ce_eligible
    df["pe_eligible"] = pe_eligible
    df["label_ce"]    = label_ce
    df["label_pe"]    = label_pe

    return df


def main():
    print("=" * 60)
    print("  INSTITUTIONAL DATASET BUILDER v2")
    print("=" * 60)
    print(f"  Direction-filtered labels | Target={TARGET_SPOT_POINTS} spot pts | "
          f"Lookahead={LOOKAHEAD} bars")

    print(f"\n[DATA] Loading {DATA_PATH} ...")
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    if "volume" not in df.columns:
        df["volume"] = 0.0

    print(f"[DATA] {len(df):,} rows | "
          f"{df['date'].min().date()} to {df['date'].max().date()}")

    print("[FEATURES] Computing all indicators ...")
    df = compute_all_features(df)

    print("[LABELS] Creating direction-filtered labels ...")
    df = create_labels(df)

    df = df.dropna()

    # ── Summary ────────────────────────────────────────────────────────
    ce_rows = df[df["ce_eligible"]]
    pe_rows = df[df["pe_eligible"]]

    ce_rate = ce_rows["label_ce"].mean() if len(ce_rows) > 0 else 0
    pe_rate = pe_rows["label_pe"].mean() if len(pe_rows) > 0 else 0

    print(f"\n  CE eligible rows : {len(ce_rows):,}  |  Label=1 rate: {ce_rate:.1%}")
    print(f"  PE eligible rows : {len(pe_rows):,}  |  Label=1 rate: {pe_rate:.1%}")

    if ce_rate < 0.15:
        print("  [WARN] CE label rate < 15% — consider lowering TARGET_SPOT_POINTS")
    elif ce_rate > 0.65:
        print("  [WARN] CE label rate > 65% — consider raising TARGET_SPOT_POINTS")
    else:
        print("  [OK] CE label balance is healthy")

    if pe_rate < 0.15:
        print("  [WARN] PE label rate < 15%")
    elif pe_rate > 0.65:
        print("  [WARN] PE label rate > 65%")
    else:
        print("  [OK] PE label balance is healthy")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print("\n[SAVED] " + OUTPUT + "  (" + str(len(df)) + " rows)")
    print("=" * 60)
    print("  Next: run ml/trainer_v2_champion.py to train new models")
    print("=" * 60)


if __name__ == "__main__":
    main()
