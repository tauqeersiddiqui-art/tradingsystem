# engine/analytics/performance.py
#
# Post-trade analytics suite — Features 2, 3, 4, 6, 7, 8.
# Reads ONLY from data/trades/trade_log_*.csv — never modifies trading state.
# All public functions return Telegram-ready HTML strings.

import os
import csv
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

logger = logging.getLogger("analytics.performance")

_TRADE_DIR = os.path.join("data", "trades")

# ── Drift-alert thresholds (can override at call site) ─────────────
DRIFT_DEFAULTS = {
    "win_rate":    45.0,    # % — alert if WR drops below
    "expectancy": -200.0,   # Rs — alert if expectancy below
    "pf":           0.8,    # — alert if profit factor below
    "capture":      0.05,   # ratio — alert if capture ratio below
}

_ML_BUCKETS = [
    (0.75, 0.80, "0.75–0.80"),
    (0.80, 0.85, "0.80–0.85"),
    (0.85, 0.90, "0.85–0.90"),
    (0.90, 1.01, "0.90+    "),
]


# ══════════════════════════════════════════════════════════════════════
# DATA ACCESS
# ══════════════════════════════════════════════════════════════════════

def _week_files() -> list:
    if not os.path.isdir(_TRADE_DIR):
        return []
    return sorted(
        [os.path.join(_TRADE_DIR, f) for f in os.listdir(_TRADE_DIR)
         if f.startswith("trade_log_") and f.endswith(".csv")],
        reverse=True,
    )


def read_trades(
    n: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list:
    """
    Return trade rows as list of dicts with numeric columns converted.
    Optional: filter by date range; limit to last N rows.
    """
    _NUM = (
        "entry_price", "exit_price", "pnl", "R_multiple",
        "ml_prob", "MFE", "MAE", "holding_seconds",
        "peak_pnl", "stop_distance_pts", "quantity",
        "stop_loss", "target",
    )
    rows = []
    for path in _week_files():
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if not row.get("date"):
                        continue
                    try:
                        rd = date.fromisoformat(row["date"])
                    except ValueError:
                        continue
                    if date_from and rd < date_from:
                        continue
                    if date_to and rd > date_to:
                        continue
                    for col in _NUM:
                        try:
                            row[col] = float(row.get(col) or 0)
                        except (ValueError, TypeError):
                            row[col] = 0.0
                    rows.append(row)
        except Exception:
            continue

    rows.sort(key=lambda r: (r["date"], r.get("entry_time", "")))
    return rows[-n:] if n is not None else rows


# ══════════════════════════════════════════════════════════════════════
# SHARED STAT KERNEL
# ══════════════════════════════════════════════════════════════════════

def _stats(rows: list) -> dict:
    if not rows:
        return {}
    pnl    = [r["pnl"]          for r in rows]
    mfe    = [r["MFE"]          for r in rows]
    mae    = [r["MAE"]          for r in rows]
    hold   = [r["holding_seconds"] for r in rows]
    wins   = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gw     = sum(wins)
    gl     = abs(sum(losses))
    n      = len(rows)

    cap_list = [r["pnl"] / r["MFE"] for r in rows if r["MFE"] > 0.5]

    avg_hold_s = sum(hold) / n
    hold_str   = f"{int(avg_hold_s//60)}m {int(avg_hold_s%60):02d}s"

    return {
        "n":          n,
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(len(wins) / n * 100, 1),
        "total_pnl":  round(sum(pnl), 2),
        "pf":         round(gw / gl, 3) if gl else float("inf"),
        "expectancy": round(sum(pnl) / n, 2),
        "avg_mfe":    round(sum(mfe) / n, 2),
        "avg_mae":    round(sum(mae) / n, 2),
        "avg_pnl":    round(sum(pnl) / n, 2),
        "avg_hold":   hold_str,
        "avg_hold_s": avg_hold_s,
        "capture":    round(sum(cap_list) / len(cap_list), 3) if cap_list else 0.0,
        "best":       round(max(pnl), 2),
        "worst":      round(min(pnl), 2),
    }


def _rs(v: float) -> str:
    return f"+₹{v:,.0f}" if v >= 0 else f"-₹{abs(v):,.0f}"


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


# ══════════════════════════════════════════════════════════════════════
# F2 — DAILY AUTO REVIEW
# ══════════════════════════════════════════════════════════════════════

def eod_review(target_date: date | None = None) -> str:
    """
    Comprehensive end-of-day review.
    Reads from trade log CSV, not the journal.
    """
    d     = target_date or date.today()
    rows  = read_trades(date_from=d, date_to=d)
    if not rows:
        return f"📊 <b>EOD REVIEW — {d}</b>\n\nNo trades today."

    s    = _stats(rows)
    n    = s["n"]

    # Best / worst
    best_row  = max(rows, key=lambda r: r["pnl"])
    worst_row = min(rows, key=lambda r: r["pnl"])

    def _trade_label(row) -> str:
        sym  = row.get("symbol", "")[-8:]
        side = row.get("side", "?")
        er   = row.get("exit_reason", "?")
        return f"{side} {sym}  {er}"

    # Exit reason counts
    exit_counts: dict = {}
    for r in rows:
        k = r.get("exit_reason", "?")
        exit_counts[k] = exit_counts.get(k, 0) + 1
    top_exit_reason = max(exit_counts, key=exit_counts.get)

    # Entry reason (setup type) counts
    setup_counts: dict = {}
    for r in rows:
        k = r.get("entry_reason", "?")
        setup_counts[k] = setup_counts.get(k, 0) + 1
    top_setup = max(setup_counts, key=setup_counts.get)

    # Regime breakdown — quick
    regime_rows: dict = defaultdict(list)
    for r in rows:
        regime_rows[r.get("regime", "UNKNOWN").upper()].append(r)
    regime_lines = []
    for reg, rlist in sorted(regime_rows.items()):
        rs = _stats(rlist)
        regime_lines.append(
            f"  {reg:<10} n={rs['n']}  WR {rs['win_rate']:.0f}%  "
            f"{_rs(rs['total_pnl'])}"
        )

    pf_str = _pf_str(s["pf"])
    return "\n".join([
        f"📊 <b>EOD REVIEW — {d}</b>",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        "",
        "<b>PERFORMANCE</b>",
        f"Trades      {n}   W {s['wins']} / L {s['losses']}",
        f"P&amp;L       {_rs(s['total_pnl'])}",
        f"Win Rate    {s['win_rate']:.0f}%",
        f"Prof Factor {pf_str}",
        f"Expectancy  {_rs(s['expectancy'])}/trade",
        "",
        "<b>TRADE QUALITY</b>",
        f"Avg Hold    {s['avg_hold']}",
        f"Avg MFE     {_rs(s['avg_mfe'])}",
        f"Avg MAE     {_rs(s['avg_mae'])}",
        f"Capture     {s['capture']:.0%}",
        "",
        "<b>HIGHLIGHTS</b>",
        f"Best        {_rs(s['best'])}  {_trade_label(best_row)}",
        f"Worst       {_rs(s['worst'])}  {_trade_label(worst_row)}",
        f"Top exit    {top_exit_reason}  ({exit_counts[top_exit_reason]}×)",
        f"Top setup   {top_setup}  ({setup_counts[top_setup]}×)",
        "",
        "<b>BY REGIME</b>",
    ] + regime_lines + [
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
    ])


# ══════════════════════════════════════════════════════════════════════
# F3 — MARKET REGIME PERFORMANCE
# ══════════════════════════════════════════════════════════════════════

def regime_breakdown(n_trades: int | None = None) -> str:
    rows = read_trades(n=n_trades)
    if not rows:
        return "📈 No trade data for regime breakdown."

    scope = f"last {n_trades} trades" if n_trades else "all-time"
    by_regime: dict = defaultdict(list)
    for r in rows:
        by_regime[r.get("regime", "UNKNOWN").upper()].append(r)

    lines = [
        f"📈 <b>REGIME PERFORMANCE</b>  ({scope})",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
    ]
    for regime in sorted(by_regime):
        s = _stats(by_regime[regime])
        lines += [
            "",
            f"<b>{regime}</b>  ({s['n']} trades)",
            f"W/L: {s['wins']}/{s['losses']}   WR: {s['win_rate']:.0f}%",
            f"P&amp;L: {_rs(s['total_pnl'])}   PF: {_pf_str(s['pf'])}",
            f"Exp: {_rs(s['expectancy'])}/trade   Cap: {s['capture']:.0%}",
            f"Avg MFE: {_rs(s['avg_mfe'])}   Avg MAE: {_rs(s['avg_mae'])}",
        ]
    lines.append("\n<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# F4 — ML SIGNAL QUALITY (probability buckets)
# ══════════════════════════════════════════════════════════════════════

def ml_bucket_breakdown(n_trades: int | None = None) -> str:
    rows = read_trades(n=n_trades)
    if not rows:
        return "🧠 No trade data for ML bucket analysis."

    scope = f"last {n_trades}" if n_trades else "all-time"
    lines = [
        f"🧠 <b>ML SIGNAL QUALITY</b>  ({scope})",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        "<code>Bucket       N   WR    AvgPnL  AvgMFE  Cap</code>",
    ]
    for lo, hi, label in _ML_BUCKETS:
        bucket = [r for r in rows if lo <= r["ml_prob"] < hi]
        if not bucket:
            lines.append(f"<code>{label}  —</code>")
            continue
        s = _stats(bucket)
        lines.append(
            f"<code>{label} "
            f"{s['n']:>3} "
            f"{s['win_rate']:>4.0f}%  "
            f"{s['avg_pnl']:>6.0f}  "
            f"{s['avg_mfe']:>6.0f}  "
            f"{s['capture']:>4.0%}</code>"
        )
    lines.append(
        "\n<i>PnL and MFE in ₹/trade.  "
        "Capture = Realized / MFE.</i>"
    )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# F6 — STRATEGY DRIFT MONITOR
# ══════════════════════════════════════════════════════════════════════

def drift_check(
    windows: list | None = None,
    thresholds: dict | None = None,
) -> tuple:
    """
    Returns (formatted_report: str, alerts: list[str]).
    Sends a Telegram alert message for each threshold breach.
    """
    if windows is None:
        windows = [20, 50, 100]
    thr = {**DRIFT_DEFAULTS, **(thresholds or {})}

    all_rows = read_trades()
    alerts: list = []

    lines = [
        "📉 <b>STRATEGY DRIFT MONITOR</b>",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        "<code>Window  N    WR    PF    Exp      Cap</code>",
    ]

    for w in windows:
        subset = all_rows[-w:] if len(all_rows) >= w else all_rows
        if not subset:
            lines.append(f"<code>L{w:<5} —</code>")
            continue
        s  = _stats(subset)
        pf = s["pf"]
        lines.append(
            f"<code>L{w:<5} {s['n']:>4} {s['win_rate']:>4.0f}%  "
            f"{_pf_str(pf):>6}  {s['expectancy']:>6.0f}  "
            f"{s['capture']:>4.0%}</code>"
        )
        # Only alert when subset is large enough to be meaningful
        if s["n"] < max(w // 3, 5):
            continue
        if s["win_rate"] < thr["win_rate"]:
            alerts.append(
                f"⚠️ DRIFT L{w}: WR {s['win_rate']:.0f}% "
                f"&lt; {thr['win_rate']:.0f}%"
            )
        if s["expectancy"] < thr["expectancy"]:
            alerts.append(
                f"⚠️ DRIFT L{w}: Exp {_rs(s['expectancy'])} "
                f"&lt; {_rs(thr['expectancy'])}"
            )
        if pf != float("inf") and pf < thr["pf"]:
            alerts.append(
                f"⚠️ DRIFT L{w}: PF {pf:.2f} &lt; {thr['pf']}"
            )
        if s["capture"] < thr["capture"]:
            alerts.append(
                f"⚠️ DRIFT L{w}: Capture {s['capture']:.0%} "
                f"&lt; {thr['capture']:.0%}"
            )

    if alerts:
        lines += ["", "🚨 <b>ALERTS</b>"] + alerts
    else:
        lines.append("\n✅ No drift detected")

    lines.append("\n<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>")
    return "\n".join(lines), alerts


# ══════════════════════════════════════════════════════════════════════
# F7 — SETUP PERFORMANCE ANALYTICS
# ══════════════════════════════════════════════════════════════════════

def setup_breakdown(n_trades: int | None = None) -> str:
    rows = read_trades(n=n_trades)
    if not rows:
        return "🎯 No trade data for setup breakdown."

    scope = f"last {n_trades}" if n_trades else "all-time"
    by_setup: dict = defaultdict(list)
    for r in rows:
        by_setup[r.get("entry_reason", "UNKNOWN")].append(r)

    lines = [
        f"🎯 <b>SETUP PERFORMANCE</b>  ({scope})",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        "<code>Setup          N   WR    Exp     PF    MFE</code>",
    ]

    ranked = sorted(
        by_setup.items(),
        key=lambda x: _stats(x[1])["total_pnl"],
        reverse=True,
    )
    for setup, group in ranked:
        s = _stats(group)
        lines.append(
            f"<code>{setup[:14]:<14} "
            f"{s['n']:>3} {s['win_rate']:>4.0f}%  "
            f"{s['avg_pnl']:>6.0f}  {_pf_str(s['pf']):>5}  "
            f"{s['avg_mfe']:>5.0f}</code>"
        )

    lines.append("\n<i>Sorted best-to-worst by total PnL</i>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# F8 — LONG-TERM EQUITY CURVE ANALYTICS
# ══════════════════════════════════════════════════════════════════════

def equity_curve_stats(alert_drawdown_pct: float = 20.0) -> tuple:
    """
    Returns (formatted_report: str, alerts: list[str]).
    Alerts when drawdown from equity peak exceeds alert_drawdown_pct %.
    """
    rows = read_trades()
    if not rows:
        return "📈 No trade data for equity curve.", []

    # Daily aggregation
    by_date: dict = defaultdict(float)
    for r in rows:
        by_date[r["date"]] += r["pnl"]

    dates_sorted = sorted(by_date)
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0

    for d in dates_sorted:
        equity += by_date[d]
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)

    # Consecutive win/loss streaks
    pnl_seq = [r["pnl"] for r in rows]
    max_cw = max_cl = cur_w = cur_l = 0
    for p in pnl_seq:
        if p > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_cw = max(max_cw, cur_w)
        max_cl = max(max_cl, cur_l)

    recovery = (equity / abs(max_dd)) if max_dd != 0 else float("inf")
    if recovery < 0:
        rec_str = "in DD"
    elif recovery == float("inf"):
        rec_str = "∞"
    else:
        rec_str = f"{recovery:.2f}"

    # Weekly rollup
    weekly: dict = defaultdict(float)
    monthly: dict = defaultdict(float)
    for d, pnl in by_date.items():
        dt = date.fromisoformat(d)
        _, wk, _ = dt.isocalendar()
        weekly[f"{dt.year}-W{wk:02d}"] += pnl
        monthly[f"{dt.strftime('%Y-%m')}"] += pnl

    last_7_dates  = dates_sorted[-7:]
    weekly_recent = list(sorted(weekly.items()))[-4:]
    monthly_all   = list(sorted(monthly.items()))

    lines = [
        "📈 <b>EQUITY CURVE</b>",
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>",
        f"Total P&amp;L       {_rs(equity)}",
        f"Max Drawdown   {_rs(max_dd)}",
        f"Recovery Factor {rec_str}",
        f"Max Consec W   {max_cw}",
        f"Max Consec L   {max_cl}",
        f"Trading Days   {len(dates_sorted)}",
        "",
        "<b>RECENT DAILY</b>",
    ] + [
        f"  {d}   {_rs(by_date[d])}"
        for d in last_7_dates
    ] + ["", "<b>WEEKLY</b>"] + [
        f"  {w}   {_rs(p)}"
        for w, p in weekly_recent
    ] + ["", "<b>MONTHLY</b>"] + [
        f"  {m}   {_rs(p)}"
        for m, p in monthly_all
    ] + ["\n<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>"]

    alerts: list = []
    if peak > 0 and abs(max_dd) / peak * 100 > alert_drawdown_pct:
        alerts.append(
            f"🚨 DRAWDOWN ALERT: {abs(max_dd)/peak*100:.1f}% from peak "
            f"(threshold {alert_drawdown_pct:.0f}%)"
        )

    return "\n".join(lines), alerts
