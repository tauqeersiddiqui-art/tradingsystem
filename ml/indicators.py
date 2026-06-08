# ml/indicators.py
# Vectorized technical indicators for institutional-grade analysis.
# Pure numpy functions — no side effects, no globals, no state.

import numpy as np


# ════════════════════════════════════════════════════════════════════════
# ATR / SMOOTHING HELPERS
# ════════════════════════════════════════════════════════════════════════

def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (RMA). Equivalent to EMA with alpha=1/period."""
    n = len(values)
    result = np.zeros(n)
    if n < period:
        return result
    result[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 14) -> np.ndarray:
    """Wilder ATR — canonical True Range smoothed with Wilder's RMA."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    return _wilder_smooth(tr, period)


# ════════════════════════════════════════════════════════════════════════
# SUPERTREND  (period=10, multiplier=3)
# ════════════════════════════════════════════════════════════════════════

def supertrend(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 10, multiplier: float = 3.0):
    """
    Supertrend indicator.

    Returns
    -------
    direction : np.ndarray  (+1 = Bullish/UP, -1 = Bearish/DOWN)
    st_line   : np.ndarray  (supertrend line value — use for distance calc)
    """
    n = len(close)
    atr_vals = atr_wilder(high, low, close, period)

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr_vals
    basic_lower = hl2 - multiplier * atr_vals

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    direction   = np.ones(n, dtype=np.int8)
    st_line     = np.zeros(n)

    # Initial ST line
    st_line[0] = final_lower[0]

    for i in range(1, n):
        # Final upper band: only narrows, OR resets when broken
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Final lower band: only widens, OR resets when broken
        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Direction flip logic
        if direction[i - 1] == -1 and close[i] > final_upper[i]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < final_lower[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        # ST line is the support (lower) when bullish, resistance (upper) when bearish
        st_line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return direction, st_line


# ════════════════════════════════════════════════════════════════════════
# ADX  (period=14)
# ════════════════════════════════════════════════════════════════════════

def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int = 14):
    """
    Average Directional Index.

    Returns
    -------
    adx_arr  : np.ndarray  (0–100; >25 = trending, <20 = ranging)
    di_plus  : np.ndarray  (+DI, bullish directional movement)
    di_minus : np.ndarray  (-DI, bearish directional movement)
    """
    n = len(close)
    tr      = np.zeros(n)
    dm_plus = np.zeros(n)
    dm_min  = np.zeros(n)

    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        tr[i]      = max(high[i] - low[i],
                         abs(high[i] - close[i - 1]),
                         abs(low[i]  - close[i - 1]))
        dm_plus[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        dm_min[i]  = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    atr_s  = _wilder_smooth(tr, period)
    di_p   = 100.0 * _wilder_smooth(dm_plus, period) / (atr_s + 1e-10)
    di_m   = 100.0 * _wilder_smooth(dm_min, period)  / (atr_s + 1e-10)

    dx      = 100.0 * np.abs(di_p - di_m) / (di_p + di_m + 1e-10)
    adx_arr = _wilder_smooth(dx, period)

    return adx_arr, di_p, di_m


# ════════════════════════════════════════════════════════════════════════
# VWAP  (session-reset using date array)
# ════════════════════════════════════════════════════════════════════════

def vwap_session(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 volume: np.ndarray, dates) -> np.ndarray:
    """
    Volume Weighted Average Price, reset at each calendar day.

    `dates` must be an array/list of date-like objects (datetime.date,
    pd.Timestamp, or date strings) with one entry per bar.

    When volume is all-zero (NIFTY spot index), uniform weight is used
    so VWAP degrades gracefully to a simple rolling mean of typical price.
    """
    import pandas as pd

    n         = len(close)
    typical   = (high + low + close) / 3.0
    vol       = np.where(volume == 0, 1.0, volume.astype(float))

    dates_dt  = pd.to_datetime(dates).date
    result    = np.zeros(n)
    cum_tpv   = 0.0
    cum_vol   = 0.0
    prev_date = None

    for i in range(n):
        d = dates_dt[i]
        if d != prev_date:
            cum_tpv   = 0.0
            cum_vol   = 0.0
            prev_date = d
        cum_tpv  += typical[i] * vol[i]
        cum_vol  += vol[i]
        result[i] = cum_tpv / cum_vol

    return result


# ════════════════════════════════════════════════════════════════════════
# LIVE VWAP ACCUMULATOR  (for live and backtest rolling-state use)
# ════════════════════════════════════════════════════════════════════════

class VWAPAccumulator:
    """
    Incrementally accumulates VWAP from session open.
    Call reset() at market open each day.
    Call update(high, low, close, volume) for each new candle.
    Read .value for current VWAP.
    """

    def __init__(self):
        self._cum_tpv = 0.0
        self._cum_vol = 0.0

    def reset(self):
        self._cum_tpv = 0.0
        self._cum_vol = 0.0

    def update(self, high: float, low: float, close: float, volume: float = 0.0):
        vol = max(volume, 1.0)       # degenerate guard for zero-vol index
        self._cum_tpv += (high + low + close) / 3.0 * vol
        self._cum_vol += vol

    @property
    def value(self) -> float:
        if self._cum_vol == 0:
            return 0.0
        return self._cum_tpv / self._cum_vol
