# engine/analytics/slippage.py
#
# F5 — Slippage Analytics.
# One row per completed trade: signal price vs fill price at entry AND exit.
# master_runner stores signal LTPs in the position dict; this module writes
# a single complete record after exit.  No live state is mutated.

import os
import csv
import threading
from datetime import date, datetime

_SLIP_DIR  = os.path.join("data", "analytics")
_SLIP_PATH = os.path.join(_SLIP_DIR, "slippage_log.csv")
_lock      = threading.Lock()

_COLUMNS = [
    "date", "time", "symbol", "side",
    # Entry
    "entry_signal_price", "entry_fill_price", "entry_slip_pts",
    # Exit
    "exit_signal_price", "exit_fill_price", "exit_slip_pts",
    # Round-trip summary
    "rt_slip_pts", "qty", "slip_cost_rs",
]


def _ensure() -> None:
    os.makedirs(_SLIP_DIR, exist_ok=True)
    if not os.path.exists(_SLIP_PATH) or os.path.getsize(_SLIP_PATH) == 0:
        with open(_SLIP_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_COLUMNS)


def log_slip(
    symbol: str,
    side: str,
    entry_signal: float,
    entry_fill: float,
    exit_signal: float,
    exit_fill: float,
    qty: int,
    entry_time: datetime | None = None,
) -> None:
    """
    Append one completed-trade slippage record.
    Call once per trade, after exit price is known.
    """
    _ensure()
    ts          = entry_time or datetime.now()
    entry_slip  = round(entry_fill  - entry_signal, 3)
    exit_slip   = round(exit_fill   - exit_signal,  3)
    rt_slip     = round(entry_slip  + exit_slip,     3)
    slip_cost   = round(rt_slip * qty, 2)

    with _lock:
        with open(_SLIP_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                ts.date().isoformat(),
                ts.strftime("%H:%M:%S"),
                symbol, side,
                round(entry_signal, 2), round(entry_fill, 2), entry_slip,
                round(exit_signal, 2),  round(exit_fill, 2),  exit_slip,
                rt_slip, qty, slip_cost,
            ])


def slippage_stats(n_trades: int | None = None) -> str:
    """Return Telegram-formatted slippage report."""
    if not os.path.exists(_SLIP_PATH):
        return "📊 No slippage data recorded yet."

    rows = []
    try:
        with open(_SLIP_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        "entry_slip": float(row["entry_slip_pts"] or 0),
                        "exit_slip":  float(row["exit_slip_pts"]  or 0),
                        "rt_slip":    float(row["rt_slip_pts"]     or 0),
                        "slip_cost":  float(row["slip_cost_rs"]    or 0),
                        "qty":        int(float(row["qty"] or 0)),
                        "side":       row["side"],
                        "date":       row["date"],
                    })
                except Exception:
                    pass
    except Exception:
        return "📊 Could not read slippage log."

    if n_trades:
        rows = rows[-n_trades:]
    if not rows:
        return "📊 No completed slippage records."

    n          = len(rows)
    entry_avg  = sum(r["entry_slip"] for r in rows) / n
    exit_avg   = sum(r["exit_slip"]  for r in rows) / n
    rt_avg     = sum(r["rt_slip"]    for r in rows) / n
    worst_rt   = max(r["rt_slip"]    for r in rows)
    best_rt    = min(r["rt_slip"]    for r in rows)
    total_cost = sum(r["slip_cost"]  for r in rows)

    # Per-side breakdown
    ce_rows = [r for r in rows if r["side"] == "CE"]
    pe_rows = [r for r in rows if r["side"] == "PE"]

    def _side_avg(rlist, key):
        return sum(r[key] for r in rlist) / len(rlist) if rlist else 0.0

    scope = f"last {n_trades}" if n_trades else "all-time"
    lines = [
        f"📊 <b>SLIPPAGE ANALYTICS</b>  ({scope}, {n} trades)",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        f"Avg Entry Slip   {entry_avg:+.3f} pts",
        f"Avg Exit Slip    {exit_avg:+.3f} pts",
        f"Avg RT Slip      {rt_avg:+.3f} pts",
        f"Best RT Slip     {best_rt:+.3f} pts",
        f"Worst RT Slip    {worst_rt:+.3f} pts",
        f"Total Slip Cost  -₹{abs(total_cost):,.0f}",
        "",
        "<b>BY SIDE</b>",
        f"CE  entry {_side_avg(ce_rows,'entry_slip'):+.3f}  "
        f"rt {_side_avg(ce_rows,'rt_slip'):+.3f}  (n={len(ce_rows)})",
        f"PE  entry {_side_avg(pe_rows,'entry_slip'):+.3f}  "
        f"rt {_side_avg(pe_rows,'rt_slip'):+.3f}  (n={len(pe_rows)})",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
    ]
    return "\n".join(lines)
