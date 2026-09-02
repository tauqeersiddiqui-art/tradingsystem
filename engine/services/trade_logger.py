# engine/services/trade_logger.py
# Persistent trade log — written after every exit, never lost on restart.
# CSV: data/trades/trade_log_YYYY_WNN.csv  (weekly rolling file)

import os
import csv
import threading
from datetime import datetime, date

from engine.execution.cost_model import net_pnl   # R6: authoritative net PnL

_TRADE_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trades")
_lock       = threading.Lock()
_trade_seq  = 0   # increments each session; will re-read on load

# ── Phase 4: post-trade loss classification ───────────────────────────
# Thresholds mirror the entry-quality filter module.
MOVE_PCT_GOOD = 0.003   # move_pct above this at entry => chased / LATE_ENTRY
WICK_RATIO_MAX = 0.6    # wick_ratio above this at entry => exhaustion / REVERSAL


def classify_loss(pnl: float, entry_quality: dict | None) -> str:
    """
    Classify a losing trade using entry-time quality metrics.

    Returns:
        ""           — winner (pnl >= 0)
        "LATE_ENTRY"  — entry chased a large move (move_pct > MOVE_PCT_GOOD)
        "REVERSAL"    — entry candle was wick-heavy (wick_ratio > WICK_RATIO_MAX)
        "UNKNOWN"     — loser with missing/absent entry_quality (restored
                        or pre-deployment positions)
    """
    if pnl >= 0:
        return ""
    if entry_quality:
        move_pct = entry_quality.get("move_pct")
        if move_pct is not None and move_pct > MOVE_PCT_GOOD:
            return "LATE_ENTRY"
        wick_ratio = entry_quality.get("wick_ratio")
        if wick_ratio is not None and wick_ratio > WICK_RATIO_MAX:
            return "REVERSAL"
    return "UNKNOWN"


def _week_path(dt: date | None = None) -> str:
    if dt is None:
        dt = date.today()
    week = dt.isocalendar()[1]
    os.makedirs(_TRADE_DIR, exist_ok=True)
    return os.path.join(_TRADE_DIR, f"trade_log_{dt.year}_W{week:02d}.csv")


_COLUMNS = [
    "trade_id", "date", "entry_time", "exit_time",
    "symbol", "side", "regime",
    "entry_price", "exit_price", "quantity", "pnl",
    "R_multiple", "ml_prob", "entry_score",
    "MFE", "MAE",
    "holding_seconds", "stop_loss", "target",
    "stop_distance_pts", "peak_pnl",
    "entry_reason", "exit_reason",
    "signal_ts", "order_submit_ts", "fill_ts",
    "signal_price", "fill_price",
    "first_bid", "first_ask", "first_ltp",
    "spread", "slippage_pts",
    "signal_to_order_latency_ms", "order_to_fill_latency_ms",
    "ce_threshold", "pe_threshold",
]


def _ensure_header(path: str):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(_COLUMNS)


def log_trade(
    entry_order:  dict,
    exit_price:   float,
    exit_reason:  str,
    position:     dict,
    entry_time:   datetime,
    exit_time:    datetime | None = None,
    ce_threshold: float = 0.62,
    pe_threshold: float = 0.62,
):
    """
    Call after every trade exit.
    entry_order  — dict returned by executor.execute_entry (symbol, price, qty, side)
    position     — the position dict from master_runner (entry, stop_loss, target, ml_prob, etc.)
    """
    global _trade_seq
    if exit_time is None:
        exit_time = datetime.now()

    entry_price  = position.get("entry", entry_order.get("price", 0))
    symbol       = position.get("symbol", entry_order.get("symbol", ""))
    side         = position.get("side", "")
    qty          = position.get("qty", entry_order.get("qty", 0))
    ml_prob      = position.get("ml_prob", 0.0)
    stop_loss    = position.get("stop_loss", 0.0)
    target       = position.get("target", 0.0)
    regime       = position.get("regime", "UNKNOWN")
    entry_reason = position.get("reason", "")
    max_pnl      = position.get("max_pnl", 0.0)
    peak_pnl     = max_pnl   # highest P&L reached during trade
    signal_ts    = position.get("_exec_signal_ts", "")
    submit_ts    = position.get("_exec_order_submit_ts", "")
    fill_ts      = position.get("_exec_fill_ts", "")
    signal_price = position.get("_exec_signal_price", 0.0)
    fill_price   = position.get("entry", entry_order.get("price", 0))
    first_bid    = position.get("_exec_first_bid", "")
    first_ask    = position.get("_exec_first_ask", "")
    first_ltp    = position.get("_exec_first_ltp", "")
    spread       = position.get("_exec_spread", "")
    slippage_pts = position.get("_exec_slippage_pts", "")
    signal_to_order_ms = position.get("_exec_signal_to_order_ms", "")
    order_to_fill_ms   = position.get("_exec_order_to_fill_ms", "")

    pnl          = net_pnl((exit_price - entry_price) * qty, qty)   # R6: NET PnL
    holding_s    = (exit_time - entry_time).total_seconds() if entry_time else 0
    # Use initial_stop_loss if stored (set at entry), else fall back to current stop_loss
    initial_stop = position.get("initial_stop_loss", stop_loss)
    stop_pts     = entry_price - initial_stop
    risk_rs      = stop_pts * qty
    R_multiple   = (pnl / risk_rs) if risk_rs != 0 else 0.0

    # MFE/MAE in currency (best / worst during hold)
    MFE = peak_pnl                        # best we ever were
    MAE = min(0.0, pnl - peak_pnl)       # worst drawdown from peak (approx)

    # Simple entry score: ml_prob × 100
    entry_score = round(ml_prob * 100, 1)

    with _lock:
        _trade_seq += 1
        seq = _trade_seq
        path = _week_path(entry_time.date() if entry_time else None)
        _ensure_header(path)
        with open(path, "a", newline="") as f:
            csv.writer(f).writerow([
                seq,
                (entry_time.date() if entry_time else date.today()).isoformat(),
                entry_time.strftime("%H:%M:%S") if entry_time else "",
                exit_time.strftime("%H:%M:%S"),
                symbol, side, regime,
                round(entry_price, 2), round(exit_price, 2), qty,
                round(pnl, 2),
                round(R_multiple, 3),
                round(ml_prob, 4),
                entry_score,
                round(MFE, 2), round(MAE, 2),
                round(holding_s, 1),
                round(stop_loss, 2), round(target, 2),
                round(stop_pts, 2),
                round(peak_pnl, 2),
                entry_reason, exit_reason,
                signal_ts, submit_ts, fill_ts,
                round(signal_price, 2) if signal_price else 0.0,
                round(fill_price, 2) if fill_price else 0.0,
                round(first_bid, 2) if isinstance(first_bid, (int, float)) else first_bid,
                round(first_ask, 2) if isinstance(first_ask, (int, float)) else first_ask,
                round(first_ltp, 2) if isinstance(first_ltp, (int, float)) else first_ltp,
                round(spread, 2) if isinstance(spread, (int, float)) else spread,
                round(slippage_pts, 2) if isinstance(slippage_pts, (int, float)) else slippage_pts,
                int(signal_to_order_ms) if signal_to_order_ms != "" else "",
                int(order_to_fill_ms) if order_to_fill_ms != "" else "",
                round(ce_threshold, 3), round(pe_threshold, 3),
            ])

    return pnl


def today_summary(trade_date: date | None = None) -> dict:
    """Read trades for the requested day from CSV and return summary stats."""
    if trade_date is None:
        trade_date = date.today()

    path = _week_path(trade_date)
    if not os.path.exists(path):
        return {"trades": 0, "pnl": 0, "wins": 0, "losses": 0,
                "avg_win": 0, "avg_loss": 0, "best": 0, "worst": 0}

    trades = []
    try:
        target_date_str = trade_date.isoformat()
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("date") == target_date_str:
                    try:
                        trades.append(float(row["pnl"]))
                    except (ValueError, KeyError):
                        pass
    except Exception:
        return {"trades": 0, "pnl": 0}

    if not trades:
        return {"trades": 0, "pnl": 0, "wins": 0, "losses": 0,
                "avg_win": 0, "avg_loss": 0, "best": 0, "worst": 0}

    wins   = [p for p in trades if p > 0]
    losses = [p for p in trades if p < 0]
    return {
        "trades":   len(trades),
        "pnl":      round(sum(trades), 2),
        "wins":     len(wins),
        "losses":   len(losses),
        "avg_win":  round(sum(wins)   / len(wins),   2) if wins   else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "best":     round(max(trades), 2),
        "worst":    round(min(trades), 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
    }


def get_trades_for_day(trade_date: date | None = None) -> list:
    """
    Read full trade details for a given day from CSV.
    Returns list of dicts with: pnl, mfe_rs (MFE), side, exit_reason, qty, entry_price, exit_price, ml_prob, regime.
    """
    if trade_date is None:
        trade_date = date.today()

    path = _week_path(trade_date)
    if not os.path.exists(path):
        return []

    trades = []
    target_date_str = trade_date.isoformat()
    try:
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("date") != target_date_str:
                    continue
                try:
                    trades.append({
                        "pnl": float(row.get("pnl", 0)),
                        "mfe_rs": float(row.get("MFE", 0)),
                        "side": row.get("side", ""),
                        "exit_reason": row.get("exit_reason", ""),
                        "qty": int(row.get("quantity", 1)),
                        "entry_price": float(row.get("entry_price", 0)),
                        "exit_price": float(row.get("exit_price", 0)),
                        "ml_prob": float(row.get("ml_prob", 0)),
                        "regime": row.get("regime", "UNKNOWN"),
                    })
                except (ValueError, KeyError):
                    continue
    except Exception:
        return []

    return trades
