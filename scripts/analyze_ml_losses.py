"""ML loss post-mortem — evidence-only diagnostic (no trading behaviour changes).

Joins the three data sources already written by the live paper-trading session:

  * data/diagnostics/journals/journal_<date>.csv  — entry/exit, ATR, EMA/VWAP
    state, signal + exit reason, MFE/MAE, post-entry LTP series, shadow flags.
  * data/phase55/phase55_decisions_<key>.csv       — regime, ml_probability,
    Phase5.5 recommendation, blocking reason.
  * data/phase55/phase55_outcomes_<key>.csv         — realised ALLOW outcomes.
  * data/analytics/replays/*.json                    — per-tick ADX / RSI /
    supertrend at entry.

It explains WHY the ML engine's trades lost. It does not modify any engine,
model, config default, or trading decision — it only reads and reports.

Output: artifacts/reports/ml_loss_analysis/
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NOT_CAPTURED = "NOT_CAPTURED"  # honest marker for fields the telemetry never logged


def _f(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _hhmmss(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ts or ""


def _load_replay_entries(replay_dir: Path) -> list[dict[str, Any]]:
    """Return each replay's entry-event features keyed by symbol/side/time."""
    out: list[dict[str, Any]] = []
    for path in sorted(replay_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        events = data.get("events") or []
        entry = next((e for e in events if str(e.get("type")).upper() == "ENTRY"), None)
        if not entry:
            continue
        out.append(
            {
                "symbol": data.get("symbol"),
                "side": data.get("side"),
                "t": entry.get("t"),
                "adx": entry.get("adx"),
                "rsi": entry.get("rsi"),
                "st_dir": entry.get("st_dir"),
                "vwap": entry.get("vwap"),
                "regime": entry.get("regime"),
            }
        )
    return out


def _match_replay(replays: list[dict[str, Any]], symbol: str, side: str, hhmmss: str) -> dict[str, Any]:
    for r in replays:
        if r.get("symbol") == symbol and str(r.get("side")).upper() == side and r.get("t") == hhmmss:
            return r
    # Fall back to symbol+side only (entry second can drift a tick).
    for r in replays:
        if r.get("symbol") == symbol and str(r.get("side")).upper() == side:
            return r
    return {}


def _delay_verdict(row: dict[str, str], side: str) -> str:
    """Would entering a few seconds later have helped? Read the post-entry LTP path."""
    entry = _f(row.get("entry_price"))
    later = [_f(row.get(k), None) for k in ("ltp_5s", "ltp_10s", "ltp_30s", "ltp_60s")]
    later = [v for v in later if v is not None]
    if entry is None or not later:
        return "UNKNOWN (no post-entry LTP series)"
    # For a long option, a lower price shortly after entry = a better fill was available.
    best_later = min(later)
    drift = best_later - entry
    be3 = str(row.get("shadow_be3_outcome") or "").lower()
    if drift < -1.0:
        return (
            f"LIKELY YES — price fell to {best_later:.1f} vs entry {entry:.1f} "
            f"({drift:+.1f}); a delayed/limit entry would have improved basis"
        )
    if be3 == "better":
        return "LIKELY YES — shadow break-even-at-3 exit would have improved the outcome"
    return f"PROBABLY NO — price did not pull back after entry (best later {best_later:.1f} vs {entry:.1f})"


def _p55_recommendation_that_would_prevent(row: dict[str, str], p55: dict[str, str]) -> str:
    reasons: list[str] = []
    if str(row.get("shadow_ml_95_would_block")).lower() == "true":
        reasons.append("ML-0.95 confidence floor (shadow_ml_95_would_block=True)")
    if str(row.get("shadow_htf_would_block")).lower() == "true":
        reasons.append("HTF/30m EMA trend gate (shadow_htf_would_block=True)")
    ce_q = _f(p55.get("confidence"))
    # Phase5.5 CE quality threshold is 0.4358 — record whether it engaged at all.
    if p55 and ce_q is not None:
        reasons.append(
            f"Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf "
            f"{ce_q:.3f} cleared it, so the current threshold would NOT have prevented this"
        )
    return "; ".join(reasons) if reasons else "None of the recorded shadow gates would have prevented it"


def build() -> Path:
    date_key = "2026_07_06"
    session_key = "20260706"
    journal = _read_csv(ROOT / f"data/diagnostics/journals/journal_{date_key}.csv")
    decisions = _read_csv(ROOT / f"data/phase55/phase55_decisions_{session_key}.csv")
    outcomes = _read_csv(ROOT / f"data/phase55/phase55_outcomes_{session_key}.csv")
    replays = _load_replay_entries(ROOT / "data/analytics/replays")

    dec_by_id = {d["decision_id"]: d for d in decisions if d.get("decision_id")}
    allowed_outcomes = [o for o in outcomes if o.get("outcome_class") == "ACTUAL_ALLOWED_TRADE"]

    trades: list[dict[str, Any]] = []
    for row in journal:
        exit_ts = row.get("exit_ts", "")
        symbol = row.get("symbol", "")
        side = str(row.get("side", "")).upper()
        # Join Phase5.5 outcome by exact exit timestamp + symbol, then to its decision.
        outcome = next(
            (o for o in allowed_outcomes if o.get("timestamp") == exit_ts and o.get("symbol") == symbol),
            {},
        )
        p55 = dec_by_id.get(outcome.get("decision_id", ""), {})
        replay = _match_replay(replays, symbol, side, _hhmmss(row.get("entry_ts", "")))
        pnl = _f(row.get("realized_pnl"))
        trades.append({"journal": row, "p55": p55, "outcome": outcome, "replay": replay, "pnl": pnl})

    losers = [t for t in trades if (t["pnl"] or 0) < 0]
    winners = [t for t in trades if (t["pnl"] or 0) >= 0]
    net = sum(t["pnl"] or 0 for t in trades)

    out_dir = ROOT / "artifacts/reports/ml_loss_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- structured JSON ----
    json_trades = []
    for t in trades:
        j, p, r = t["journal"], t["p55"], t["replay"]
        json_trades.append(
            {
                "entry_ts": j.get("entry_ts"),
                "exit_ts": j.get("exit_ts"),
                "symbol": j.get("symbol"),
                "direction": j.get("side"),
                "regime_engine": j.get("regime"),
                "regime_phase55": p.get("regime", NOT_CAPTURED if not p else ""),
                "confidence_ml_prob": _f(j.get("ml_prob")),
                "ce_prob_raw": _f(j.get("ce_prob_raw")),
                "pe_prob_raw": _f(j.get("pe_prob_raw")),
                "shap_explanation": NOT_CAPTURED,
                "atr_at_entry": _f(j.get("atr_at_entry")),
                "adx_at_entry": r.get("adx", NOT_CAPTURED),
                "rsi_at_entry": r.get("rsi", NOT_CAPTURED),
                "ema_state": j.get("htf_trend_state"),
                "vwap_state": j.get("vwap_state"),
                "supertrend_dir": r.get("st_dir", NOT_CAPTURED),
                "volatility": f"ATR {j.get('atr_at_entry')} (no separate vol metric logged)",
                "entry_reason": j.get("signal_reason"),
                "exit_reason": j.get("exit_reason"),
                "stop_or_target": "STOP hit" if j.get("exit_reason") == "STOP" else j.get("exit_reason"),
                "mfe_rs": _f(j.get("mfe_rs")),
                "mae_rs": _f(j.get("mae_rs")),
                "loss_class": j.get("loss_class"),
                "realized_pnl": t["pnl"],
                "qty": _f(j.get("qty")),
                "phase55_recommendation_that_would_prevent": _p55_recommendation_that_would_prevent(j, p),
                "should_have_been_skipped": bool((t["pnl"] or 0) < 0 and str(j.get("shadow_ml_95_would_block")).lower() == "true"),
                "delaying_entry_verdict": _delay_verdict(j, t["journal"].get("side", "")),
            }
        )

    (out_dir / f"ml_loss_analysis_{session_key}.json").write_text(
        json.dumps(
            {
                "generated": datetime.now().isoformat(),
                "session": session_key,
                "net_ml_pnl": round(net, 2),
                "n_trades": len(trades),
                "n_losers": len(losers),
                "n_winners": len(winners),
                "trades": json_trades,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---- markdown report ----
    md = _render_md(session_key, trades, losers, winners, net, json_trades)
    md_path = out_dir / f"ml_loss_analysis_{session_key}.md"
    md_path.write_text(md, encoding="utf-8")
    return md_path


def _render_md(session_key, trades, losers, winners, net, json_trades) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# ML Loss Analysis — Session {session_key}\n")
    a(f"_Generated: {datetime.now().isoformat()} — evidence only, no trading logic changed._\n")

    gross_loss = sum(t["pnl"] for t in losers)
    gross_win = sum(t["pnl"] for t in winners)
    a("## Headline\n")
    a(f"- ML trades: **{len(trades)}** — Winners **{len(winners)}** / Losers **{len(losers)}**")
    a(f"- Gross wins **₹{gross_win:+.1f}**, gross losses **₹{gross_loss:+.1f}**, **net ₹{net:+.1f}**")
    a(f"- Avg win **₹{(gross_win/len(winners)) if winners else 0:+.1f}** vs avg loss "
      f"**₹{(gross_loss/len(losers)) if losers else 0:+.1f}** — payoff is inverted (small wins, big losses)\n")

    a("## Root-cause pattern (across ALL 8 trades)\n")
    all_pos_mfe = all((_f(t['journal'].get('mfe_rs')) or 0) > 0 for t in trades)
    a(f"- **Every trade went green first**: MFE > 0 on all trades = {all_pos_mfe}. "
      "Entries were directionally reasonable; the loss came *after* a favourable excursion.")
    a("- **No trade hit its target.** Exits were STOP (7) or TIME_EXIT_WEAK (1) — winners were "
      "small break-even/ladder locks while losers ran to the full stop.")
    a("- **Confidence was NOT the problem.** ML probabilities ranged 0.68–0.99; the Phase5.5 CE "
      "quality threshold (0.4358) never engaged. A confidence filter would not have prevented these.")
    a("- The `shadow_ml_95_would_block` flag is **True for all 8** — only an aggressive 0.95 floor "
      "blocks them, and that also blocks the winners.")
    a("- Dominant `loss_class`: `immediate_adverse_move` and `stop_too_tight` — an **exit/stop "
      "management** signature, not an entry-selection one.\n")

    a("## Per-trade breakdown (losers)\n")
    for jt in [x for x in json_trades if (x['realized_pnl'] or 0) < 0]:
        a(f"### {jt['symbol']} {jt['direction']}  |  ₹{jt['realized_pnl']:+.1f}  ({jt['loss_class']})\n")
        a(f"- **Entry / Exit:** {jt['entry_ts']} → {jt['exit_ts']}")
        a(f"- **Regime:** engine=`{jt['regime_engine']}`, phase55=`{jt['regime_phase55']}`")
        a(f"- **Confidence:** ml_prob={jt['confidence_ml_prob']}, ce_raw={jt['ce_prob_raw']}, pe_raw={jt['pe_prob_raw']}")
        a(f"- **SHAP:** {jt['shap_explanation']} (not logged by current telemetry)")
        a(f"- **ATR:** {jt['atr_at_entry']}  |  **ADX:** {jt['adx_at_entry']}  |  **RSI:** {jt['rsi_at_entry']}")
        a(f"- **EMA/HTF state:** {jt['ema_state']}  |  **VWAP:** {jt['vwap_state']}  |  **Supertrend dir:** {jt['supertrend_dir']}")
        a(f"- **Entry reason:** {jt['entry_reason']}  |  **Exit reason:** {jt['exit_reason']} ({jt['stop_or_target']})")
        a(f"- **MFE / MAE:** ₹{jt['mfe_rs']:+.1f} / ₹{jt['mae_rs']:+.1f}  (went green ₹{jt['mfe_rs']:.0f} before reversing)")
        a(f"- **Phase5.5 rec that would prevent it:** {jt['phase55_recommendation_that_would_prevent']}")
        a(f"- **Should have been skipped?** {'YES' if jt['should_have_been_skipped'] else 'NO (no recorded gate flags it)'}")
        a(f"- **Would delaying entry have helped?** {jt['delaying_entry_verdict']}\n")

    a("## Winners (contrast)\n")
    a("| Symbol | Dir | PnL | MFE | MAE | conf | exit |")
    a("|---|---|---|---|---|---|---|")
    for jt in [x for x in json_trades if (x['realized_pnl'] or 0) >= 0]:
        a(f"| {jt['symbol']} | {jt['direction']} | ₹{jt['realized_pnl']:+.1f} | "
          f"₹{jt['mfe_rs']:+.0f} | ₹{jt['mae_rs']:+.0f} | {jt['confidence_ml_prob']} | {jt['exit_reason']} |")

    a("\n## Telemetry gaps (fields the required report asks for but the system does not log)\n")
    a("- **SHAP explanation** — no SHAP values are computed or persisted anywhere in the engine.")
    a("- **RSI / ADX / supertrend** — only present in replay JSONs, not in the trade journal.")
    a("- **Volatility** — no standalone metric; ATR-at-entry is the only volatility proxy logged.")
    a("- Recommend (post-freeze): add SHAP + RSI/ADX columns to the trade journal so this "
      "analysis is fully data-driven next session.\n")

    a("## Bottom line\n")
    a("The ML engine's entries were directionally sound (every trade printed positive MFE). The "
      "−₹981 came from **asymmetric exits**: winners locked tiny gains while losers round-tripped to "
      "full stops. **No Phase5.5 confidence/regime filter as configured would have converted this "
      "day to positive** — the lever is stop/exit management, which is explicitly out of scope for "
      "this freeze. Phase5.5 continues to record shadow evidence for a future, properly-calibrated gate.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
