# engine/core/decision_engine.py
# FINAL — decision + execution intelligence (non-monolithic)

from datetime import datetime
import numpy as np

from engine.indicators import ema, rsi_calc, atr as atr_calc
from engine.risk.risk_manager import compute_entry_stops
from execution.filters import has_oi_wall
from execution.profit_manager import manage_position as pm_manage


# ================= TIME FILTER ================= #

def is_tradeable_time(now):
    h, m = now.hour, now.minute
    if h == 9 and m < 30:
        return False, "OPEN_WAIT"
    if (h == 15 and m > 10) or h > 15:
        return False, "CLOSE_ZONE"
    return True, "OK"


# ================= REGIME ================= #

def detect_regime(prices):
    if len(prices) < 50:
        return "RANGE"

    e20 = ema(prices, 20)
    e50 = ema(prices, 50)
    if not e20 or not e50:
        return "RANGE"

    trend_strength = abs(e20 - e50) / max(prices[-1], 1)

    recent_vol = float(np.std(np.diff(prices[-20:])))
    base_vol = float(np.std(np.diff(prices[-60:-20]))) if len(prices) >= 60 else recent_vol
    vol_ratio = recent_vol / base_vol if base_vol > 0 else 1.0

    if vol_ratio > 1.8:
        return "EXPANSION"
    if trend_strength > 0.0004:
        return "TREND"
    return "RANGE"


# ================= SIGNAL ================= #

def generate_signal(candles, opens, rsi_1m, regime, ema_trend):

    if regime == "TREND":
        if ema_trend == "UP" and 35 < rsi_1m < 68:
            return "CE"
        if ema_trend == "DOWN" and 32 < rsi_1m < 65:
            return "PE"

    elif regime == "EXPANSION":
        if candles[-1] > candles[-2] and ema_trend == "UP":
            return "CE"
        if candles[-1] < candles[-2] and ema_trend == "DOWN":
            return "PE"

    elif regime == "RANGE":
        if ema_trend == "UP" and rsi_1m < 32:
            return "CE"
        if ema_trend == "DOWN" and rsi_1m > 68:
            return "PE"

    return None


# ================= STEP ================= #

def evaluate_step(ctx, market_data):

    now = datetime.now()

    candles = market_data["candles"]
    highs = market_data["highs"]
    lows = market_data["lows"]
    opens = market_data["opens"]

    # Indicators
    e20 = ema(candles, 20)
    e50 = ema(candles, 50)
    rsi_1m = rsi_calc(candles, 14)
    atr_val = atr_calc(highs, lows, candles, 14)

    regime = detect_regime(candles)
    ema_trend = "UP" if e20 and e50 and e20 > e50 else "DOWN"

    # Time filter
    can_trade, _ = is_tradeable_time(now)
    if not can_trade:
        return None

    signal = generate_signal(candles, opens, rsi_1m, regime, ema_trend)
    if not signal:
        return None

    return {
        "signal": signal,
        "regime": regime,
        "rsi": rsi_1m,
        "atr": atr_val,
        "trend": ema_trend,
        "timestamp": now
    }


# ================= ENTRY PREP ================= #

def prepare_trade(ctx, decision, option_chain=None, ml_prob=0.6, orb_confirmed=False):

    broker = ctx.broker
    signal = decision["signal"]

    # ---- OI FILTER (EDGE) ---- #
    if option_chain:
        spot = decision.get("spot")
        if spot:
            atm = round(spot / 50) * 50
            if has_oi_wall(option_chain, atm, signal):
                return None

    symbol, lot = broker.get_atm_option(signal)
    if not symbol:
        return None

    bid, ask = broker.get_bid_ask(symbol)
    if not bid or not ask:
        return None

    spread_pct = (ask - bid) / ask if ask > 0 else 0
    if spread_pct > 0.05:
        return None

    entry_price = ask * 1.01

    stop, target, stop_pct = compute_entry_stops(
        entry_price,
        decision["atr"],
        decision["regime"]
    )

    return {
        "symbol": symbol,
        "side": signal,
        "entry": entry_price,
        "stop": stop,
        "target": target,
        "qty": lot,
        "ml_prob": ml_prob,
        "meta": decision,
        "max_pnl": 0
    }


# ================= POSITION MANAGEMENT ================= #

def manage_position(ctx, position):

    broker = ctx.broker
    ltp = broker.ltp(position["symbol"])
    if not ltp:
        return None

    stop_loss, max_pnl, reason = pm_manage(
        entry_price=position["entry"],
        ltp=ltp,
        lot_size=position["qty"],
        stop_loss=position["stop"],
        max_pnl=position["max_pnl"],
        ml_prob=position.get("ml_prob", 0.5)
    )

    position["stop"] = stop_loss
    position["max_pnl"] = max_pnl

    if reason:
        return {"exit": True, "reason": reason}

    return None