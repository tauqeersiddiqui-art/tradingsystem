# utils/obsidian_logger.py
# Obsidian "Second Brain" logging for trading system.
# Appends trade records, daily summaries, and pattern notes to markdown files.
# Zero dependencies on trading logic — pure output layer.

import os
import threading
from datetime import datetime, date
from typing import Optional, Dict, Any

# ─── Configuration ──────────────────────────────────────────────────────
VAULT_ROOT = os.path.join("trading_brain")
DAILY_DIR  = os.path.join(VAULT_ROOT, "daily")
TRADES_DIR = os.path.join(VAULT_ROOT, "trades")
PATTERNS_DIR = os.path.join(VAULT_ROOT, "patterns")
RULES_DIR = os.path.join(VAULT_ROOT, "rules")

# Thread-safe write lock
_write_lock = threading.Lock()

# ─── Internal helpers ───────────────────────────────────────────────────

def _ensure_dirs() -> None:
    """Create vault directories if they don't exist."""
    for d in (DAILY_DIR, TRADES_DIR, PATTERNS_DIR, RULES_DIR):
        os.makedirs(d, exist_ok=True)


def _safe_append(filepath: str, content: str) -> bool:
    """
    Append content to file with UTF-8 encoding and thread safety.
    Returns True on success, False on failure (never raises).
    """
    try:
        with _write_lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(content)
        return True
    except Exception:
        # Silent fail — logging layer must never crash trading loop
        return False


def _trade_filepath(trade_date: date) -> str:
    """Get the trades markdown file path for a given date."""
    return os.path.join(TRADES_DIR, f"{trade_date.isoformat()}.md")


def _daily_filepath(trade_date: date) -> str:
    """Get the daily summary markdown file path for a given date."""
    return os.path.join(DAILY_DIR, f"{trade_date.isoformat()}.md")


def _patterns_filepath() -> str:
    """Get the common failures patterns file path."""
    return os.path.join(PATTERNS_DIR, "common_failures.md")


def _format_timestamp(ts: datetime) -> str:
    """Format datetime as HH:MM:SS for markdown."""
    return ts.strftime("%H:%M:%S")


def _format_pnl(pnl: float) -> str:
    """Format PnL with sign and ₹ symbol."""
    sign = "+" if pnl >= 0 else ""
    return f"₹{sign}{pnl:,.2f}"


# ─── Public API ─────────────────────────────────────────────────────────

def log_trade(
    *,
    entry_price: float,
    exit_price: float,
    pnl: float,
    mfe: float,
    ml_score: float,
    strategy: str,
    side: str,
    symbol: str,
    entry_ts: Optional[datetime] = None,
    exit_ts: Optional[datetime] = None,
    exit_reason: str = "",
    held_seconds: float = 0,
    trade_date: Optional[date] = None,
) -> bool:
    """
    Append a closed trade record to trading_brain/trades/YYYY-MM-DD.md

    Called when a trade is CLOSED (exit executed).
    """
    _ensure_dirs()

    d = trade_date or date.today()
    filepath = _trade_filepath(d)

    entry_time = _format_timestamp(entry_ts) if entry_ts else "—"
    exit_time  = _format_timestamp(exit_ts) if exit_ts else "—"
    held_str   = f"{int(held_seconds)//60}m {int(held_seconds)%60:02d}s" if held_seconds else "—"

    content = (
        f"\n### Trade — {entry_time} -> {exit_time} ({held_str})\n"
        f"- **Symbol:** {symbol}\n"
        f"- **Side:** {side}\n"
        f"- **Entry:** {entry_price:.2f}\n"
        f"- **Exit:** {exit_price:.2f}\n"
        f"- **PnL:** {_format_pnl(pnl)}\n"
        f"- **MFE:** ₹{mfe:,.2f}\n"
        f"- **ML Confidence:** {ml_score:.2%}\n"
        f"- **Strategy:** {strategy}\n"
        f"- **Exit Reason:** {exit_reason or '—'}\n\n"
        f"**Mistake:**\n\n"
        f"**Improvement:**\n\n"
        f"---\n"
    )

    ok = _safe_append(filepath, content)
    if ok:
        print(f"[OBSIDIAN] Trade logged -> {filepath}")
    return ok


def log_daily_summary(
    *,
    total_trades: int,
    net_pnl: float,
    win_rate: float,
    avg_mfe: float,
    gross_profit: float = 0.0,
    gross_loss: float = 0.0,
    max_drawdown: float = 0.0,
    ce_trades: int = 0,
    ce_wr: float = 0.0,
    pe_trades: int = 0,
    pe_wr: float = 0.0,
    observations: str = "",
    action_next_day: str = "",
    trade_date: Optional[date] = None,
) -> bool:
    """
    Create/update trading_brain/daily/YYYY-MM-DD.md with end-of-day summary.

    Called at EOD (15:30 trigger).
    """
    _ensure_dirs()

    d = trade_date or date.today()
    filepath = _daily_filepath(d)

    # Check if file exists to decide whether to write header
    file_exists = os.path.exists(filepath)

    content = ""
    if not file_exists:
        content += f"# Daily Summary — {d.isoformat()}\n\n"

    content += (
        f"## Summary\n"
        f"- **Total Trades:** {total_trades}\n"
        f"- **Net PnL:** {_format_pnl(net_pnl)}\n"
        f"- **Gross Profit:** ₹{gross_profit:,.2f}\n"
        f"- **Gross Loss:** -₹{abs(gross_loss):,.2f}\n"
        f"- **Win Rate:** {win_rate:.1f}%\n"
        f"- **Avg MFE:** ₹{avg_mfe:,.2f}\n"
        f"- **Max Drawdown:** -₹{abs(max_drawdown):,.2f}\n"
        f"- **CE Trades:** {ce_trades} (WR: {ce_wr:.1f}%)\n"
        f"- **PE Trades:** {pe_trades} (WR: {pe_wr:.1f}%)\n\n"
    )

    if observations:
        content += f"## Observations\n{observations}\n\n"
    else:
        content += "## Observations\n-\n\n"

    if action_next_day:
        content += f"## Action for Next Day\n{action_next_day}\n\n"
    else:
        content += "## Action for Next Day\n-\n\n"

    content += "---\n\n"

    ok = _safe_append(filepath, content)
    if ok:
        print(f"[OBSIDIAN] Daily summary updated -> {filepath}")
    return ok


def log_pattern(
    *,
    pattern_name: str,
    details: str,
    trade_date: Optional[date] = None,
) -> bool:
    """
    Append a detected pattern to trading_brain/patterns/common_failures.md

    Called when a pattern condition is detected (e.g., high MFE low capture,
    repeated losing trades on same side).
    """
    _ensure_dirs()

    filepath = _patterns_filepath()
    d = trade_date or date.today()

    file_exists = os.path.exists(filepath)

    content = ""
    if not file_exists:
        content += "# Common Failure Patterns\n\n"

    content += (
        f"## {pattern_name}\n"
        f"**Observed on:** {d.isoformat()}\n\n"
        f"{details}\n\n"
        f"---\n\n"
    )

    ok = _safe_append(filepath, content)
    if ok:
        print(f"[OBSIDIAN] Pattern logged -> {filepath}")
    return ok


def check_and_log_patterns(
    *,
    trades_today: list,
    trade_date: Optional[date] = None,
) -> None:
    """
    Analyze today's trades for common patterns and log them.
    Called at EOD along with daily summary.

    Patterns detected:
    - High MFE Low Capture: Avg MFE > 50 but net PnL negative/low
    - Repeated Losing Side: Same side (CE/PE) has >2 consecutive losses
    - Immediate Reversal: >30% trades with MFE <= 0
    - Stop Too Tight: Many stops hit with MFE 1-4 pts
    """
    if not trades_today:
        return

    d = trade_date or date.today()

    # Compute metrics
    total = len(trades_today)
    mfe_vals = [t.get("mfe_rs", 0) for t in trades_today if t.get("mfe_rs")]
    avg_mfe = sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0
    net_pnl = sum(t.get("pnl", t.get("realized_pnl", 0)) for t in trades_today)

    # Pattern 1: High MFE Low Capture
    if avg_mfe > 50 and net_pnl <= 0:
        log_pattern(
            pattern_name="High MFE Low Capture",
            details=(
                f"Average MFE: ₹{avg_mfe:,.0f} but Net PnL: {_format_pnl(net_pnl)}. "
                f"Trades reached good profits but gave them back. "
                f"Review trailing stop logic or exit timing."
            ),
            trade_date=d,
        )

    # Pattern 2: Immediate Reversal (MFE <= 0)
    zero_mfe_count = sum(1 for m in mfe_vals if m <= 0)
    if total > 0 and (zero_mfe_count / total) > 0.3:
        log_pattern(
            pattern_name="Immediate Adverse Move",
            details=(
                f"{zero_mfe_count}/{total} trades ({zero_mfe_count/total*100:.0f}%) "
                f"never went positive (MFE ≤ 0). "
                f"Entry timing or signal quality issue — check ML threshold or ORB confirmation."
            ),
            trade_date=d,
        )

    # Pattern 3: Repeated Losing Side
    # Use "pnl" key (from CSV) or fall back to "realized_pnl"
    ce_losses = [t for t in trades_today if t.get("side") == "CE" and t.get("pnl", t.get("realized_pnl", 0)) <= 0]
    pe_losses = [t for t in trades_today if t.get("side") == "PE" and t.get("pnl", t.get("realized_pnl", 0)) <= 0]

    # Check for consecutive losses on same side
    def max_consecutive_losses(trades, side):
        max_streak = 0
        current = 0
        for t in trades:
            pnl_val = t.get("pnl", t.get("realized_pnl", 0))
            if t.get("side") == side and pnl_val <= 0:
                current += 1
                max_streak = max(max_streak, current)
            elif t.get("side") == side:
                current = 0
        return max_streak

    ce_streak = max_consecutive_losses(trades_today, "CE")
    pe_streak = max_consecutive_losses(trades_today, "PE")

    if ce_streak >= 3:
        log_pattern(
            pattern_name="Repeated CE Losses",
            details=(
                f"{ce_streak} consecutive losing CE trades. "
                f"Consider: CE ML threshold too low, HTF confirmation missing, "
                f"or regime filter not catching chop."
            ),
            trade_date=d,
        )

    if pe_streak >= 3:
        log_pattern(
            pattern_name="Repeated PE Losses",
            details=(
                f"{pe_streak} consecutive losing PE trades. "
                f"Consider: PE ML threshold too low, HTF confirmation missing, "
                f"or regime filter not catching chop."
            ),
            trade_date=d,
        )

    # Pattern 4: Stop Too Tight (MFE 1-4 pts then stop)
    tight_stops = [t for t in trades_today
                   if t.get("exit_reason") in ("STOP", "Stop Loss")
                   and 0 < t.get("mfe_rs", 0) <= 4 * t.get("qty", 1)]
    if len(tight_stops) >= 3:
        log_pattern(
            pattern_name="Stop Too Tight",
            details=(
                f"{len(tight_stops)} trades peaked 1-4 pts then hit stop. "
                f"Initial stop or trail gap may be too tight for current volatility."
            ),
            trade_date=d,
        )


def initialize_vault() -> None:
    """Create the vault structure and initial index files."""
    _ensure_dirs()

    # Create a vault index/README
    index_path = os.path.join(VAULT_ROOT, "README.md")
    if not os.path.exists(index_path):
        content = (
            "# Trading Brain — Obsidian Vault\n\n"
            "Auto-generated second brain for trading system.\n\n"
            "## Structure\n"
            "- `daily/` — End-of-day summaries\n"
            "- `trades/` — Individual trade records (one file per day)\n"
            "- `patterns/` — Recurring failure patterns\n"
            "- `rules/` — Extracted trading rules (manual)\n\n"
            "---\n"
            f"*Vault initialized: {datetime.now().isoformat()}*\n"
        )
        _safe_append(index_path, content)

    # Create patterns index if missing
    patterns_index = os.path.join(PATTERNS_DIR, "README.md")
    if not os.path.exists(patterns_index):
        content = (
            "# Patterns Index\n\n"
            "Auto-detected recurring failure modes.\n\n"
            "---\n"
            f"*Created: {datetime.now().isoformat()}*\n"
        )
        _safe_append(patterns_index, content)

    # Create rules index if missing
    rules_index = os.path.join(RULES_DIR, "README.md")
    if not os.path.exists(rules_index):
        content = (
            "# Trading Rules\n\n"
            "Manually curated rules extracted from pattern analysis.\n\n"
            "## Template\n"
            "### Rule Name\n"
            "- **Condition:** When X happens\n"
            "- **Action:** Do Y\n"
            "- **Evidence:** Link to pattern/date\n\n"
            "---\n"
            f"*Created: {datetime.now().isoformat()}*\n"
        )
        _safe_append(rules_index, content)

    print(f"[OBSIDIAN] Vault initialized at {VAULT_ROOT}")