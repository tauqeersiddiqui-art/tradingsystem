# telegram/messages.py
# Human-readable trade messages with live in-place updates.
# Enhanced with Decision Intelligence context for trade filtering.

import re
from datetime import datetime


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
    m = re.match(
        r'^(NIFTY|BANKNIFTY|SENSEX)(\d{2})([A-Z]{3})(\d{4,5})(CE|PE)$', s
    )
    if m:
        idx, yy, mon, strike, otype = m.groups()
        year   = 2000 + int(yy)
        month  = _MONTHS.get(mon, mon)
        expiry = f"{mon} {year}"
        return {"index": idx, "expiry": expiry, "strike": strike, "type": otype}

    # Fallback
    return {"index": raw, "expiry": "", "strike": "", "type": ""}


def fmt_symbol(raw: str) -> str:
    """Return human-readable symbol string."""
    p = parse_symbol(raw)
    if p["expiry"]:
        return f"{p['index']}  {p['expiry']}  {p['strike']}  {p['type']}"
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


def _trail_lock_label(entry: float, stop: float, qty: int, max_pnl: float) -> str:
    locked_pts = max(0.0, stop - entry)
    if locked_pts <= 0:
        peak_pts = max_pnl / max(qty, 1)
        return f"building; peak {_pts_str(peak_pts)}pt"

    locked_rs = locked_pts * max(qty, 1)
    if max_pnl > 0:
        capture = min(999.0, max(0.0, locked_rs / max_pnl * 100.0))
        return f"locked +{locked_pts:.1f}pt / Rs{locked_rs:,.0f} ({capture:.0f}% peak)"
    return f"locked +{locked_pts:.1f}pt / Rs{locked_rs:,.0f}"


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
    lots     = qty // 30
    side_e   = _side_emoji(side)
    reg_e    = _regime_emoji(regime)
    sl_pts   = round(price - stop, 2)
    tgt_pts  = round(target - price, 2)
    now_str  = datetime.now().strftime("%H:%M:%S")

    # ── Decision Intelligence Context (if available) ──
    decision_info = ""
    ds = data.get("decision_score")
    if ds is not None:
        # DecisionScore dataclass from decision_intelligence
        try:
            final_score = getattr(ds, "final_score", 0.0)
            threshold = getattr(ds, "threshold", 0.0)
            ml_contrib = getattr(ds, "ml_contribution", 0.0)
            global_state = getattr(ds, "global_state", 0.0)
            vol_factor = getattr(ds, "volatility_factor", 1.0)

            # Global market state emoji
            gm_state = data.get("global_market_state")
            gm_emoji = "🟢"
            if gm_state is not None:
                risk_state = getattr(gm_state, "risk_state", "NEUTRAL")
                if risk_state == "RISK_ON":
                    gm_emoji = "🟢"
                elif risk_state == "RISK_OFF":
                    gm_emoji = "🔴"
                else:
                    gm_emoji = "🟡"

            decision_info = (
                f"\n"
                f"🎯 DI Score     : {final_score:.2f}/{threshold:.2f}\n"
                f"   ML contrib   : {ml_contrib:.2f} | Global: {global_state:+.1f}\n"
                f"   {gm_emoji} Risk State : {gm_state.risk_state if gm_state else 'N/A'} | Vol: {vol_factor:.1f}x"
            )
        except Exception:
            pass  # Fail-safe: if parsing fails, skip the extra info

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
        f"{reg_e} Market Regime : {regime}{decision_info}\n"
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
    lots      = qty // 30

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

    lock_label = _trail_lock_label(entry, stop, qty, max_pnl)

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
    lots        = qty // 30

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
        f"📦 Quantity      : 30  (1 lot)\n"
        f"\n"
        f"🛑 Stop Loss     : {stop:.1f}  <i>(-{abs(sl_pts):.1f} pts)</i>\n"
        f"🎯 Target        : {target:.1f}  <i>(+{abs(tgt_pts):.1f} pts)</i>\n"
        f"\n"
        f"⚡ Trigger       : BANKNIFTY {move_sign}{move_pts:.1f} pts momentum\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"⏱️ Opened at {now_str}  |  <i>live updates below</i>"
    )


def format_scalp_live(pos: dict, ltp: float) -> str:
    symbol   = fmt_symbol(pos.get("symbol", ""))
    side     = pos.get("side", "").upper()
    entry    = pos.get("entry", 0.0)
    stop     = pos.get("stop_loss", 0.0)
    target   = pos.get("target", 0.0)
    qty      = pos.get("qty", 30)
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
        f"🛑 Stop           : {stop:.1f}  ({_trail_lock_label(entry, stop, qty, max_pnl)})\n"
        f"🎯 Target         : {target:.1f}\n"
        f"\n"
        f"⏱️ Held: {_held_str(held)}  |  🕐 {now_str}"
    )


def format_scalp_exit(pos: dict, fill: float, reason: str, pnl: float) -> str:
    symbol   = fmt_symbol(pos.get("symbol", ""))
    side     = pos.get("side", "").upper()
    entry    = pos.get("entry", 0.0)
    qty      = pos.get("qty", 30)
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
        f"📦 Qty: {qty}  (1 lot)  ⚡ Scalp\n"
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


def _sparkline(values: list, width: int = 32) -> str:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    low = min(vals)
    high = max(vals)
    if high <= low:
        return "-" * len(vals)
    chars = "._:-=+*#"
    scale = (len(chars) - 1) / (high - low)
    return "".join(chars[int((v - low) * scale)] for v in vals)


def _section_banknifty_chart(ms: dict) -> str:
    chart = ms.get("banknifty_chart") or {}
    closes = chart.get("closes") or []
    line = _sparkline(closes)
    if not line:
        return ""

    first = float(chart.get("first", closes[0]))
    last = float(chart.get("last", closes[-1]))
    high = float(chart.get("high", max(closes)))
    low = float(chart.get("low", min(closes)))
    change = last - first
    start_ts = chart.get("start", "--")
    end_ts = chart.get("end", "--")
    moves = chart.get("moves") or {}
    interval_label = str(chart.get("interval_label") or "LIVE").upper()
    move_labels = chart.get("move_labels") or ["5m", "15m", "30m"]

    def _fmt_move(value):
        return "--" if value is None else f"{float(value):+.1f}"

    move_text = "  ".join(
        f"{label} {_fmt_move(moves.get(label))}" for label in move_labels
    )

    return (
        f"\n<b>BANKNIFTY LIVE CHART ({interval_label})</b>\n"
        f"<code>{line}</code>\n"
        f"<code>{start_ts} {first:,.1f} -> {end_ts} {last:,.1f} ({change:+.1f})</code>\n"
        f"<code>H {high:,.1f}  L {low:,.1f}  {move_text}</code>\n"
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
    orb_high = ms.get("orb_high")
    orb_low = ms.get("orb_low")
    if orb and orb_high is not None and orb_low is not None:
        orb = f"{orb} OR(9:15-9:29) H={float(orb_high):.0f} L={float(orb_low):.0f}"
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



def format_engine_dashboard(ctx, market_state: dict, ltp: float = 0.0) -> str:
    """Rich AI engine status — called by render_engine in dashboard.py."""
    ms       = market_state or {}
    now_str  = datetime.now().strftime("%H:%M:%S")
    session  = ms.get("session", "ACTIVE")

    # Direction
    st_dir   = ms.get("supertrend_dir", 0)
    vwap     = ms.get("vwap", 0.0)
    pvwap    = ms.get("price_vs_vwap", 0.0) * 100
    rsi      = ms.get("rsi_1m", ms.get("rsi", 50.0))
    adx      = ms.get("adx", 0.0)
    htf5_dir = ms.get("htf5_dir", 0)

    if st_dir > 0:
        dir_line = "📈 SuperTrend: <b>BULLISH</b> ↑"
    elif st_dir < 0:
        dir_line = "📉 SuperTrend: <b>BEARISH</b> ↓"
    else:
        dir_line = "〰️ SuperTrend: <b>NEUTRAL</b>"

    vwap_sign = "above" if pvwap >= 0 else "below"
    if htf5_dir > 0:
        htf_line = "5m SuperTrend: <b>BULLISH</b>"
    elif htf5_dir < 0:
        htf_line = "5m SuperTrend: <b>BEARISH</b>"
    else:
        htf_line = "5m SuperTrend: <b>NEUTRAL</b>"
    vwap_e    = "✅" if pvwap >= 0 else "⚠️"

    # ML bias bars
    ce_adj  = ms.get("ce_adj", 0.0)
    pe_adj  = ms.get("pe_adj", 0.0)
    ce_thr  = ms.get("ce_threshold", 0.70)
    pe_thr  = ms.get("ml_threshold", 0.65)
    candidate = "CE" if ce_adj >= pe_adj else "PE"
    phase55 = ms.get("phase55") or {}
    phase55_status = "Enabled" if phase55.get("enabled") else "Disabled"
    phase55_evaluated = int(phase55.get("trades_evaluated", 0) or 0)
    phase55_allowed = int(phase55.get("trades_allowed", 0) or 0)
    phase55_blocked = int(phase55.get("trades_blocked", 0) or 0)
    phase55_correct = int(phase55.get("correct_blocks", 0) or 0)
    phase55_false = int(phase55.get("false_positive_blocks", 0) or 0)
    phase55_saved = float(phase55.get("estimated_pnl_saved", 0.0) or 0.0)
    phase55_missed = float(phase55.get("estimated_pnl_missed", 0.0) or 0.0)

    def _bar(v, thr):
        filled = max(0, min(10, round(v * 10)))
        bar = "█" * filled + "░" * (10 - filled)
        ok = "✅" if v >= thr else ("🟡" if v >= thr - 0.06 else "🔴")
        return f"{bar} {v:.2f} {ok}"

    def _phase55_rs(value: float) -> str:
        return f"+Rs {value:,.0f}" if value >= 0 else f"-Rs {abs(value):,.0f}"

    # Decision
    block_raw = ms.get("block_reason", "WARMING_UP")
    if block_raw.startswith("SIGNAL_FIRE"):
        decision_line = "🟢 <b>SIGNAL FIRING</b>"
    else:
        import html as _html
        decision_line = f"🔴 Waiting — {_html.escape(block_raw)}"

    # Stats
    positions    = getattr(ctx, "positions", [])
    trades_today = getattr(ctx, "trades_today", 0)
    pnl          = getattr(ctx, "pnl", 0.0)
    wins         = sum(1 for p in positions if p > 0)
    losses       = len(positions) - wins
    wr           = (wins / len(positions) * 100) if positions else 0
    pf_wins      = sum(p for p in positions if p > 0)
    pf_loss      = sum(abs(p) for p in positions if p < 0)
    pf           = (pf_wins / pf_loss) if pf_loss > 0 else float("inf")
    pf_str       = f"{pf:.2f}" if pf != float("inf") else "inf"
    pnl_e        = "💰" if pnl >= 0 else "🔴"
    ltp_str      = f"{ltp:,.1f}" if ltp else "---"

    return (
        f"🤖 <b>AI ENGINE STATUS</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"🕐 {now_str}   📡 BANKNIFTY <b>{ltp_str}</b>\n"
        f"\n"
        f"<b>MARKET DIRECTION</b>\n"
        f"{dir_line}\n"
        f"{htf_line}\n"
        f"{vwap_e} VWAP: {vwap:,.0f}  ({pvwap:+.2f}%  —  price {vwap_sign})\n"
        f"📊 RSI: {rsi:.1f}   ADX: {adx:.1f}\n"
        f"\n"
        f"<b>ML CONFIDENCE</b>\n"
        f"Candidate: <b>{candidate}</b> (no entry until threshold + structure pass)\n"
        f"📈 CE  {_bar(ce_adj, ce_thr)}\n"
        f"📉 PE  {_bar(pe_adj, pe_thr)}\n"
        f"\n"
        f"<b>PHASE55</b>\n"
        f"Enabled: <b>{phase55_status}</b>\n"
        f"Trades Evaluated: {phase55_evaluated}\n"
        f"Allowed: {phase55_allowed}   Blocked: {phase55_blocked}\n"
        f"Correct Blocks: {phase55_correct}   False Blocks: {phase55_false}\n"
        f"Estimated PnL Saved: {_phase55_rs(phase55_saved)}\n"
        f"Estimated PnL Missed: {_phase55_rs(-phase55_missed)}\n"
        + _section_banknifty_chart(ms)
        + _section_ml_analytics(ms)
        + f"\n\n"
        f"<b>DECISION</b>\n"
        f"{decision_line}\n"
        + _section_block_counts(ms.get("block_counts", {}))
        + f"\n\n"
        f"<b>TODAY  ({trades_today} trades)</b>\n"
        f"{pnl_e} P&amp;L: <b>{'+'if pnl>=0 else ''}Rs {pnl:,.0f}</b>   "
        f"W/L: {wins}/{losses}   WR: {wr:.0f}%\n"
        f"PF: {pf_str}\n"
        + _section_exit_analytics(ctx)
        + _section_exit_breakdown(ctx)
        + _section_feed_health(ms)
        + f"\n<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )


# ─────────────────────────────────────────────────────────────────────
# DECISION INTELLIGENCE — Enhanced trade filtering messages
# ─────────────────────────────────────────────────────────────────────

def format_decision_skip(
    symbol: str,
    side: str,
    ml_confidence: float,
    global_state: str,
    volatility: str,
    final_score: float,
    threshold: float,
    skip_reason: str,
    entry_price: float = 0.0,
) -> str:
    """
    Format a skipped trade message showing decision intelligence breakdown.
    """
    side_e = "📈" if side.upper() == "CE" else "📉"
    risk_e = {"RISK_ON": "🚀", "RISK_OFF": "🛑", "NEUTRAL": "🟡"}.get(global_state, "📊")
    vol_e = {"LOW": "平静", "NORMAL": "温和", "HIGH": "动荡"}.get(volatility, "📊")

    return (
        f"{side_e} <b>TRADE SKIPPED — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"🧠 ML Confidence  : {ml_confidence:.0%}\n"
        f"{risk_e} Global State   : <b>{global_state}</b>\n"
        f"{vol_e} Volatility     : <b>{volatility}</b>\n"
        f"\n"
        f"📊 Final Score    : <b>{final_score:.3f}</b>\n"
        f"门槛 Threshold    : <b>{threshold:.3f}</b>\n"
        f"\n"
        f"❌ Decision: <b>SKIP</b>\n"
        f"Reason: <code>{skip_reason}</code>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )


def format_decision_allow(
    symbol: str,
    side: str,
    entry_price: float,
    qty: int,
    stop_loss: float,
    ml_confidence: float,
    global_state: str,
    volatility: str,
    final_score: float,
    threshold: float,
    lots: int = None,
) -> str:
    """
    Format an allowed trade message with decision intelligence context.
    """
    side_e = "📈" if side.upper() == "CE" else "📉"
    risk_e = {"RISK_ON": "🚀", "RISK_OFF": "🛑", "NEUTRAL": "🟡"}.get(global_state, "📊")
    vol_e = {"LOW": "平静", "NORMAL": "温和", "HIGH": "动荡"}.get(volatility, "📊")
    lots = lots or (qty // 30)

    return (
        f"{side_e} <b>TRADE OPEN — {side}</b>  ✅\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"💵 Entry Price   : <b>{entry_price:.1f}</b>\n"
        f"📦 Quantity      : {qty}  ({lots} lot{'s' if lots != 1 else ''})\n"
        f"\n"
        f"🛑 Stop Loss     : {stop_loss:.1f}\n"
        f"\n"
        f"🧠 ML Confidence  : {ml_confidence:.0%}\n"
        f"{risk_e} Global State   : <b>{global_state}</b>\n"
        f"{vol_e} Volatility     : <b>{volatility}</b>\n"
        f"\n"
        f"📊 Final Score    : <b>{final_score:.3f}</b>\n"
        f"门槛 Threshold    : <b>{threshold:.3f}</b>\n"
        f"\n"
        f"✅ Decision: <b>ALLOW</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )


def format_decision_with_scores(
    symbol: str,
    side: str,
    ml_confidence: float,
    orb_signal: float,
    global_state: float,
    volatility_factor: float,
    final_score: float,
    threshold: float,
    decision: str,
) -> str:
    """
    Detailed decision breakdown showing all score components.
    """
    side_e = "📈" if side.upper() == "CE" else "📉"
    risk_e = "🚀" if global_state > 0 else ("🛑" if global_state < 0 else "🟡")

    return (
        f"{side_e} <b>DECISION BREAKDOWN — {side}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📌 <b>{symbol}</b>\n"
        f"\n"
        f"<b>SCORE BREAKDOWN</b>\n"
        f"ML Confidence ({ml_confidence:.2f})  × 0.50  = {ml_confidence * 0.5:.3f}\n"
        f"ORB Signal    ({orb_signal:.2f})    × 0.20  = {orb_signal * 0.2:.3f}\n"
        f"Global State  ({global_state:+.1f})   × 0.20  = {global_state * 0.2:.3f}\n"
        f"Volatility    ({volatility_factor:.1f})   × 0.10  = {(volatility_factor - 1) * 0.1:.3f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>FINAL SCORE</b>: <b>{final_score:.3f}</b>\n"
        f"Threshold: <b>{threshold:.3f}</b>\n"
        f"Decision: <b>{decision}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"
    )
