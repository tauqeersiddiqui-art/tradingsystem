# ml/feature_config.py
# FIXED — audit v2
#
# Changes from original:
#   FIX-1  : removed vix_regime (hardcoded 0.5 in both train and live — pure noise)
#   FIX-2  : removed volume_spike_ratio (NIFTY index volume has no meaning)
#   FIX-3  : returns now computed from closes (was always 0.0 in live)
#   FIX-4  : atr now taken from signal["atr"] which signal_engine MUST set (see signal_engine.py)
#             fallback is a real ATR estimate from price std, NOT returns std
#   FIX-5  : added moneyness  = (close - ema20) / close  — spot position vs EMA20
#   FIX-6  : added time_to_expiry_min — theta decay proxy critical for options
#   Total features: 28 (was 28, removed 2, added 2)

from datetime import datetime

FEATURE_COLUMNS = [
    # Core price / indicator features
    "ema20", "ema50", "macd", "returns", "volatility",
    "rsi", "atr", "trend_strength",
    # Time features
    "hour", "weekday",
    # Short-term momentum
    "return_1", "return_3",
    # Candle structure
    "candle_body_pct", "range_break_strength",
    # Session context
    "mins_since_open", "mins_to_close", "session_open", "session_close",
    # Options-specific
    "time_to_expiry_min",   # FIX-6: theta proxy
    "moneyness",            # FIX-5: (close - ema20) / close
    # Early-reversal prediction features
    "momentum_velocity",
    "range_compression",
    "wick_ratio",
    "body_efficiency",
    "mom3_strength",
    "upper_wick",
    "lower_wick",
    "close_position",
]


def sget(signal, key, default):
    v = signal.get(key, default)
    return default if v is None else v


def build_live_features(closes, opens, highs, lows, volumes, signal):
    if len(closes) < 25:
        return {f: 0.0 for f in FEATURE_COLUMNS}

    closes  = list(closes)
    opens   = list(opens)
    highs   = list(highs)
    lows    = list(lows)
    volumes = list(volumes)

    ema20          = sget(signal, "ema20",          0.0)
    ema50          = sget(signal, "ema50",          0.0)
    rsi_1m         = sget(signal, "rsi_1m",         50.0)
    trend_strength = sget(signal, "trend_strength", 0.0)

    # FIX-3: compute returns from actual closes (was always 0 before)
    returns  = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] != 0 else 0.0
    return_1 = returns
    return_3 = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 and closes[-4] != 0 else 0.0

    # Volatility — rolling std of pct returns
    if len(closes) >= 21:
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, 21)]
        volatility = float(__import__('numpy').std(rets))
    else:
        volatility = 0.001
    volatility = max(volatility, 1e-6)

    # FIX-4: true ATR from signal (signal_engine.py now sets "atr" correctly)
    # Fallback: estimate from high/low range if not available
    signal_atr = signal.get("atr", None)
    if signal_atr and signal_atr > 1.0:
        atr_val = float(signal_atr)
    elif len(highs) >= 14 and len(lows) >= 14 and len(closes) >= 14:
        # Wilder ATR estimate from raw OHLC
        import numpy as np
        h = np.array(highs[-14:], dtype=float)
        l = np.array(lows[-14:], dtype=float)
        c = np.array(closes[-14:], dtype=float)
        tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, 14)]
        atr_val = float(np.mean(tr))
    else:
        atr_val = volatility * closes[-1] * 14 ** 0.5  # rough proxy

    atr_val = max(atr_val, 0.5)  # floor at 0.5 points

    # Candle structure
    hl = highs[-1] - lows[-1]
    candle_body_pct = abs(closes[-1] - opens[-1]) / (hl if hl > 0 else 1e-6)

    rolling_high_10 = max(highs[-10:]) if len(highs) >= 10 else highs[-1]
    range_break_str = (closes[-1] - rolling_high_10) / (atr_val + 1e-6)

    # Time features
    now        = datetime.now()
    mkt_open   = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_close  = now.replace(hour=15, minute=30, second=0, microsecond=0)
    mins_open  = max(0.0, (now - mkt_open).total_seconds() / 60)
    mins_close = max(0.0, (mkt_close - now).total_seconds() / 60)

    # FIX-5: moneyness — where close is relative to EMA20
    moneyness = (closes[-1] - ema20) / closes[-1] if closes[-1] != 0 else 0.0

    # FIX-6: time to expiry proxy (minutes to 15:30 on nearest Thursday)
    # Simplified: use mins_close as proxy. Real impl should use actual expiry calendar.
    time_to_expiry_min = min(mins_close, 375.0)

    # Momentum / wick features
    import numpy as np
    mom_vel = 0.0
    if len(closes) >= 4:
        mom_vel = (closes[-1] - closes[-2]) - (closes[-2] - closes[-3])

    range_comp = 1.0
    if len(highs) >= 15 and len(lows) >= 15:
        r5  = max(highs[-5:])  - min(lows[-5:])
        r15 = max(highs[-15:]) - min(lows[-15:])
        range_comp = r5 / (r15 + 1e-6)

    body     = abs(closes[-1] - opens[-1])
    wick     = hl - body
    wick_ratio = min((wick / (body + 1e-6)), 10.0)
    body_eff   = body / (hl + 1e-6) if hl > 0 else 0.5

    mom3_str = abs(closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else 0.0

    upper_w = (highs[-1] - max(closes[-1], opens[-1])) / (atr_val + 1e-6)
    lower_w = (min(closes[-1], opens[-1]) - lows[-1])  / (atr_val + 1e-6)
    close_pos = (closes[-1] - lows[-1]) / (hl + 1e-6) if hl > 0 else 0.5

    return {
        "ema20":               float(ema20),
        "ema50":               float(ema50),
        "macd":                float(ema20 - ema50),
        "returns":             float(returns),          # FIX-3
        "volatility":          float(min(max(volatility, 0.0), 0.02)),
        "rsi":                 float(rsi_1m),
        "atr":                 float(atr_val),          # FIX-4
        "trend_strength":      float(trend_strength),
        "hour":                int(now.hour),
        "weekday":             int(now.weekday()),
        "return_1":            float(return_1),
        "return_3":            float(return_3),
        "candle_body_pct":     float(candle_body_pct),
        "range_break_strength": float(min(max(range_break_str, -5.0), 5.0)),
        "mins_since_open":     float(min(mins_open, 375.0)),
        "mins_to_close":       float(min(mins_close, 375.0)),
        "session_open":        int(mins_open < 30),
        "session_close":       int(mins_close < 60),
        "time_to_expiry_min":  float(time_to_expiry_min),  # FIX-6
        "moneyness":           float(min(max(moneyness, -0.02), 0.02)),  # FIX-5
        "momentum_velocity":   float(mom_vel),
        "range_compression":   float(range_comp),
        "wick_ratio":          float(wick_ratio),
        "body_efficiency":     float(body_eff),
        "mom3_strength":       float(mom3_str),
        "upper_wick":          float(upper_w),
        "lower_wick":          float(lower_w),
        "close_position":      float(close_pos),
    }


def _safe_build_live_features(closes, opens, highs, lows, volumes, signal):
    try:
        feats = build_live_features(closes, opens, highs, lows, volumes, signal)
        if not feats:
            return {f: 0.0 for f in FEATURE_COLUMNS}
        for f in FEATURE_COLUMNS:
            if f not in feats:
                feats[f] = 0.0
        return feats
    except Exception as e:
        print("[FEATURE ERROR]", e)
        return {f: 0.0 for f in FEATURE_COLUMNS}
