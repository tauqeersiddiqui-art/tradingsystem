# telegram/messages.py


def format_trade_entry(data):

    return f"""
🟢 TRADE ENTRY CONFIRMED

📌 Instrument : {data['symbol']}
📍 Direction  : {data['side']}
📈 Regime     : {data.get('regime','TREND')}

💰 Entry Price   : {round(data['price'],2)}
📦 Lot Size      : {data['qty']}
🛑 Stop Loss     : {round(data['stop'],2)}  ({data.get('sl_pct','NA')}%)

🧠 ML Probability : {round(data['ml_prob'],3)}
━━━━━━━━━━━━━━━━━━
"""


# ── Exit reason helpers ───────────────────────────────────────────────────────

def _fmt_held(seconds: float) -> str:
    """Format held duration as 'Xm Ys'."""
    s = int(seconds)
    return f"{s // 60}m {s % 60:02d}s"


def _map_exit_reason(raw: str, entry_price: float, stop_level: float) -> str:
    """
    Translate internal exit reason codes into display labels.

    Stop-type reasons are further refined by comparing stop_level to
    entry_price to distinguish a trailing stop from a raw stop loss.
    """
    if raw in ("STOP", "Stop Loss"):
        if stop_level > entry_price + 0.01:          # trail moved above cost
            return "TRAILING_STOP"
        if abs(stop_level - entry_price) <= 0.01:    # break-even lock
            return "BREAK_EVEN_STOP"
        return "STOP_LOSS_HIT"

    if raw.startswith("ML_UNRELIABLE_TODAY"):
        return "ML_UNRELIABLE_EXIT"

    return {
        "Drawdown":                  "PROFIT_PROTECTION",
        "TARGET_HIT":                "TARGET_HIT",
        "TIME_EXIT_WEAK":            "TIME_EXIT",
        "MANUAL":                    "MANUAL_EXIT",
        "FAST_REVERSAL_30S":         "FAST_REVERSAL",
        "VOLATILE_DAY_2PCT_ADVERSE": "DRAWDOWN_EXIT",
        "RANGE_DAY_FAST_EXIT":       "ML_EXIT",
        "TREND_DAY_ML_DISAGREES":    "ML_EXIT",
        "ML_EDGE_COLLAPSED":         "ML_EXIT",
    }.get(raw, raw)   # unknown reasons pass through unchanged


def format_trade_exit(data):
    entry        = data.get("entry_price", 0)
    exit_p       = data.get("exit_price", 0)
    trigger_p    = data.get("trigger_price", exit_p)   # LTP that crossed the threshold
    stop         = data.get("stop_level", 0)
    mfe_pts      = data.get("mfe_pts", 0.0)
    mae_pts      = data.get("mae_pts", 0.0)
    held_s       = data.get("held_seconds", 0.0)
    raw_reason   = data.get("reason", "")
    pnl          = data.get("pnl", 0)

    reason       = _map_exit_reason(raw_reason, entry, stop)
    held_str     = _fmt_held(held_s)
    move         = exit_p - entry
    mfe_str      = f"+{mfe_pts:.1f}" if mfe_pts >= 0 else f"{mfe_pts:.1f}"
    mae_str      = f"{mae_pts:.1f}"  if mae_pts <= 0 else f"+{mae_pts:.1f}"

    return f"""
🔴 TRADE EXIT CONFIRMED

📌 Instrument : {data['symbol']}
📍 Direction  : {data['side']}
📈 Regime     : {data.get('regime','TREND')}

💰 Entry Price    : {round(entry, 2)}
💰 Exit Price     : {round(exit_p, 2)}
📈 Price Move     : {round(move, 2)}

📦 Lot Size       : {data['qty']}
💵 Realized PnL   : {round(pnl, 2)}

🧠 ML at Entry    : {round(data.get('ml_prob', 0), 3)}

🛑 Exit Reason    : {reason}
🎯 Trigger Price  : {round(trigger_p, 2)}
📉 Stop Level     : {round(stop, 2)}
📊 MFE            : {mfe_str} pts
📊 MAE            : {mae_str} pts
⏱ Held           : {held_str}
━━━━━━━━━━━━━━━━━━━━
"""
