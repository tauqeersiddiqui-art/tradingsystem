# ml/dataset_builder_v2.py
# Feature computation for dataset building.
# Provides compute_all_features() and _in_active_session() used by v3 builder.

import numpy as np
import pandas as pd
from ml.indicators import supertrend as _compute_supertrend, adx as _compute_adx

# Market hours (in minutes from midnight)
_MKT_OPEN_MIN  = 9 * 60 + 15   # 555
_MKT_CLOSE_MIN = 15 * 60 + 30  # 930


def _in_active_session(mins_from_midnight: int) -> bool:
    """Return True if the time is within active market hours (9:15–15:30)."""
    return _MKT_OPEN_MIN <= mins_from_midnight <= _MKT_CLOSE_MIN


def compute_all_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all 36 features for every row in the DataFrame.
    Expects columns: date, open, high, low, close, volume.
    Returns the DataFrame with feature columns added.
    """
    n = len(df)
    closes  = df["close"].values.astype(float)
    opens   = df["open"].values.astype(float)
    highs   = df["high"].values.astype(float)
    lows    = df["low"].values.astype(float)
    volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.zeros(n)

    # Rolling EMA
    def rolling_ema(arr, span):
        alpha = 2.0 / (span + 1)
        out = np.empty_like(arr)
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = arr[i] * alpha + out[i - 1] * (1 - alpha)
        return out

    ema20 = rolling_ema(closes, 20)
    ema50 = rolling_ema(closes, 50)

    # Returns
    returns = np.zeros(n)
    returns[1:] = (closes[1:] - closes[:-1]) / np.where(closes[:-1] != 0, closes[:-1], 1)

    # Volatility (rolling 21-bar std of returns)
    volatility = np.full(n, 0.001)
    for i in range(21, n):
        volatility[i] = np.std(returns[i - 20:i + 1])
    volatility = np.maximum(volatility, 1e-6)

    # RSI-14
    rsi = np.full(n, 50.0)
    for i in range(15, n):
        gains, losses = [], []
        for j in range(i - 13, i + 1):
            d = closes[j] - closes[j - 1]
            (gains if d > 0 else losses).append(abs(d))
        avg_g = np.mean(gains) if gains else 1e-6
        avg_l = np.mean(losses) if losses else 1e-6
        rsi[i] = 100 - (100 / (1 + avg_g / avg_l))

    # ATR-14 (Wilder)
    atr = np.full(n, 1.0)
    for i in range(14, n):
        tr = [max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
              for j in range(i - 13, i + 1)]
        atr[i] = max(float(np.mean(tr)), 0.5)

    # Trend strength
    trend_strength = np.where(closes != 0, (ema20 - ema50) / closes, 0.0)

    # Supertrend (rolling window — use last 200 bars for each position)
    supertrend_dir  = np.zeros(n)
    supertrend_dist = np.zeros(n)
    window = 200
    for i in range(window, n):
        h = highs[i - window + 1:i + 1]
        l = lows[i - window + 1:i + 1]
        c = closes[i - window + 1:i + 1]
        st_dir, st_line = _compute_supertrend(
            np.array(h, dtype=float),
            np.array(l, dtype=float),
            np.array(c, dtype=float),
            period=10, multiplier=3.0,
        )
        supertrend_dir[i] = float(int(st_dir[-1]))
        if closes[i] != 0:
            supertrend_dist[i] = float(np.clip((closes[i] - st_line[-1]) / closes[i], -0.05, 0.05))

    # ADX (rolling window)
    adx_val    = np.full(n, 20.0)
    di_spread  = np.zeros(n)
    for i in range(window, n):
        h = highs[i - window + 1:i + 1]
        l = lows[i - window + 1:i + 1]
        c = closes[i - window + 1:i + 1]
        adx_arr, di_plus, di_minus = _compute_adx(
            np.array(h, dtype=float),
            np.array(l, dtype=float),
            np.array(c, dtype=float),
            period=14,
        )
        adx_val[i]   = float(np.clip(adx_arr[-1], 0, 100))
        di_spread[i] = float(np.clip(di_plus[-1] - di_minus[-1], -60, 60))

    # VWAP (cumulative from session open — reset each day)
    vwap_val = np.zeros(n)
    vwap_pv  = np.zeros(n)
    days = df["date"].dt.date.values if hasattr(df["date"].dt, "date") else np.zeros(n, dtype="datetime64[D]")

    unique_days = np.unique(days)
    for d in unique_days:
        mask = days == d
        idx = np.where(mask)[0]
        if len(idx) == 0:
            continue
        cum_pv = 0.0
        cum_v  = 0.0
        for i in idx:
            hl2 = (highs[i] + lows[i] + closes[i]) / 3.0
            v   = max(volumes[i], 1.0)
            cum_pv += hl2 * v
            cum_v  += v
            vwap_val[i] = cum_pv / cum_v if cum_v > 0 else closes[i]
        # price_vs_vwap for this day
        for i in idx:
            if closes[i] != 0 and vwap_val[i] > 0:
                vwap_pv[i] = float(np.clip((closes[i] - vwap_val[i]) / closes[i], -0.05, 0.05))

    # EMA alignment
    ema_alignment = np.where(ema20 > ema50, 1.0, -1.0)

    # Volume ratio (current / 20-bar avg)
    volume_ratio = np.zeros(n)
    for i in range(20, n):
        avg_vol = np.mean(volumes[i - 19:i + 1])
        volume_ratio[i] = volumes[i] / avg_vol if avg_vol > 0 else 1.0

    # Returns_1 and returns_3
    return_1 = returns.copy()
    return_3 = np.zeros(n)
    return_3[3:] = (closes[3:] - closes[:-3]) / np.where(closes[:-3] != 0, closes[:-3], 1)

    # Candle structure
    hl = highs - lows
    candle_body_pct = np.where(hl > 0, np.abs(closes - opens) / hl, 0.0)

    rolling_high_10 = np.zeros(n)
    for i in range(10, n):
        rolling_high_10[i] = max(highs[i - 9:i + 1])
    range_break_strength = np.where(atr > 0, (closes - rolling_high_10) / atr, 0.0)

    # Time features
    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    mins_since_open  = np.maximum(0.0, (mins_from_midnight - _MKT_OPEN_MIN).astype(float))
    mins_to_close    = np.maximum(0.0, (_MKT_CLOSE_MIN - mins_from_midnight).astype(float))
    session_open     = (mins_since_open < 30).astype(float)
    session_close    = (mins_to_close < 60).astype(float)
    hour             = df["date"].dt.hour.values.astype(float)
    weekday          = df["date"].dt.weekday.values.astype(float)

    # Options-specific
    moneyness          = np.where(closes != 0, (closes - ema20) / closes, 0.0)
    time_to_expiry_min = np.minimum(mins_to_close, 375.0)

    # Momentum / wick features
    mom_vel = np.zeros(n)
    mom_vel[2:] = (closes[2:] - closes[1:-1]) - (closes[1:-1] - closes[:-2])

    range_compression = np.ones(n)
    for i in range(15, n):
        r5  = max(highs[i - 4:i + 1]) - min(lows[i - 4:i + 1])
        r15 = max(highs[i - 14:i + 1]) - min(lows[i - 14:i + 1])
        range_compression[i] = r5 / (r15 + 1e-6)

    body       = np.abs(closes - opens)
    wick       = hl - body
    wick_ratio = np.minimum(wick / (body + 1e-6), 10.0)
    body_eff   = np.where(hl > 0, body / hl, 0.5)

    mom3_str = np.zeros(n)
    mom3_str[3:] = np.abs(closes[3:] - closes[:-3]) / np.where(closes[:-3] != 0, closes[:-3], 1)

    upper_wick = np.where(atr > 0, (highs - np.maximum(closes, opens)) / atr, 0.0)
    lower_wick = np.where(atr > 0, (np.minimum(closes, opens) - lows) / atr, 0.0)
    close_pos  = np.where(hl > 0, (closes - lows) / hl, 0.5)

    # MACD
    macd = ema20 - ema50

    # Assign all feature columns
    df["supertrend_dir"]     = np.clip(supertrend_dir, -1, 1)
    df["supertrend_dist"]    = np.clip(supertrend_dist, -0.05, 0.05)
    df["price_vs_vwap"]      = np.clip(vwap_pv, -0.05, 0.05)
    df["adx"]                = np.clip(adx_val, 0, 100)
    df["di_spread"]          = np.clip(di_spread, -60, 60)
    df["ema_alignment"]      = np.clip(ema_alignment, -1, 1)
    df["volume_ratio"]       = np.clip(volume_ratio, 0, 10)
    df["ema20"]              = ema20
    df["ema50"]              = ema50
    df["macd"]               = macd
    df["returns"]            = returns
    df["volatility"]         = np.clip(volatility, 0, 0.02)
    df["rsi"]                = rsi
    df["atr"]                = atr
    df["trend_strength"]     = trend_strength
    df["hour"]               = hour
    df["weekday"]            = weekday
    df["return_1"]           = return_1
    df["return_3"]           = return_3
    df["candle_body_pct"]    = candle_body_pct
    df["range_break_strength"] = np.clip(range_break_strength, -5.0, 5.0)
    df["mins_since_open"]    = np.minimum(mins_since_open, 375.0)
    df["mins_to_close"]      = np.minimum(mins_to_close, 375.0)
    df["session_open"]       = session_open
    df["session_close"]      = session_close
    df["time_to_expiry_min"] = time_to_expiry_min
    df["moneyness"]          = np.clip(moneyness, -0.02, 0.02)
    df["momentum_velocity"]  = mom_vel
    df["range_compression"]  = range_compression
    df["wick_ratio"]         = wick_ratio
    df["body_efficiency"]    = body_eff
    df["mom3_strength"]      = mom3_str
    df["upper_wick"]         = np.clip(upper_wick, -5, 5)
    df["lower_wick"]         = np.clip(lower_wick, -5, 5)
    df["close_position"]     = close_pos

    return df
