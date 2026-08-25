# telegram/messages.py
# Human-readable trade messages with live in-place updates.

import os
import re
from datetime import datetime

# BANKNIFTY lot size — same env key as engine.config.Config.LOT_SIZE so the
# displayed lot count always matches live sizing.
_DEFAULT_LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))


# ─────────────────────────────────────────────────────────────────────
# SYMBOL PARSER
# NIFTY2661623300PE  =>  NIFTY  10 Jun 2026  23300  PE
# ─────────────────────────────────────────────────────────────────────

_MONTHS = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}
_MONTH_NUMS = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}


def parse_symbol(raw: str) -> dict:
    """
    Parse Zerodha option symbol into human-readable components.

    Formats handled:
      NIFTY2661623300PE   YY=26 M=6(June) DD=16 strike=23300 type=PE
      NIFTY26JUN23300PE   YY=26 MON=JUN   strike=23300       type=PE

    NIFTY strikes are always 5 digits (23000-26000).
    Month is 1 digit (1-9) or 2 digits (10-12).
    """
    s = raw.upper().strip()

    # Format A: single-digit month  YY + M(1) + DD(2) + STRIKE(5)
    m = re.match(
        r'^(NIFTY|BANKNIFTY|SENSEX)(\d{2})(\d)(\d{2})(\d{5})(CE|PE)$', s
    )
    if m:
        idx, yy, mm, dd, strike, otype = m.groups()
        year   = 2000 + int(yy)
        month  = _MONTH_NUMS.get(mm.zfill(2), mm)
        day    = int(dd)
        expiry = f"{day} {month} {year}"
        return {"index": idx, "expiry": expiry, "strike": strike, "type": otype}

    # Format B: two-digit month  YY + MM(2) + DD(2) + STRIKE(5)
    m = re.match(
        r'^(NIFTY|BANKNIFTY|SENSEX)(\d{2})(\d{2})(\d{2})(\d{5})(CE|PE)$', s
    )
    if m:
        idx, yy, mm, dd, strike, otype = m.groups()
        year   = 2000 + int(yy)
        month  = _MONTH_NUMS.get(mm, mm)
        day    = int(dd)
        expiry = f"{day} {month} {year}"
        return {"index": idx, "expiry": expiry, "strike": strike, "type": otype}

    # Format C: 3-letter month  YY + MON(3) + STRIKE(4-5)
    # e.g. BANKNIFTY26AUG57500CE  =>  BANKNIFTY  Aug 2026  57500  CE
    m = re.match(
        r'^(NIFTY|BANKNIFTY|SENSEX)(\d{2})([A-Z]{3})(\d{4,5})(CE|PE)$', s
    )
    if m:
        idx, yy, mon, strike, otype = m.groups()
        year   = 2000 + int(yy)
        month  = _MONTHS.get(mon, mon)
        expiry = f"{month} {year}"
        return {"index": idx, "expiry": expiry, "strike": strike, "type": otype}

    # Fallback
    return {"index": raw, "expiry": "", "strike": "", "type": ""}


def fmt_symbol(raw: str) -> str:
    """Return human-readable symbol string.

    BANKNIFTY26AUG57500CE  =>  BANKNIFTY Aug 2026 57500 CE
    NIFTY2661623300PE      =>  NIFTY 16 Jun 2026 23300 PE
    """
    p = parse_symbol(raw)
    if p["expiry"]:
        return f"{p['index']} {p['expiry']} {p['strike']} {p['type']}"
    return raw


# ─────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────

def _pnl_emoji(pnl: float) -> str:
    if pnl > 0:
        return "💰"
    if pnl < 0:
        return "🔴"
    return "⚪"


def _side_emoji(side: str) -> str:
    return "📈" if side.upper() == "CE" else "📉"


def _held_str(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _pnl_str(pnl: float) -> str:
    if pnl >= 0:
        return f"+Rs {pnl:,.0f}"
    return f"-Rs {abs(pnl):,.0f}"


def _pts_str(pts: float) -> str:
    if pts >= 0:
        return f"+{pts:.1f}"
    return f"{pts:.1f}"


def _map_exit_reason(raw: str, entry_price: float, stop_level: float) -> tuple:
    """Returns (label, emoji)."""
    if raw in ("STOP", "Stop Loss"):
        if stop_level > entry_price + 0.01:
            return "Trailing Stop", "🛡️"
        if abs(stop_level - entry_price) <= 0.5:
            return "Break-Even Stop", "🔒"
        return "Stop Loss", "🛑"
    if raw.startswith("ML_UNRELIABLE"):
        return "ML Unreliable Exit", "🤖"
    mapping = {
        "Drawdown":                  ("Profit Protection", "🛡️"),
        "TARGET_HIT":                ("Target Hit", "🎯"),
        "TIME_EXIT_WEAK":            ("Time Exit", "⏱️"),
        "MANUAL":                    ("Manual Exit", "👆"),
        "FAST_REVERSAL_30S":         ("Fast Reversal", "⚡"),
        "VOLATILE_DAY_2PCT_ADVERSE": ("Drawdown Exit", "⬇️"),
        "RANGE_DAY_FAST_EXIT":       ("ML Exit", "🤖"),
        "TREND_DAY_ML_DISAGREES":    ("ML Exit", "🤖"),
        "ML_EDGE_COLLAPSED":         ("ML Exit", "🤖"),
    }
    label, emoji = mapping.get(raw, (raw, "❓"))
    return label, emoji


def _regime_emoji(regime: str) -> str:
    return {"TREND": "🏄", "RANGE": "〰️", "VOLATILE": "⚡", "GAP": "🚀"}.get(
        regime.upper(), "📊"
    )


# ─────────────────────────────────────────────────────────────────────
# TRADE ENTRY — initial message (with EXIT button attached by notifier)
# ─────────────────────────────────────────────────────────────────────

def format_trade_entry(data: dict) -> str:
    symbol   = fmt_symbol(data.get("symbol", ""))
    side     = data.get("side", "").upper()
    price    = data.get("price", 0.0)
    qty      = data.get("qty", 0)
    stop     = data.get("stop", 0.0)
    target   = data.get("target", 0.0)
    ml_prob  = data.get("ml_prob", 0.0)
    regime   = data.get("regime", "TREND")
    lot_size = data.get("lot_size") or _DEFAULT_LOT_SIZE
    lots     = qty // lot_size
    side_e   = _side_emoji(side)
    reg_e    = _regime_emoji(regime)
    sl_pts   = round(price - stop, 2)
    tgt_pts  = round(target - price, 2)
    now_str  = datetime.now().strftime("%H:%M:%S")

    return (
        f"{side_e} <b>TRADE OPEN — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"💵 Entry Price   : <b>{price:.1f}</b>\n"
        f"📦 Quantity      : {qty}  ({lots} lot{'s' if lots != 1 else ''})\n"
        f"\n"
        f"🛑 Stop Loss     : {stop:.1f}  <i>(-{sl_pts:.1f} pts)</i>\n"
        f"🎯 Target        : {target:.1f}  <i>(+{tgt_pts:.1f} pts)</i>\n"
        f"\n"
        f"🧠 ML Signal     : {ml_prob:.0%}  confidence\n"
        f"{reg_e} Market Regime : {regime}\n"
        f"\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"⏱️ Opened at {now_str}\n"
        f"<i>Live updates every 20s below ↓</i>"
    )


# ─────────────────────────────────────────────────────────────────────
# LIVE UPDATE — replaces entry message while trade is open
# Called every ~20s by the dashboard cycle
# ─────────────────────────────────────────────────────────────────────

def format_trade_live(position: dict, ltp: float, entry_time: datetime) -> str:
    symbol    = fmt_symbol(position.get("symbol", ""))
    side      = position.get("side", "").upper()
    entry     = position.get("entry", 0.0)
    stop      = position.get("stop_loss", 0.0)
    target    = position.get("target", 0.0)
    qty       = position.get("qty", 1)
    ml_prob   = position.get("ml_prob", 0.0)
    regime    = position.get("regime", "TREND")
    max_pnl   = position.get("max_pnl", 0.0)
    # Task #15 BUG 2: .get with default — restored/legacy positions may
    # lack lot_size (format_trade_exit used to KeyError on it).
    lot_size  = position.get("lot_size") or 30
    lots      = qty // lot_size

    pnl       = (ltp - entry) * qty
    move_pts  = ltp - entry
    peak_pts  = max_pnl / max(qty, 1)
    sl_pts    = entry - stop
    side_e    = _side_emoji(side)
    reg_e     = _regime_emoji(regime)
    now_str   = datetime.now().strftime("%H:%M:%S")
    held      = (datetime.now() - entry_time).total_seconds() if entry_time else 0

    # Status bar
    if pnl > 0:
        status = "🟢 IN PROFIT"
    elif pnl < 0:
        status = "🔴 IN LOSS"
    else:
        status = "⚪ BREAKEVEN"

    # Trail lock status
    from engine.execution.profit_manager import LOCK_PTS
    if peak_pts >= 40:
        lock_label = "🔒 80% locked"
    elif peak_pts >= 25:
        lock_label = "🔒 65% locked"
    elif peak_pts >= 15:
        lock_label = "🔒 40% locked"
    elif peak_pts >= 8:
        lock_label = f"🔒 Rs400/lot locked"
    elif peak_pts >= 4:
        lock_label = f"🔒 Rs200/lot locked"
    else:
        lock_label = "⏳ building..."

    pnl_sign = "+" if pnl >= 0 else ""
    move_sign = "+" if move_pts >= 0 else ""

    return (
        f"{side_e} <b>LIVE TRADE — {side}</b>  {status}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"💵 Entry          : {entry:.1f}\n"
        f"📡 LTP Now        : <b>{ltp:.1f}</b>  ({move_sign}{move_pts:.1f} pts)\n"
        f"💰 P&amp;L         : <b>{pnl_sign}Rs {abs(pnl):,.0f}</b>  ({pnl_sign}{pnl/(lots or 1):,.0f}/lot)\n"
        f"📈 Peak P&amp;L    : +Rs {max_pnl:,.0f}  ({_pts_str(peak_pts)} pts)\n"
        f"\n"
        f"🛑 Stop Level     : {stop:.1f}  ({lock_label})\n"
        f"🎯 Target         : {target:.1f}\n"
        f"\n"
        f"📦 Qty: {qty}  ({lots} lot{'s' if lots != 1 else ''})  "
        f"🧠 ML: {ml_prob:.0%}  "
        f"{reg_e} {regime}\n"
        f"⏱️ Held: {_held_str(held)}  |  🕐 {now_str}"
    )


# ─────────────────────────────────────────────────────────────────────
# TRADE EXIT — new message posted after trade closes
# ─────────────────────────────────────────────────────────────────────

def format_trade_exit(data: dict) -> str:
    symbol      = fmt_symbol(data.get("symbol", ""))
    side        = data.get("side", "").upper()
    entry       = data.get("entry_price", 0.0)
    exit_p      = data.get("exit_price", 0.0)
    qty         = data.get("qty", 1)
    pnl         = data.get("pnl", 0.0)
    ml_prob     = data.get("ml_prob", 0.0)
    regime      = data.get("regime", "TREND")
    stop        = data.get("stop_level", 0.0)
    mfe_pts     = data.get("mfe_pts", 0.0)
    mae_pts     = data.get("mae_pts", 0.0)
    held_s      = data.get("held_seconds", 0.0)
    raw_reason  = data.get("reason", "")
    # Task #15 BUG 2: .get with default — the exit dict built by
    # master_runner historically lacked lot_size -> KeyError here.
    lot_size    = data.get("lot_size") or 30
    lots        = qty // lot_size

    reason_label, reason_emoji = _map_exit_reason(raw_reason, entry, stop)
    pnl_e    = _pnl_emoji(pnl)
    side_e   = _side_emoji(side)
    reg_e    = _regime_emoji(regime)
    move_pts = exit_p - entry
    pnl_sign = "+" if pnl >= 0 else ""
    per_lot  = pnl / max(lots, 1)

    result_line = (
        f"✅ <b>PROFIT  {pnl_sign}Rs {abs(pnl):,.0f}</b>"
        if pnl >= 0 else
        f"❌ <b>LOSS  -Rs {abs(pnl):,.0f}</b>"
    )

    return (
        f"{side_e} <b>TRADE CLOSED — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"{result_line}\n"
        f"📊 Per lot        : {pnl_sign}Rs {abs(per_lot):,.0f}\n"
        f"\n"
        f"💵 Entry          : {entry:.1f}\n"
        f"🚪 Exit           : {exit_p:.1f}  ({_pts_str(move_pts)} pts)\n"
        f"\n"
        f"{reason_emoji} Exit Reason    : <b>{reason_label}</b>\n"
        f"📈 Best (MFE)     : {_pts_str(mfe_pts)} pts\n"
        f"📉 Worst (MAE)    : {_pts_str(mae_pts)} pts\n"
        f"⏱️ Held           : {_held_str(held_s)}\n"
        f"\n"
        f"🧠 ML at Entry    : {ml_prob:.0%}  |  "
        f"{reg_e} {regime}  |  📦 {qty} qty\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )


# ─────────────────────────────────────────────────────────────────────
# SCALP MESSAGES — entry / live / exit cards for the scalp layer
# ─────────────────────────────────────────────────────────────────────

def format_scalp_entry(pos: dict, move_pts: float) -> str:
    symbol    = fmt_symbol(pos.get("symbol", ""))
    side      = pos.get("side", "").upper()
    price     = pos.get("entry", 0.0)
    stop      = pos.get("stop_loss", 0.0)
    target    = pos.get("target", 0.0)
    qty       = pos["qty"]
    lot_size  = pos["lot_size"]
    lots      = max(1, round(qty / lot_size))
    side_e    = _side_emoji(side)
    tgt_pts   = round(target - price, 1)
    sl_pts    = round(price - stop, 1)
    now_str   = datetime.now().strftime("%H:%M:%S")
    move_sign = "+" if move_pts >= 0 else ""

    return (
        f"⚡ {side_e} <b>SCALP OPEN — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"💵 Entry Price   : <b>{price:.1f}</b>\n"
        f"📦 Quantity      : {qty}  ({lots} lot{'s' if lots > 1 else ''})\n"
        f"\n"
        f"🛑 Stop Loss     : {stop:.1f}  <i>(-{abs(sl_pts):.1f} pts)</i>\n"
        f"🎯 Target        : {target:.1f}  <i>(+{abs(tgt_pts):.1f} pts)</i>\n"
        f"\n"
        f"⚡ Trigger       : NIFTY {move_sign}{move_pts:.1f} pts momentum\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"⏱️ Opened at {now_str}  |  <i>live updates below</i>"
    )


def format_scalp_live(pos: dict, ltp: float) -> str:
    symbol   = fmt_symbol(pos.get("symbol", ""))
    side     = pos.get("side", "").upper()
    entry    = pos.get("entry", 0.0)
    stop     = pos.get("stop_loss", 0.0)
    target   = pos.get("target", 0.0)
    qty      = pos["qty"]
    lot_size = pos["lot_size"]
    max_pnl  = pos.get("max_pnl", 0.0)
    entry_ts = pos.get("entry_ts")
    side_e   = _side_emoji(side)

    pnl       = (ltp - entry) * qty
    move_pts  = ltp - entry
    peak_pts  = max_pnl / max(qty, 1)
    pnl_sign  = "+" if pnl >= 0 else ""
    move_sign = "+" if move_pts >= 0 else ""
    status    = "🟢 IN PROFIT" if pnl > 0 else ("🔴 IN LOSS" if pnl < 0 else "⚪ BREAKEVEN")
    held      = (datetime.now() - entry_ts).total_seconds() if entry_ts else 0
    now_str   = datetime.now().strftime("%H:%M:%S")

    return (
        f"⚡ {side_e} <b>SCALP LIVE — {side}</b>  {status}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"💵 Entry          : {entry:.1f}\n"
        f"📡 LTP Now        : <b>{ltp:.1f}</b>  ({move_sign}{move_pts:.1f} pts)\n"
        f"💰 P&amp;L         : <b>{pnl_sign}Rs {abs(pnl):,.0f}</b>\n"
        f"📈 Peak P&amp;L    : +Rs {max_pnl:,.0f}  ({_pts_str(peak_pts)} pts)\n"
        f"\n"
        f"🛑 Stop           : {stop:.1f}  🎯 Target: {target:.1f}\n"
        f"\n"
        f"⏱️ Held: {_held_str(held)}  |  🕐 {now_str}"
    )


def format_scalp_exit(pos: dict, fill: float, reason: str, pnl: float) -> str:
    symbol   = fmt_symbol(pos.get("symbol", ""))
    side     = pos.get("side", "").upper()
    entry    = pos.get("entry", 0.0)
    # Task #20 FIX 8: .get with defaults — restored/legacy scalp positions
    # may lack qty/lot_size (same KeyError class fixed in format_trade_live
    # and format_trade_exit).
    qty      = pos.get("qty", 0)
    lot_size = pos.get("lot_size") or 30
    max_pnl  = pos.get("max_pnl", 0.0)
    min_pnl  = pos.get("min_pnl", 0.0)
    entry_ts = pos.get("entry_ts")
    side_e   = _side_emoji(side)

    held_s   = (datetime.now() - entry_ts).total_seconds() if entry_ts else 0
    move_pts = fill - entry
    pnl_sign = "+" if pnl >= 0 else ""
    peak_pts = max_pnl / max(qty, 1)
    mae_pts  = min_pnl / max(qty, 1)

    _reason_map = {
        "SCALP_STOP":     ("Stop Loss",   "🛑"),
        "SCALP_TARGET":   ("Target Hit",  "🎯"),
        "SCALP_TIME_EXIT": ("Time Limit", "⏳"),
    }
    reason_label, reason_e = _reason_map.get(reason, (reason, "📤"))
    result_line = (
        f"✅ <b>PROFIT  {pnl_sign}Rs {abs(pnl):,.0f}</b>"
        if pnl >= 0 else
        f"❌ <b>LOSS  -Rs {abs(pnl):,.0f}</b>"
    )

    return (
        f"⚡ {side_e} <b>SCALP CLOSED — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"{result_line}\n"
        f"\n"
        f"💵 Entry          : {entry:.1f}\n"
        f"🚪 Exit           : {fill:.1f}  ({_pts_str(move_pts)} pts)\n"
        f"\n"
        f"{reason_e} Exit Reason    : <b>{reason_label}</b>\n"
        f"📈 Best (MFE)     : {_pts_str(peak_pts)} pts\n"
        f"📉 Worst (MAE)    : {_pts_str(mae_pts)} pts\n"
        f"⏱️ Held           : {_held_str(held_s)}\n"
        f"📦 Qty: {qty}  ({qty // lot_size if lot_size > 0 else 1} lot)  ⚡ Scalp\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )


# ─────────────────────────────────────────────────────────────────────
# DASHBOARD SECTION HELPERS  (Features 3/4/5/6/8)
# ─────────────────────────────────────────────────────────────────────

def _section_block_counts(counts: dict) -> str:
    if not counts:
        return ""
    # Sort by count desc, show top 8
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:8]
    rows = "\n".join(f"{k:<20} {v}" for k, v in top)
    return f"\n<b>TRADE BLOCKERS</b>\n<code>{rows}</code>"


def _section_ml_analytics(ms: dict) -> str:
    ce  = ms.get("ce_adj", 0.0)
    pe  = ms.get("pe_adj", 0.0)
    edge = ms.get("ml_edge", abs(ce - pe))
    pct  = ms.get("ml_percentile", 0)
    return (
        f"\n<b>ML ANALYTICS</b>\n"
        f"CE {ce:.2f}  PE {pe:.2f}  "
        f"EDGE {edge:.2f}  Pct {pct}%"
    )


def _section_exit_analytics(ctx) -> str:
    ea = getattr(ctx, "exit_analytics", None)
    if not ea or not ea.get("realized_list"):
        return ""
    n = len(ea["realized_list"])
    avg_mfe  = sum(ea["mfe_rs_list"])  / n
    avg_mae  = sum(ea["mae_rs_list"])  / n
    avg_real = sum(ea["realized_list"]) / n
    cap_list = [c for c in ea["capture_list"] if c != 0]
    avg_cap  = sum(cap_list) / len(cap_list) if cap_list else 0.0
    mfe_s  = f"+₹{avg_mfe:,.0f}"  if avg_mfe  >= 0 else f"-₹{abs(avg_mfe):,.0f}"
    mae_s  = f"-₹{abs(avg_mae):,.0f}" if avg_mae <= 0 else f"+₹{avg_mae:,.0f}"
    real_s = f"+₹{avg_real:,.0f}" if avg_real >= 0 else f"-₹{abs(avg_real):,.0f}"
    return (
        f"\n<b>EXIT ANALYTICS</b>  ({n} trades)\n"
        f"Avg MFE      {mfe_s}\n"
        f"Avg MAE      {mae_s}\n"
        f"Avg Realized {real_s}\n"
        f"Capture Ratio {avg_cap:.0%}"
    )


def _section_exit_breakdown(ctx) -> str:
    counts = getattr(ctx, "exit_type_counts", {})
    if not counts:
        return ""
    rows = "\n".join(
        f"{k:<22} {v}"
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    )
    return f"\n<b>EXIT BREAKDOWN</b>\n<code>{rows}</code>"


def _section_feed_health(ms: dict) -> str:
    fh = ms.get("feed_health")
    if not fh:
        return ""
    ws_s  = "YES" if fh.get("ws_connected") else "NO"
    age   = fh.get("tick_age_s", 0)
    age_s = f"{age:.1f}s" if age < 60 else f"{age/60:.1f}m"
    cl    = fh.get("chain_live", 0)
    ct    = fh.get("chain_total", 0)
    ce_oi = fh.get("ce_oi", 0) / 1_000_000
    pe_oi = fh.get("pe_oi", 0) / 1_000_000
    orb   = ms.get("orb_mode", "")
    rows  = (
        f"{'WS Connected':<18} {ws_s}\n"
        f"{'Tick Age':<18} {age_s}\n"
        f"{'Tokens':<18} {fh.get('token_count', 0)}\n"
        f"{'Chain Live':<18} {cl}/{ct}\n"
        f"{'CE OI':<18} {ce_oi:.1f}M\n"
        f"{'PE OI':<18} {pe_oi:.1f}M"
        + (f"\n{'ORB':<18} {orb}" if orb else "")
    )
    return f"\n<b>FEED HEALTH</b>\n<code>{rows}</code>"



# ─────────────────────────────────────────────────────────────────────
# HUMAN-READABLE BLOCKER TRANSLATION
# ─────────────────────────────────────────────────────────────────────

_BLOCK_HUMAN = {
    "WARMING_UP":             "⏳ Warming up — collecting data",
    "ORB_BUILD":              "🌅 Building Opening Range (9:15–9:30)",
    "MARKET_CLOSING":         "🔚 Market closing (after 15:15)",
    "LUNCH_FILTER":           "🍱 Lunch filter (11:00–12:30)",
    "WARMUP_BLOCK":           "⏳ Warm-up — no entries yet",
    "INSUFFICIENT_DATA":      "📉 Not enough candles yet",
    "RANGE_REGIME_SKIP":      "〰️ Sideways (range) day — skipping (historically weak)",
    "COOLDOWN":               "🕐 Cooling off after last trade",
    "NO_DIRECTION":           "〰️ No clear direction (SuperTrend flat)",
    "HTF5_OPPOSES":           "↕️ 5-min trend opposes 1-min — waiting for alignment",
    "TRAP_FILTER":            "🎭 Breakout trap detected — skipping",
    "SIGNAL_FIRE":            "🟢 SIGNAL FIRING",
    "NO_SIGNAL":              "🕐 Waiting for a signal",
    "PNL_GUARD":              "🛡️ Daily loss limit reached — entries paused",
    "CONFIRM_NO_HISTORY":     "📉 Not enough price history for confirmation",
    "CONFIRM_NO_HH":          "📉 No higher-high structure for CE",
    "CONFIRM_NO_LL":          "📉 No lower-low structure for PE",
    "CONFIRM_STRUCT_BREAK":   "💥 Structure broke — move gave back too much",
    "CONFIRM_BAD_PULLBACK":   "↩️ Bad pullback timing (chasing or too deep)",
    "CONFIRM_PULLBACK_FAIL":  "↩️ Pullback failed — price broke the entry zone",
    "CONFIRM_NO_MOMENTUM":    "🌀 Momentum stalled — last ticks not pushing",
    "CONFIRM_HTF_NEUTRAL":    "〰️ 5-min trend neutral — no confirmation",
    "CONFIRM_HTF_OPPOSES":    "↕️ 5-min trend opposes — skipping",
    "CONFIRM_BREAKOUT_TRAP":  "🎭 Breakout snapped back — trap avoided",
    "CONFIRM_SPIKE_TRAP":     "🎭 Spike-and-reverse — trap avoided",
    "CONFIRM_MICRO_REVERSAL": "🌀 Micro reversal detected — skipping",
    "CE_WEAK":                "CE signal too weak",
    "CE_WEAK_RANK":           "CE ranked weak vs other signals",
    "ENTRY_SLIPPAGE":         "💸 Entry spread/slippage too wide",
    "HTF_FAIL":               "5-min trend check failed",
    "HTF_MISALIGN":           "5-min trend misaligned",
    "ML_BELOW_THR":           "ML confidence below threshold",
    "ML_BLOCKED":             "ML engine blocked the signal",
    "NO_EDGE":                "No ML edge (CE vs PE gap too small)",
    "NO_STRUCTURE":           "No price structure confirmation",
    "PULLBACK_WAIT":          "↩️ Waiting for pullback",
    "RANGE_REGIME":           "〰️ Range day detected",
    "VWAP_FAIL":              "VWAP not aligned",
    "VWAP_NEAR_MISS":         "VWAP barely missed",
    "ML_EDGE_MARGIN":         "ML edge margin not met",
    "REENTRY_COOLDOWN":       "🕐 Re-entry cooldown active",
    "PREDICT_FIRST":          "ML-first gate",
    "REQUIRE_HTF_ALIGN":      "HTF alignment required",
    "REQUIRE_VWAP_ALIGN":     "VWAP alignment required",
    "SKIP_RANGE_REGIME":      "Range regime skip",
    "MAX_ENTRY_SLIP_PTS":     "💸 Entry slippage cap",
    "MAX_HOLD_SECONDS":       "⏱️ Max hold reached",
    "INITIAL_SL_MULT":        "Initial SL multiplier",
    "TIME_EXIT_WEAK":         "⏱️ Time exit (weak move)",
    "HTF":                    "HTF gate",
    "VWAP":                   "VWAP gate",
}


def _human_block(reason: str) -> str:
    """Translate a raw block reason (e.g. 'WARMUP_BLOCK (until 10:45)') to plain language."""
    if not reason:
        return "🕐 Waiting"
    if reason.startswith("SIGNAL_FIRE"):
        return "🟢 <b>SIGNAL FIRING</b>"
    key = reason.split("(")[0].strip()
    human = _BLOCK_HUMAN.get(key) or _BLOCK_HUMAN.get(reason) or reason
    if "RANGE_REGIME" in reason and "(" in reason:
        return f"{human} {reason[reason.find('('):]}"
    return human


def _human_block_counts(counts: dict) -> str:
    """Block counts (today) in plain language, top 5."""
    if not counts:
        return ""
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
    rows = []
    for k, v in top:
        human = _BLOCK_HUMAN.get(k, k.replace("_", " "))
        rows.append(f"  {human:<28} x{v}")
    return "\n" + "\n".join(rows)


def format_engine_dashboard(ctx, market_state: dict, ltp: float = 0.0) -> str:
    """Human-readable AI engine status for Telegram."""
    ms       = market_state or {}
    now_str  = datetime.now().strftime("%H:%M:%S")

    # Engine status — is it still trading / paused / stopped?
    import telegram.notifier as _tn
    if getattr(_tn, "ENGINE_STOP_REQUESTED", False):
        status_line = "🛑 <b>STOPPED</b> (via /stop)"
    elif getattr(_tn, "ENGINE_PAUSED", False):
        status_line = "⏸️ <b>PAUSED</b> (safety — send /resume)"
    else:
        status_line = "✅ <b>RUNNING</b>"

    # Direction
    st_dir = ms.get("supertrend_dir", 0)
    pvwap  = ms.get("price_vs_vwap", 0.0) * 100
    rsi    = ms.get("rsi_1m", 50.0)
    adx    = ms.get("adx", 0.0)

    if st_dir > 0:
        dir_line = "📈 <b>BULLISH</b> ↑"
    elif st_dir < 0:
        dir_line = "📉 <b>BEARISH</b> ↓"
    else:
        dir_line = "〰️ <b>NEUTRAL</b> (no trend)"

    vwap_e = "✅" if pvwap >= 0 else "⚠️"
    vwap_sign = "above" if pvwap >= 0 else "below"

    # ML confidence bars
    ce_adj = ms.get("ce_adj", 0.0)
    pe_adj = ms.get("pe_adj", 0.0)
    ce_thr = ms.get("ce_threshold", 0.70)
    pe_thr = ms.get("ml_threshold", 0.65)

    def _bar(v, thr):
        filled = max(0, min(10, round(v * 10)))
        bar = "█" * filled + "░" * (10 - filled)
        ok = "✅" if v >= thr else ("🟡" if v >= thr - 0.06 else "🔴")
        return f"{bar} {v:.2f} {ok}"

    # Decision / current blocker in plain language
    block_raw = ms.get("block_reason", "WARMING_UP")
    decision_line = _human_block(block_raw)
    if not block_raw.startswith("SIGNAL_FIRE"):
        decision_line = f"⏳ Waiting — {decision_line}"

    # Today stats
    trades_today = getattr(ctx, "trades_today", 0)
    pnl          = getattr(ctx, "pnl", 0.0)
    positions    = getattr(ctx, "positions", [])
    wins         = sum(1 for p in positions if p > 0)
    losses       = len(positions) - wins
    wr           = (wins / len(positions) * 100) if positions else 0
    pnl_e        = "💰" if pnl >= 0 else "🔴"
    ltp_str      = f"{ltp:,.1f}" if ltp else "---"

    # Last trade
    lt = getattr(ctx, "last_trade", None) or {}
    if lt.get("symbol"):
        lt_pnl = lt.get("pnl", 0)
        lt_e   = "✅" if lt_pnl >= 0 else "🔴"
        last_line = (
            f"{lt_e} {lt.get('ts','')}  {fmt_symbol(lt.get('symbol',''))}\n"
            f"   Entry {lt.get('entry',0):.1f} → Exit {lt.get('exit',0):.1f}  "
            f"P&L <b>{'+' if lt_pnl>=0 else ''}₹{lt_pnl:,.0f}</b>  ({lt.get('reason','')})"
        )
    else:
        last_line = "— no trades yet today"

    return (
        f"🤖 <b>AI ENGINE</b>  {status_line}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"🕐 {now_str}   📡 <b>BANK NIFTY</b> {ltp_str}\n"
        f"\n"
        f"<b>MARKET</b>\n"
        f"{dir_line}   {vwap_e} VWAP {vwap_sign} ({pvwap:+.2f}%)\n"
        f"📊 RSI(1m): {rsi:.1f}   ADX: {adx:.1f}\n"
        f"\n"
        f"<b>ML CONFIDENCE</b>\n"
        f"📈 CE  {_bar(ce_adj, ce_thr)}\n"
        f"📉 PE  {_bar(pe_adj, pe_thr)}\n"
        f"\n"
        f"<b>WHAT'S HAPPENING</b>\n"
        f"{decision_line}\n"
        + (_human_block_counts(ms.get("block_counts", {})) if ms.get("block_counts") else "")
        + f"\n\n"
        f"<b>LAST TRADE</b>\n{last_line}\n"
        f"\n"
        f"<b>TODAY</b>  ({trades_today} trades)\n"
        f"{pnl_e} P&amp;L: <b>{'+' if pnl>=0 else ''}₹{pnl:,.0f}</b>   "
        f"W/L: {wins}/{losses}   WR: {wr:.0f}%\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )
