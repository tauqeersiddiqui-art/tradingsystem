# engine/services/dashboard.py
# Two rich Telegram dashboards:
#   render_engine(ctx, market_state)      — AI engine, ML bias, decision
#   render_market(ctx, market_state, pos) — live position, market internals

import html as _html
from datetime import datetime


def _bar(prob: float, width: int = 10) -> str:
    filled = max(0, min(width, round(prob * width)))
    return "█" * filled + "░" * (width - filled)


def _bias_label(adj: float, threshold: float) -> str:
    if adj >= threshold + 0.08:
        return "STRONG"
    if adj >= threshold:
        return "above thr"
    if adj >= threshold - 0.06:
        return "near thr"
    return "low"


def _dir_arrow(direction: int) -> str:
    return {1: "BULL ↑", -1: "BEAR ↓", 0: "UNCLEAR --"}.get(direction, "--")


def _pnl_color(pnl: float) -> str:
    return f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"


def _win_rate(positions: list) -> float:
    if not positions:
        return 0.0
    wins = sum(1 for p in positions if p > 0)
    return wins / len(positions)


def _profit_factor(positions: list) -> float:
    wins  = sum(p for p in positions if p > 0)
    loss  = sum(abs(p) for p in positions if p < 0)
    return (wins / loss) if loss > 0 else float("inf")


def _expectancy(positions: list) -> float:
    if not positions:
        return 0.0
    wr   = _win_rate(positions)
    wins = [p for p in positions if p > 0]
    loss = [p for p in positions if p < 0]
    avg_w = (sum(wins) / len(wins)) if wins else 0
    avg_l = (sum(loss) / len(loss)) if loss else 0
    return wr * avg_w + (1 - wr) * avg_l


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
    change_pct = (change / first * 100) if first else 0.0
    start_ts = chart.get("start", "--")
    end_ts = chart.get("end", "--")
    moves = chart.get("moves") or {}

    def _fmt_move(value):
        return "--" if value is None else f"{float(value):+.1f}"

    return (
        "\n<b>BANKNIFTY LIVE CHART</b>\n"
        f"<code>{line}</code>\n"
        f"<code>{start_ts} {first:,.1f} -> {end_ts} {last:,.1f} "
        f"({change:+.1f}, {change_pct:+.2f}%)</code>\n"
        f"<code>H {high:,.1f}  L {low:,.1f}  "
        f"5m {_fmt_move(moves.get('5m'))}  "
        f"15m {_fmt_move(moves.get('15m'))}  "
        f"30m {_fmt_move(moves.get('30m'))}</code>\n"
    )


# ─────────────────────────────────────────────────────────────────────
# DASHBOARD 1  — AI ENGINE STATUS
# ─────────────────────────────────────────────────────────────────────

def render_engine(ctx, market_state: dict, ltp: float = 0.0) -> str:
    ms       = market_state or {}
    now_str  = datetime.now().strftime("%H:%M:%S")
    session  = ms.get("session", "")
    dir_bias = ms.get("direction_bias", 0)

    # Technicals
    ema20  = ms.get("ema20", 0.0)
    ema50  = ms.get("ema50", 0.0)
    ema_dir = ms.get("ema_direction", "--")
    rsi    = ms.get("rsi_1m", 50.0)
    adx    = ms.get("adx", 0.0)
    st_dir = ms.get("supertrend_dir", 0)
    st_str = "UP ↑" if st_dir > 0 else ("DOWN ↓" if st_dir < 0 else "--")
    vwap   = ms.get("vwap", 0.0)
    pvwap  = ms.get("price_vs_vwap", 0.0) * 100   # pct

    # ML
    ce_adj = ms.get("ce_adj", 0.0)
    pe_adj = ms.get("pe_adj", 0.0)
    ce_raw = ms.get("ce_prob", 0.0)
    pe_raw = ms.get("pe_prob", 0.0)
    thr     = ms.get("ml_threshold", 0.65)   # PE threshold
    ce_thr  = ms.get("ce_threshold", 0.70)   # CE threshold (higher)
    ce_lbl = _bias_label(ce_adj, ce_thr)
    pe_lbl = _bias_label(pe_adj, thr)

    # Phase 5.5 optional intelligence filter
    phase55 = ms.get("phase55") or {}
    phase55_status = "Enabled" if phase55.get("enabled") else "Disabled"
    phase55_filter = _html.escape(str(phase55.get("filter_used") or "none"))
    phase55_blocked = "YES" if phase55.get("trade_blocked") else "NO"
    phase55_reason = _html.escape(str(phase55.get("reason") or "--"))

    # Scoring
    ml_pct  = ms.get("ml_percentile", 0)
    score   = ms.get("ml_score", 0.0)
    score_r = ms.get("score_required", 40.0)
    score_g = round(score - score_r, 1)
    score_ok = score >= score_r

    # Block reason / decision — escape raw reason so <CE<0.62> etc. don't break HTML
    block     = _html.escape(ms.get("block_reason", "WARMING_UP"))
    block_raw = ms.get("block_reason", "WARMING_UP")
    if block_raw.startswith("SIGNAL_FIRE"):
        decision_line = f"\U0001f7e2 FIRING — {block.split('(')[-1].rstrip(')')}"
    else:
        decision_line = f"\U0001f534 WAITING — {block}"

    # Stats
    positions    = getattr(ctx, "positions", [])
    trades_today = getattr(ctx, "trades_today", 0)
    pnl          = getattr(ctx, "pnl", 0.0)
    wr           = _win_rate(positions) * 100
    pf           = _profit_factor(positions)
    exp          = _expectancy(positions)
    wins         = sum(1 for p in positions if p > 0)
    losses       = len(positions) - wins
    avg_w        = sum(p for p in positions if p > 0) / max(wins, 1)
    avg_l        = sum(p for p in positions if p < 0) / max(losses, 1)
    pf_str       = f"{pf:.2f}" if pf != float("inf") else "inf"

    ltp_str = f"{ltp:,.1f}" if ltp else "--"

    return (
        f"<b>AGENTIC TRADER — AI ENGINE</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"\U0001f551 {now_str}  |  BANKNIFTY {ltp_str}  |  {_dir_arrow(dir_bias)}\n"
        f"Session: {session}\n"
        f"\n"
        f"<b>TECHNICALS</b>\n"
        f"EMA20 {ema20:,.0f}  EMA50 {ema50:,.0f}  [{ema_dir}]\n"
        f"RSI(14): {rsi:.1f}   ADX: {adx:.1f}\n"
        f"Supertrend: {st_str}   VWAP: {vwap:,.0f} ({pvwap:+.2f}%)\n"
        f"\n"
        f"<b>ML BIAS</b>\n"
        f"CE  {ce_adj:.2f} {_bar(ce_adj)}  {ce_lbl}  (raw {ce_raw:.2f})  thr={ce_thr:.2f}\n"
        f"PE  {pe_adj:.2f} {_bar(pe_adj)}  {pe_lbl}  (raw {pe_raw:.2f})  thr={thr:.2f}\n"
        f"\n"
        f"<b>PHASE55</b>\n"
        f"Enabled/Disabled: {phase55_status}\n"
        f"Filter Used: {phase55_filter}\n"
        f"Trade Blocked: {phase55_blocked}\n"
        f"Reason: {phase55_reason}\n"
        f"\n"
        f"<b>SCORING</b>\n"
        f"Score:  {score:.1f}  {'[PASS]' if score_ok else '[FAIL]'}  (req {score_r:.0f})\n"
        f"Gap:    {score_g:+.1f}   Percentile: {ml_pct}%\n"
        f"\n"
        f"<b>DECISION</b>\n"
        f"{decision_line}\n"
        f"\n"
        f"<b>TODAY  ({trades_today} trades)</b>\n"
        f"P&amp;L: {_pnl_color(pnl)}   W/L: {wins}/{losses}   WR: {wr:.0f}%\n"
        f"Avg W: {_pnl_color(avg_w)}   Avg L: {_pnl_color(avg_l)}\n"
        f"PF: {pf_str}   Expectancy: {_pnl_color(exp)}/trade"
    )


# ─────────────────────────────────────────────────────────────────────
# DASHBOARD 2  — LIVE MARKET STATUS
# ─────────────────────────────────────────────────────────────────────

def render_market(ctx, market_state: dict, position: dict | None,
                  ltp: float = 0.0) -> str:
    ms  = market_state or {}
    now = datetime.now().strftime("%H:%M")

    orb_high = ms.get("orb_high")
    orb_low  = ms.get("orb_low")
    orb_done = ms.get("orb_done", False)

    if orb_high and orb_low:
        orb_str = f"{orb_high:,.0f}H / {orb_low:,.0f}L"
        orb_str += " (locked)" if orb_done else " (building)"
    else:
        orb_str = "building..."

    vwap = ms.get("vwap", 0.0)

    if position is None:
        pos_block = "[NO OPEN POSITION]"
    else:
        entry     = position.get("entry", 0.0)
        side      = position.get("side", "?")
        symbol    = position.get("symbol", "?")
        qty       = position.get("qty", 0)
        sl        = position.get("stop_loss", 0.0)
        target    = position.get("target", 0.0)
        ml_prob   = position.get("ml_prob", 0.0)
        max_pnl   = position.get("max_pnl", 0.0)
        entry_ts  = position.get("entry_ts")
        held_str  = ""
        if entry_ts:
            held_s   = (datetime.now() - entry_ts).total_seconds()
            held_str = f"{int(held_s // 60)}m {int(held_s % 60)}s"

        pnl_pts  = ltp - entry
        pnl_rs   = pnl_pts * qty
        peak_pts = max_pnl / max(qty, 1) if qty > 0 else 0

        # Trail lock description — mirrors profit_manager.py ladder
        if peak_pts >= 40:
            lock_pct = "80%"
        elif peak_pts >= 25:
            lock_pct = "65%"
        elif peak_pts >= 15:
            lock_pct = "40%"
        elif peak_pts >= 8:
            lock_pct = "+400/lot"
        elif peak_pts >= 4:
            lock_pct = "+200/lot"
        else:
            lock_pct = "--"

        pnl_sign  = "+" if pnl_rs >= 0 else ""
        peak_sign = "+" if max_pnl >= 0 else ""

        pos_block = (
            f"<b>{side} {symbol}</b>\n"
            f"Entry {entry:.1f}  →  LTP {ltp:.1f}  ({pnl_pts:+.1f} pts)\n"
            f"P&amp;L: {pnl_sign}₹{pnl_rs:,.0f}   Peak: {peak_sign}₹{max_pnl:,.0f}\n"
            f"Trail SL: {sl:.1f}  [{lock_pct} locked]\n"
            f"Target: {target:.1f}  |  ML: {ml_prob:.2f}\n"
            f"Qty: {qty}  |  Held: {held_str}"
        )

    import telegram.notifier as _tn
    engine_state = "PAUSED" if _tn.ENGINE_PAUSED else "ACTIVE"

    return (
        f"<b>📡 LIVE MARKET</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"{pos_block}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"ORB: {orb_str}\n"
        f"VWAP: {vwap:,.0f}  |  {now} IST\n"
        + _section_banknifty_chart(ms)
        + f"Engine: {engine_state}  |  /help for commands"
    )


# Backward-compat alias (master_runner.py calls render(ctx, market_data, decision))
def render(ctx, market_data: dict, decision: dict | None) -> str:
    features = (decision or {}).get("features", {})
    market_state = {
        "session":        "ACTIVE",
        "ema20":          features.get("ema20", 0),
        "ema50":          features.get("ema50", 0),
        "ema_direction":  "UP" if features.get("ema20", 0) > features.get("ema50", 0) else "DOWN",
        "rsi_1m":         features.get("rsi_1m", 50),
        "adx":            features.get("adx", 0),
        "supertrend_dir": features.get("supertrend_dir", 0),
        "vwap":           0,
        "price_vs_vwap":  features.get("price_vs_vwap", 0),
        "ce_adj":         (decision or {}).get("ml_prob", 0) if (decision or {}).get("side") == "CE" else 0,
        "pe_adj":         (decision or {}).get("ml_prob", 0) if (decision or {}).get("side") == "PE" else 0,
        "ce_prob":        0,
        "pe_prob":        0,
        "ml_threshold":   0.62,
        "block_reason":   "SIGNAL_FIRE" if decision else "NO_SIGNAL",
        "direction_bias": 1 if (decision or {}).get("side") == "CE" else (-1 if (decision or {}).get("side") == "PE" else 0),
    }
    candles = market_data.get("candles", [0])
    ltp     = float(candles[-1]) if candles else 0.0
    return render_engine(ctx, market_state, ltp)
