# ml/feature_config.py
# Institutional-grade feature set — 36 features
#
# KEY ADDITIONS vs original 28-feature set:
#   NEW-1 : supertrend_dir / supertrend_dist — primary trend gate (no counter-trend trades)
#   NEW-2 : price_vs_vwap — institutional anchor; CE above VWAP only, PE below only
#   NEW-3 : adx — regime filter; model can learn to ignore low-ADX ranging sessions
#   NEW-4 : di_spread — directional momentum confirmation (DI+ minus DI-)
#   NEW-5 : ema_alignment — EMA20 vs EMA50 direction confirmation
#   NEW-6 : volume_ratio — conviction filter; avoids low-liquidity fake breakouts
#   NEW-7 : supertrend_dist — distance from ST line, measures trend strength
#
# These 7 features are the "direction stack" — they must all agree for a strong signal.
# When the model is trained on direction-filtered data (ce_eligible / pe_eligible rows),
# it learns: "given direction is confirmed, is THIS candle a high-quality entry?"

from datetime import datetime, time as dtime

_MARKET_OPEN_T  = dtime(9, 15)
_MARKET_CLOSE_T = dtime(15, 30)

# ─────────────────────────────────────────────────────────────────────
# CANONICAL FEATURE ORDER  (must be identical in train, backtest, live)
# ─────────────────────────────────────────────────────────────────────
FEATURE_COLUMNS = [
    # Direction stack (NEW)
    "supertrend_dir",      # +1=UP, -1=DOWN (Supertrend 10/3)
    "supertrend_dist",     # (close - st_line) / close — strength of trend
    "price_vs_vwap",       # (close - vwap) / close — VWAP bias
    "adx",                 # ADX(14); >25=trending, <20=ranging
    "di_spread",           # DI+ minus DI-; positive=bullish pressure
    "ema_alignment",       # +1 if EMA20>EMA50, -1 otherwise
    "volume_ratio",        # current vol / 20-bar avg vol (0 if no volume data)

    # Core price / indicators
    "ema20", "ema50", "macd", "returns", "volatility",
    "rsi", "atr", "trend_strength",

    # Time
    "hour", "weekday",

    # Short-term momentum
    "return_1", "return_3",

    # Candle structure
    "candle_body_pct", "range_break_strength",

    # Session context
    "mins_since_open", "mins_to_close", "session_open", "session_close",

    # Options-specific
    "time_to_expiry_min",
    "moneyness",

    # Early-reversal / momentum features
    "momentum_velocity",
    "range_compression",
    "wick_ratio",
    "body_efficiency",
    "mom3_strength",
    "upper_wick",
    "lower_wick",
    "close_position",
]  # 36 features total


# ─────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────

def sget(signal: dict, key: str, default):
    v = signal.get(key, default)
    return default if v is None else v


# ─────────────────────────────────────────────────────────────────────
# MAIN FEATURE BUILDER
# Called every candle in live engine and backtest signal engine.
# `signal` is a dict pre-computed by _compute_signal / _compute_signal_dict.
# ─────────────────────────────────────────────────────────────────────

def build_live_features(closes, opens, highs, lows, volumes, signal, ts=None):
    """
    Build 36 features from rolling OHLCV window + pre-computed signal dict.

    `closes/opens/highs/lows/volumes` — lists of floats, latest last.
    `signal` — dict with at minimum:
        ema20, ema50, rsi_1m, atr, trend_strength
    New keys (populated by updated _compute_signal in both engines):
        supertrend_dir, supertrend_dist, price_vs_vwap,
        adx, di_spread, ema_alignment, volume_ratio, vwap
    `ts` — datetime of current candle (MUST be passed to get correct time features)
    """
    if len(closes) < 25:
        return {f: 0.0 for f in FEATURE_COLUMNS}

    closes  = list(closes)
    opens   = list(opens)
    highs   = list(highs)
    lows    = list(lows)
    volumes = list(volumes)

    # ── Pull pre-computed values from signal dict ──────────────────────
    ema20          = sget(signal, "ema20",          0.0)
    ema50          = sget(signal, "ema50",          0.0)
    rsi_1m         = sget(signal, "rsi_1m",         50.0)
    trend_strength = sget(signal, "trend_strength", 0.0)

    # Direction stack (NEW)
    supertrend_dir  = float(sget(signal, "supertrend_dir",  0))
    supertrend_dist = float(sget(signal, "supertrend_dist", 0.0))
    price_vs_vwap   = float(sget(signal, "price_vs_vwap",   0.0))
    adx_val         = float(sget(signal, "adx",             20.0))
    di_spread_val   = float(sget(signal, "di_spread",       0.0))
    ema_alignment   = float(sget(signal, "ema_alignment",   0.0))
    volume_ratio    = float(sget(signal, "volume_ratio",    1.0))

    # ── Returns ────────────────────────────────────────────────────────
    returns  = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] != 0 else 0.0
    return_1 = returns
    return_3 = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 and closes[-4] != 0 else 0.0

    # ── Volatility ─────────────────────────────────────────────────────
    if len(closes) >= 21:
        import numpy as np
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, 21)]
        volatility = float(np.std(rets))
    else:
        volatility = 0.001
    volatility = max(volatility, 1e-6)

    # ── ATR ────────────────────────────────────────────────────────────
    signal_atr = signal.get("atr", None)
    if signal_atr and signal_atr > 1.0:
        atr_val = float(signal_atr)
    elif len(highs) >= 14:
        import numpy as np
        h = np.array(highs[-14:], dtype=float)
        lw = np.array(lows[-14:], dtype=float)
        c  = np.array(closes[-14:], dtype=float)
        tr = [max(h[i] - lw[i], abs(h[i] - c[i - 1]), abs(lw[i] - c[i - 1])) for i in range(1, 14)]
        atr_val = float(np.mean(tr))
    else:
        atr_val = volatility * closes[-1] * 14 ** 0.5
    atr_val = max(atr_val, 0.5)

    # ── Candle structure ───────────────────────────────────────────────
    hl              = highs[-1] - lows[-1]
    candle_body_pct = abs(closes[-1] - opens[-1]) / (hl if hl > 0 else 1e-6)

    rolling_high_10 = max(highs[-10:]) if len(highs) >= 10 else highs[-1]
    range_break_str = (closes[-1] - rolling_high_10) / (atr_val + 1e-6)

    # ── Time features ──────────────────────────────────────────────────
    # MUST use candle timestamp — using datetime.now() in backtest gives
    # wrong times (2024 candles get 2026 wall-clock → model sees wrong session).
    now       = ts if ts is not None else datetime.now()
    mkt_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    mins_open  = max(0.0, (now - mkt_open).total_seconds() / 60)
    mins_close = max(0.0, (mkt_close - now).total_seconds() / 60)

    # ── Options-specific ───────────────────────────────────────────────
    moneyness          = (closes[-1] - ema20) / closes[-1] if closes[-1] != 0 else 0.0
    time_to_expiry_min = min(mins_close, 375.0)

    # ── Momentum / wick features ───────────────────────────────────────
    import numpy as np

    mom_vel = 0.0
    if len(closes) >= 4 and closes[-2] != 0 and closes[-3] != 0:
        r1 = (closes[-1] - closes[-2]) / closes[-2]
        r2 = (closes[-2] - closes[-3]) / closes[-3]
        mom_vel = r1 - r2

    range_comp = 1.0
    if len(highs) >= 15 and len(lows) >= 15:
        r5  = max(highs[-5:])  - min(lows[-5:])
        r15 = max(highs[-15:]) - min(lows[-15:])
        range_comp = r5 / (r15 + 1e-6)

    body       = abs(closes[-1] - opens[-1])
    wick       = hl - body
    wick_ratio = min(wick / (body + 1e-6), 10.0)
    body_eff   = body / (hl + 1e-6) if hl > 0 else 0.5

    mom3_str   = abs(closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else 0.0

    upper_w  = (highs[-1] - max(closes[-1], opens[-1])) / (atr_val + 1e-6)
    lower_w  = (min(closes[-1], opens[-1]) - lows[-1])  / (atr_val + 1e-6)
    close_pos = (closes[-1] - lows[-1]) / (hl + 1e-6) if hl > 0 else 0.5

    return {
        # Direction stack (NEW)
        "supertrend_dir":     float(np.clip(supertrend_dir, -1, 1)),
        "supertrend_dist":    float(np.clip(supertrend_dist, -0.05, 0.05)),
        "price_vs_vwap":      float(np.clip(price_vs_vwap,  -0.05, 0.05)),
        "adx":                float(np.clip(adx_val,         0, 100)),
        "di_spread":          float(np.clip(di_spread_val,  -60, 60)),
        "ema_alignment":      float(np.clip(ema_alignment,  -1, 1)),
        "volume_ratio":       float(np.clip(volume_ratio,    0, 10)),

        # Core indicators
        "ema20":              float(ema20),
        "ema50":              float(ema50),
        "macd":               float(ema20 - ema50),
        "returns":            float(returns),
        "volatility":         float(min(max(volatility, 0.0), 0.02)),
        "rsi":                float(rsi_1m),
        "atr":                float(atr_val),
        "trend_strength":     float(trend_strength),

        # Time
        "hour":               int(now.hour),
        "weekday":            int(now.weekday()),

        # Short-term momentum
        "return_1":           float(return_1),
        "return_3":           float(return_3),

        # Candle structure
        "candle_body_pct":        float(candle_body_pct),
        "range_break_strength":   float(np.clip(range_break_str, -5.0, 5.0)),

        # Session
        "mins_since_open":    float(min(mins_open, 375.0)),
        "mins_to_close":      float(min(mins_close, 375.0)),
        "session_open":       int(mins_open < 30),
        "session_close":      int(mins_close < 60),

        # Options
        "time_to_expiry_min": float(time_to_expiry_min),
        "moneyness":          float(np.clip(moneyness, -0.02, 0.02)),

        # Reversal / momentum
        "momentum_velocity":  float(mom_vel),
        "range_compression":  float(range_comp),
        "wick_ratio":         float(wick_ratio),
        "body_efficiency":    float(body_eff),
        "mom3_strength":      float(mom3_str),
        "upper_wick":         float(upper_w),
        "lower_wick":         float(lower_w),
        "close_position":     float(close_pos),
    }


def _safe_build_live_features(closes, opens, highs, lows, volumes, signal, ts=None):
    try:
        feats = build_live_features(closes, opens, highs, lows, volumes, signal, ts=ts)
        if not feats:
            return {f: 0.0 for f in FEATURE_COLUMNS}
        for f in FEATURE_COLUMNS:
            if f not in feats:
                feats[f] = 0.0
        return feats
    except Exception as e:
        print("[FEATURE ERROR]", e)
        return {f: 0.0 for f in FEATURE_COLUMNS}
