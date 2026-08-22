# engine/diagnostics/eod_report.py
#
# Part 3+4: End-of-day performance report.
# Reads today's journal CSV and produces a structured summary.
# No strategy logic — pure analytics.

import os
import csv
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("eod_report")

_JOURNAL_DIR = os.path.join("data", "diagnostics", "journals")


def _today_journal_path(trade_date: Optional[date] = None) -> str:
    d = trade_date or date.today()
    return os.path.join(_JOURNAL_DIR, f"journal_{d.strftime('%Y_%m_%d')}.csv")


def _load_today(trade_date: Optional[date] = None) -> list:
    path = _today_journal_path(trade_date)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append(row)
            except Exception:
                pass
    # Task #20 FIX 3: since the journal flushes an entry-time row (Task #15
    # BUG 3), open trades leave rows with empty exit_ts/realized_pnl that
    # would be counted as 0.0 losses. Keep CLOSED trades only.
    rows = [r for r in rows if r.get("exit_ts")]
    return rows


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _side_block(trades: list, side: str) -> dict:
    sub = [t for t in trades if t.get("side") == side]
    if not sub:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0,
                "avg_win": 0, "avg_loss": 0, "total": 0}
    wins  = [_safe_float(t["realized_pnl"]) for t in sub if _safe_float(t["realized_pnl"]) > 0]
    losses= [_safe_float(t["realized_pnl"]) for t in sub if _safe_float(t["realized_pnl"]) <= 0]
    n     = len(sub)
    gw    = sum(wins); gl = abs(sum(losses)) or 0.001
    tot   = gw - gl
    return {
        "n":        n,
        "wr":       round(len(wins) / n * 100, 1),
        "pf":       round(gw / gl, 2),
        "exp":      round(tot / n, 1),
        "avg_win":  round(gw / len(wins), 1) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 1) if losses else 0,
        "total":    round(tot, 2),
    }


def _mfe_mae_block(trades: list) -> dict:
    mfe_vals = [_safe_float(t["mfe_rs"]) for t in trades if t.get("mfe_rs") not in ("", None)]
    mae_vals = [_safe_float(t["mae_rs"]) for t in trades if t.get("mae_rs") not in ("", None)]
    stops    = [t for t in trades if t.get("exit_reason") == "STOP"]

    n = len(trades) or 1
    zero_mfe = sum(1 for m in mfe_vals if m <= 0)
    went_pos  = sum(1 for t in stops if _safe_float(t.get("mfe_rs", 0)) > 0)

    return {
        "pct_mfe_zero":        round(zero_mfe / max(len(mfe_vals), 1) * 100, 1),
        "pct_profitable_then_lost": round(went_pos / max(len(stops), 1) * 100, 1),
        "avg_mfe_rs":          round(sum(mfe_vals) / max(len(mfe_vals), 1), 1),
        "avg_mae_rs":          round(sum(mae_vals) / max(len(mae_vals), 1), 1),
        "avg_peak_before_stop": round(
            sum(_safe_float(t.get("mfe_rs", 0)) for t in stops) / max(len(stops), 1),
            1
        ),
    }


def _loss_class_block(trades: list) -> dict:
    counts: dict = {}
    for t in trades:
        cls = t.get("loss_class", "unknown")
        if cls == "winner":
            continue
        counts[cls] = counts.get(cls, 0) + 1
    total = sum(counts.values()) or 1
    return {k: {"n": v, "pct": round(v / total * 100, 1)} for k, v in
            sorted(counts.items(), key=lambda x: -x[1])}


def _exit_reason_block(trades: list) -> dict:
    counts: dict = {}
    pnl_map: dict = {}
    for t in trades:
        reason = t.get("exit_reason", "UNKNOWN")
        pnl    = _safe_float(t.get("realized_pnl", 0))
        counts[reason]  = counts.get(reason, 0) + 1
        pnl_map[reason] = pnl_map.get(reason, 0) + pnl
    return {r: {"n": counts[r], "total_pnl": round(pnl_map[r], 2)} for r in counts}


def _shadow_block(trades: list) -> dict:
    n = len(trades) or 1
    htf_blocked     = sum(1 for t in trades if str(t.get("shadow_htf_would_block","")).lower() == "true")
    ml95_blocked    = sum(1 for t in trades if str(t.get("shadow_ml_95_would_block","")).lower() == "true")
    be3_better      = sum(1 for t in trades if t.get("shadow_be3_outcome") == "better")
    trail10_better  = sum(1 for t in trades if t.get("shadow_trail10_outcome") == "better")
    return {
        "htf_would_have_blocked":      htf_blocked,
        "ml95_would_have_blocked":     ml95_blocked,
        "be3_would_improve":           be3_better,
        "trail10_would_improve":       trail10_better,
    }


def generate_eod_report(trade_date: Optional[date] = None, send_telegram: bool = False) -> dict:
    """
    Build end-of-day report dict. Returns structured dict and optionally formats
    a string for Telegram.
    """
    trades = _load_today(trade_date)
    if not trades:
        logger.info("[EOD] No journal trades found for today.")
        return {}

    pnls   = [_safe_float(t["realized_pnl"]) for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(trades)
    gw     = sum(wins); gl = abs(sum(losses)) or 0.001
    tot    = sum(pnls)

    # Max drawdown (running equity)
    eq = 0.0; peak = 0.0; mdd = 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    report = {
        "date":    (trade_date or date.today()).isoformat(),
        "session": trades[0].get("session_id", ""),
        "version": {
            "git_commit":     trades[0].get("git_commit", ""),
            "config_version": trades[0].get("config_version", ""),
        },
        "overall": {
            "trades":       n,
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / n * 100, 1),
            "profit_factor":round(gw / gl, 2),
            "expectancy":   round(tot / n, 1),
            "gross_profit": round(gw, 2),
            "gross_loss":   round(sum(losses), 2),
            "net_pnl":      round(tot, 2),
            "max_drawdown": round(mdd, 2),
        },
        "ce":           _side_block(trades, "CE"),
        "pe":           _side_block(trades, "PE"),
        "exit_reasons": _exit_reason_block(trades),
        "loss_classes": _loss_class_block(trades),
        "mfe_mae":      _mfe_mae_block(trades),
        "shadow":       _shadow_block(trades),
    }

    _log_report(report)

    if send_telegram:
        _format_telegram(report)

    return report


def _log_report(r: dict):
    o = r["overall"]
    logger.info(
        f"[EOD REPORT] {r['date']} | "
        f"Trades={o['trades']} WR={o['win_rate']}% PF={o['profit_factor']} "
        f"Exp={o['expectancy']:+.0f} Net={o['net_pnl']:+.0f} MDD={o['max_drawdown']:+.0f}"
    )
    ce = r["ce"]; pe = r["pe"]
    logger.info(f"[EOD CE] n={ce['n']} WR={ce['wr']}% PF={ce['pf']} Exp={ce['exp']:+.0f}")
    logger.info(f"[EOD PE] n={pe['n']} WR={pe['wr']}% PF={pe['pf']} Exp={pe['exp']:+.0f}")
    mm = r["mfe_mae"]
    logger.info(
        f"[EOD MFE/MAE] MFE0={mm['pct_mfe_zero']}% "
        f"profThenLost={mm['pct_profitable_then_lost']}% "
        f"avgMFE={mm['avg_mfe_rs']:+.0f} avgMAE={mm['avg_mae_rs']:+.0f}"
    )


def _format_telegram(r: dict) -> str:
    """Return a Telegram-formatted EOD string (HTML)."""
    o  = r["overall"]
    ce = r["ce"]; pe = r["pe"]
    mm = r["mfe_mae"]
    sh = r["shadow"]
    v  = r["version"]

    lines = [
        f"<b>EOD Report — {r['date']}</b>",
        f"<code>{v['git_commit']} / {v['config_version']}</code>",
        "",
        "<b>Overall</b>",
        f"Trades: {o['trades']}  WR: {o['win_rate']}%  PF: {o['profit_factor']}",
        f"Exp: {o['expectancy']:+.0f}  Net: {o['net_pnl']:+.0f}  MDD: {o['max_drawdown']:+.0f}",
        f"GrossWin: {o['gross_profit']:+.0f}  GrossLoss: {o['gross_loss']:+.0f}",
        "",
        "<b>CE</b>",
        f"n={ce['n']}  WR={ce['wr']}%  PF={ce['pf']}  Exp={ce['exp']:+.0f}",
        "",
        "<b>PE</b>",
        f"n={pe['n']}  WR={pe['wr']}%  PF={pe['pf']}  Exp={pe['exp']:+.0f}",
        "",
        "<b>MFE / MAE</b>",
        f"MFE=0: {mm['pct_mfe_zero']}%  "
        f"Won→Lost: {mm['pct_profitable_then_lost']}%",
        f"avgMFE: {mm['avg_mfe_rs']:+.0f}  avgMAE: {mm['avg_mae_rs']:+.0f}",
        "",
        "<b>Shadow analysis</b>",
        f"HTF would block: {sh['htf_would_have_blocked']}  "
        f"ML95 would block: {sh['ml95_would_have_blocked']}",
        f"BE@3 better: {sh['be3_would_improve']}  "
        f"Trail@10 better: {sh['trail10_would_improve']}",
    ]

    # Exit reasons
    if r.get("exit_reasons"):
        lines.append("")
        lines.append("<b>Exit reasons</b>")
        for reason, d in r["exit_reasons"].items():
            lines.append(f"  {reason}: {d['n']} ({d['total_pnl']:+.0f})")

    # Loss classes
    if r.get("loss_classes"):
        lines.append("")
        lines.append("<b>Loss categories</b>")
        for cls, d in r["loss_classes"].items():
            lines.append(f"  {cls}: {d['n']} ({d['pct']}%)")

    msg = "\n".join(lines)
    logger.info("[EOD TELEGRAM]\n" + msg)
    return msg


def format_eod_telegram(trade_date: Optional[date] = None) -> str:
    """Public helper: build report and return formatted Telegram string."""
    r = generate_eod_report(trade_date)
    return _format_telegram(r) if r else "No trades today."
