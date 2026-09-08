# scripts/analyze_session.py
#
# SESSION FORENSICS — "WHY DID WE LOSE?" report.
#
# Reads the structured activity stream written by
# engine/diagnostics/session_activity.py:
#     data/diagnostics/activity/activity_YYYY-MM-DD.jsonl
# and turns it into a human post-mortem: where the money went, what the ML
# was saying at each entry, which gates blocked signals, which losses were
# bad-luck vs bad-process, and the top likely causes of the day's result.
#
# Usage:
#   python scripts/analyze_session.py                 # today
#   python scripts/analyze_session.py --date 2026-09-03
#   python scripts/analyze_session.py --json          # machine-readable
#
# Cost model (CLAUDE.md): ₹66 per lot per completed trade (buy+sell pair).
#   Expense = round((qty / 65) * 66)

import os
import sys
import json
import argparse
from datetime import datetime, date, time as dtime

# Windows consoles default to cp1252 and crash on ₹/§ box-drawing output.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTIVITY_DIR = os.path.join("data", "diagnostics", "activity")
COST_PER_LOT_ROUNDTRIP = 66.0      # ₹ per lot per completed trade (CLAUDE.md)
LOT_UNIT               = 65        # qty units per lot
GAVE_BACK_KEEP_RATIO   = 0.30      # kept <30% of peak profit => gave it back
INSTANT_LOSS_SECONDS   = 60        # lost within 60s of entry => instant loss
COIN_FLIP_EDGE         = 0.15      # |CE-PE| below this = ML had no conviction


# ──────────────────────────────────────────────────────────────────────
# LOADERS
# ──────────────────────────────────────────────────────────────────────

def activity_path(d: date) -> str:
    return os.path.join(ACTIVITY_DIR, f"activity_{d.isoformat()}.jsonl")


def load_events(d: date) -> list:
    path = activity_path(d)
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                ev = json.loads(ln)
                ev["_dt"] = datetime.fromisoformat(ev["ts"])
                events.append(ev)
            except Exception:
                continue
    events.sort(key=lambda e: e["_dt"])
    return events


def trade_cost(qty) -> float:
    try:
        lots = int(qty or 0) / LOT_UNIT
        return round(lots * COST_PER_LOT_ROUNDTRIP, 2)
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────────
# TRADE ARCHETYPES — classify HOW each trade lost
# ──────────────────────────────────────────────────────────────────────

def classify_trade(tr: dict) -> list:
    """Return a list of archetype tags explaining this trade's outcome."""
    tags = []
    pnl     = float(tr.get("pnl") or 0)
    mfe     = float(tr.get("mfe") or 0)
    held    = float(tr.get("held_seconds") or 0)
    etype   = str(tr.get("exit_type") or "")
    regime  = str(tr.get("regime") or "").upper()
    edge    = tr.get("_edge_at_entry")

    if pnl > 0:
        tags.append("WINNER")
        if mfe > 0 and pnl < mfe * GAVE_BACK_KEEP_RATIO:
            tags.append(f"GAVE_BACK(kept {pnl/mfe*100:.0f}% of +{mfe:.0f} peak)")
        return tags

    # Losing trade archetypes
    if held > 0 and held < INSTANT_LOSS_SECONDS:
        tags.append("INSTANT_LOSS(went negative in <60s = wrong direction at entry)")
    if mfe >= 150 and pnl < mfe * GAVE_BACK_KEEP_RATIO:
        tags.append(f"GAVE_BACK(peak +{mfe:.0f} -> final {pnl:+.0f})")
    if etype == "STOP_LOSS_HIT":
        tags.append("CLEAN_STOP(hard stop hit = risk worked, entry was wrong)")
    elif etype == "TRAILING_STOP" and pnl < 0:
        tags.append("TRAIL_FAIL(trailing stop gave back profit then stopped)")
    elif etype == "BREAK_EVEN_STOP" and pnl < 0:
        tags.append("BE_SLIP(break-even stop still lost = slippage)")
    elif etype == "PROFIT_PROTECTION":
        tags.append("PROFIT_PROTECT(drawdown exit)")
    elif etype == "TIME_EXIT":
        tags.append("TIME_EXIT(weak trade ran out of clock)")
    if "RANGE" in regime or regime in ("UNKNOWN", ""):
        tags.append("CHOP(entry during range regime)")
    if edge is not None and edge < COIN_FLIP_EDGE:
        tags.append(f"COIN_FLIP(ML edge only {edge:.2f} at entry)")
    if not tags:
        tags.append("GENERIC_LOSS")
    return tags


def nearest_decision(events: list, ts: datetime) -> dict | None:
    """Last DECISION event at or before ts (for entry-context audit)."""
    best = None
    for ev in events:
        if ev["type"] != "DECISION" or ev["_dt"] > ts:
            continue
        best = ev
    return best


# ──────────────────────────────────────────────────────────────────────
# ANALYSIS
# ──────────────────────────────────────────────────────────────────────

def analyze(events: list) -> dict:
    trades    = [e for e in events if e["type"] == "EXIT"]
    scalps    = [e for e in events if e["type"] == "SCALP_EXIT"]
    gates     = [e for e in events if e["type"] == "GATE_BLOCKED"]
    lifes     = [e for e in events if e["type"] == "LIFECYCLE"]
    holds     = [e for e in events if e["type"] == "EXIT_HOLD"]
    decisions = [e for e in events if e["type"] == "DECISION"]

    # ── attach entry context (ML edge at entry) to each trade ─────────
    for tr in trades:
        dec = nearest_decision(events, tr["_dt"])
        edge = None
        if dec:
            try:
                edge = abs(float(dec.get("ce", 0)) - float(dec.get("pe", 0)))
            except Exception:
                edge = None
        tr["_edge_at_entry"] = edge
        tr["_tags"] = classify_trade(tr)

    gross = sum(float(t.get("pnl") or 0) for t in trades) + \
            sum(float(s.get("pnl") or 0) for s in scalps)
    costs = sum(trade_cost(t.get("qty")) for t in trades) + \
            sum(trade_cost(s.get("qty")) for s in scalps)

    # Exit-type P&L table
    by_type = {}
    for t in trades + scalps:
        if t["type"] == "SCALP_EXIT":
            et = "SCALP_" + str(t.get("exit_reason", "?"))
        else:
            et = t.get("exit_type") or str(t.get("exit_reason", "?"))
        d = by_type.setdefault(et, {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        p = float(t.get("pnl") or 0)
        d["n"]      += 1
        d["pnl"]    += p
        d["wins"]   += 1 if p > 0 else 0
        d["losses"] += 1 if p <= 0 else 0

    # Gate census (signals fired but blocked)
    by_gate = {}
    for g in gates:
        gt = g.get("gate", "?")
        d  = by_gate.setdefault(gt, {"n": 0, "sides": {}})
        d["n"] += 1
        s = str(g.get("side") or "?")
        d["sides"][s] = d["sides"].get(s, 0) + 1

    # Dominant blockers from decision stream
    blockers = {}
    fired = 0
    for dec in decisions:
        if dec.get("signal_fired"):
            fired += 1
        for b in dec.get("blockers", []) or []:
            bk = str(b).split("(")[0].strip()
            blockers[bk] = blockers.get(bk, 0) + 1

    # Time-of-day PnL
    tod = {}
    for t in trades + scalps:
        h, m = t["_dt"].hour, t["_dt"].minute
        bucket = ("09-10" if h < 10 else "10-11" if h < 11 else
                  "11-12:30" if (h < 12 or (h == 12 and m < 30)) else
                  "12:30-14" if h < 14 else "14-close")
        d = tod.setdefault(bucket, {"n": 0, "pnl": 0.0})
        d["n"]   += 1
        d["pnl"] += float(t.get("pnl") or 0)

    return {
        "trades": trades, "scalps": scalps, "gates": gates, "lifecycles": lifes,
        "exit_holds": holds, "decisions": decisions,
        "gross": gross, "costs": costs, "net": gross - costs,
        "by_type": by_type, "by_gate": by_gate, "blockers": blockers,
        "signals_fired": fired,
        "tod": tod,
        "first": events[0]["ts"] if events else None,
        "last":  events[-1]["ts"] if events else None,
    }


# ──────────────────────────────────────────────────────────────────────
# REPORT RENDERER
# ──────────────────────────────────────────────────────────────────────

def _fmt_rs(v) -> str:
    try:
        return f"{float(v):+,.0f}"
    except Exception:
        return str(v)


def render_report(a: dict, d: date) -> str:
    L = []
    bar = "═" * 78
    thin = "─" * 78
    L.append(bar)
    L.append(f"  SESSION FORENSICS — {d.isoformat()}   (WHY did we lose/win?)")
    L.append(f"  events: {a['first']} → {a['last']}")
    L.append(bar)

    # §1 P&L ----------------------------------------------------------------
    n_all = len(a["trades"]) + len(a["scalps"])
    L.append("")
    L.append("§1  P&L SUMMARY")
    L.append(thin)
    L.append(f"  Trades closed:        {n_all}  (ML: {len(a['trades'])}, scalp: {len(a['scalps'])})")
    L.append(f"  Gross P&L:            ₹{_fmt_rs(a['gross'])}")
    L.append(f"  Brokerage+tax (est.): ₹{_fmt_rs(-a['costs'])}"
             f"   (₹{COST_PER_LOT_ROUNDTRIP:.0f}/lot/trade)")
    L.append(f"  NET P&L:              ₹{_fmt_rs(a['net'])}")
    if a["exit_holds"]:
        L.append(f"  Profit holds (₹250 gate): {len(a['exit_holds'])} — winning exits delayed")

    # §2 Where the money went ----------------------------------------------
    L.append("")
    L.append("§2  WHERE THE MONEY WENT  (by exit type)")
    L.append(thin)
    if a["by_type"]:
        L.append(f"  {'exit type':<24}{'n':>4}{'W':>4}{'L':>4}{'gross ₹':>12}")
        for et, st in sorted(a["by_type"].items(), key=lambda kv: kv[1]["pnl"]):
            L.append(f"  {et:<24}{st['n']:>4}{st['wins']:>4}{st['losses']:>4}"
                     f"{_fmt_rs(st['pnl']):>12}")
    else:
        L.append("  (no trades)")

    # §3 Trade-by-trade forensics ------------------------------------------
    L.append("")
    L.append("§3  TRADE-BY-TRADE FORENSICS")
    L.append(thin)
    for t in a["trades"]:
        held = float(t.get("held_seconds") or 0)
        try:
            _nifty_s = f"{float(t.get('nifty')):,.1f}"
        except (TypeError, ValueError):
            _nifty_s = str(t.get("nifty") or "?")
        L.append(
            f"  {t['ts'][11:19]} {t.get('side','?'):<3} {t.get('symbol','?'):<22} "
            f"pnl={_fmt_rs(t.get('pnl')):>8}  ml={t.get('ml_prob', 0):.2f} "
            f"held={held:.0f}s mfe={_fmt_rs(t.get('mfe'))} nifty={_nifty_s}"
        )
        for tag in t.get("_tags", []):
            L.append(f"      → {tag}")
    for s in a["scalps"]:
        L.append(
            f"  {s['ts'][11:19]} SCALP {s.get('side','?'):<3} {s.get('symbol','?'):<19} "
            f"pnl={_fmt_rs(s.get('pnl')):>8}  reason={s.get('exit_reason')}"
        )
    if not a["trades"] and not a["scalps"]:
        L.append("  (no completed trades)")

    # §4 Gates — signals blocked at the door --------------------------------
    L.append("")
    L.append("§4  GATE CENSUS  (ML fired but an entry gate said NO)")
    L.append(thin)
    if a["by_gate"]:
        for gt, st in sorted(a["by_gate"].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"  {gt:<24} {st['n']:>4}x   sides={st['sides']}")
        L.append("  (each blocked signal is a trade we never took — could be")
        L.append("   saving us money, or missing winners; compare with §5)")
    else:
        L.append("  (no gate rejections recorded)")

    # §5 What the ML was doing all day --------------------------------------
    L.append("")
    L.append("§5  ML DECISION STREAM")
    L.append(thin)
    L.append(f"  decision snapshots logged: {len(a['decisions'])}"
             f"   |  signals fired: {a['signals_fired']}")
    if a["blockers"]:
        top = sorted(a["blockers"].items(), key=lambda kv: -kv[1])[:10]
        L.append("  dominant blockers (how often the ML was held back):")
        for bk, n in top:
            L.append(f"    {bk:<28} {n:>5}x")
    if a["exit_holds"]:
        L.append(f"  ⚠ ₹250 profit-gate held {len(a['exit_holds'])} winning exits:")
        for h in a["exit_holds"][:8]:
            L.append(f"    {h['ts'][11:19]} {h.get('held_exit_reason')} "
                     f"@ ₹{_fmt_rs(h.get('cur_pnl'))} (kept holding…)")
        L.append("  → each hold risks giving a winner back to the market.")

    # §6 Time-of-day ---------------------------------------------------------
    L.append("")
    L.append("§6  TIME-OF-DAY P&L")
    L.append(thin)
    for bucket in ("09-10", "10-11", "11-12:30", "12:30-14", "14-close"):
        if bucket in a["tod"]:
            st = a["tod"][bucket]
            L.append(f"  {bucket:<12} n={st['n']:<3} ₹{_fmt_rs(st['pnl']):>10}")

    # §7 Health / lifecycle ---------------------------------------------------
    L.append("")
    L.append("§7  SESSION HEALTH TIMELINE")
    L.append(thin)
    for ev in a["lifecycles"]:
        extra = ""
        if ev.get("event") == "KILL_SWITCH":
            extra = f"  day_pnl=₹{_fmt_rs(ev.get('day_pnl'))}"
        elif ev.get("event") == "FEED_STALE":
            extra = f"  stale {ev.get('stale_seconds', '?')}s"
        elif ev.get("event") == "AUTO_PAUSE":
            extra = f"  after {ev.get('consecutive_stops')} losing stops"
        L.append(f"  {ev['ts'][11:19]}  {ev.get('event')}{extra}")
    if not a["lifecycles"]:
        L.append("  (no lifecycle events)")

    return "\n".join(L)



# ──────────────────────────────────────────────────────────────────────
# §8 VERDICT — auto-generated "top causes of today's result"
# ──────────────────────────────────────────────────────────────────────

def render_verdict(a: dict) -> str:
    L = []
    bar = "═" * 78
    L.append("")
    L.append("§8  VERDICT — TOP CAUSES OF TODAY'S RESULT")
    L.append(bar)
    causes = []

    # Rank loss buckets by absolute damage
    for et, st in sorted(a["by_type"].items(), key=lambda kv: kv[1]["pnl"]):
        if st["pnl"] < 0 and st["losses"] > 0:
            causes.append((st["pnl"],
                           f"{et} burned ₹{_fmt_rs(st['pnl'])} across "
                           f"{st['losses']} loss(es) — biggest single damage source"))

    # Gave-back winners
    gb = [(t, tag) for t in a["trades"] for tag in t.get("_tags", [])
          if tag.startswith("GAVE_BACK") and float(t.get("pnl") or 0) <= 0]
    if gb:
        total_mfe = sum(float(t.get("mfe") or 0) for t, _ in gb)
        causes.append((-total_mfe,
                       f"{len(gb)} trade(s) had real profit and gave it back "
                       f"(peak sum ₹{_fmt_rs(total_mfe)}) — profit-retention problem, "
                       f"review trailing/₹250-hold logic"))

    # Instant losses = direction wrong at entry
    il = [t for t in a["trades"]
          if any(tg.startswith("INSTANT_LOSS") for tg in t.get("_tags", []))]
    if il:
        tot = sum(float(t.get("pnl") or 0) for t in il)
        causes.append((tot,
                       f"{len(il)} instant loss(es) (negative in <60s, total ₹{_fmt_rs(tot)}) "
                       f"— entries were on the wrong side of the move"))

    # Coin-flip entries
    cf = [t for t in a["trades"]
          if any(tg.startswith("COIN_FLIP") for tg in t.get("_tags", []))]
    if cf:
        tot = sum(float(t.get("pnl") or 0) for t in cf)
        causes.append((min(0, tot),
                       f"{len(cf)} entry(ies) fired with ML edge < {COIN_FLIP_EDGE} "
                       f"(total ₹{_fmt_rs(tot)}) — raise ML_EDGE_MARGIN or threshold"))

    # Chop entries
    ch = [t for t in a["trades"]
          if any(tg.startswith("CHOP") for tg in t.get("_tags", []))
          and float(t.get("pnl") or 0) < 0]
    if ch:
        tot = sum(float(t.get("pnl") or 0) for t in ch)
        causes.append((tot,
                       f"{len(ch)} loss(es) entered in RANGE regime (₹{_fmt_rs(tot)}) "
                       f"— consider standing down in chop"))

    # Profit holds
    if a["exit_holds"]:
        causes.append((-250.0 * len(a["exit_holds"]),
                       f"₹250 profit-gate held {len(a['exit_holds'])} winning exit(s) — "
                       f"each hold gave the market another chance to reverse"))

    # Health problems
    n_stale = sum(1 for e in a["lifecycles"] if e.get("event") == "FEED_STALE")
    if n_stale:
        causes.append((0.0, f"feed went stale {n_stale}x — execution may have "
                            f"filled at stale prices"))

    # Costs
    if a["costs"] > 0:
        causes.append((-a["costs"],
                       f"brokerage+taxes cost ₹{_fmt_rs(-a['costs'])} on "
                       f"{len(a['trades']) + len(a['scalps'])} trades — "
                       f"fewer, higher-conviction trades"))

    if not causes:
        L.append("  No losing patterns detected (or not enough data).")
        return "\n".join(L)

    for i, (impact, text) in enumerate(
            sorted(causes, key=lambda c: c[0])[:5], 1):
        L.append(f"  #{i}  {text}")
    L.append("")
    L.append("  Fix order: address #1 and #2 first — they carry the day's damage.")
    return "\n".join(L)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Session forensics: why did we lose?")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--json", action="store_true", help="dump analysis as JSON")
    args = ap.parse_args()

    if args.date:
        try:
            d = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Bad --date '{args.date}' (expected YYYY-MM-DD)")
            sys.exit(2)
    else:
        d = date.today()

    events = load_events(d)
    if not events:
        print(f"No activity data for {d.isoformat()}")
        print(f"Expected file: {activity_path(d)}")
        print("The session activity stream is written by master_runner.py")
        print("(engine/diagnostics/session_activity.py) while the engine runs.")
        sys.exit(0)

    a = analyze(events)
    if args.json:
        def _ser(o):
            if isinstance(o, datetime):
                return o.isoformat(timespec="seconds")
            return str(o)
        slim = {k: v for k, v in a.items() if k not in ("trades", "scalps")}
        slim["trades"] = [
            {k: v for k, v in t.items() if k not in ("_dt", "features")}
            for t in a["trades"]
        ]
        slim["scalps"] = [
            {k: v for k, v in s.items() if k != "_dt"} for s in a["scalps"]
        ]
        print(json.dumps(slim, default=_ser, indent=2))
        return

    print(render_report(a, d))
    print(render_verdict(a))


if __name__ == "__main__":
    main()