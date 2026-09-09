# engine/diagnostics/session_activity.py
#
# SESSION ACTIVITY RECORDER — full observability of every trading decision.
#
# Purpose: after any session (win or lose) we can answer "WHY did we lose?"
# by replaying this structured event stream.  Every event is one JSON line in
#   data/diagnostics/activity/activity_YYYY-MM-DD.jsonl
#
# Event types
#   LIFECYCLE     engine start/stop, kill switch, auto-pause, feed stale,
#                 ATM drift, resume — system health timeline
#   DECISION      per-minute ML decision snapshot: CE/PE probs, direction,
#                 thresholds, 5m trend, VWAP, active blockers, signal fire
#   GATE_BLOCKED  signal fired but an entry gate rejected it (PAUSED,
#                 TG_THRESHOLD, SAME_SYMBOL_COOLDOWN, MAX_TRADES, OI_WALL,
#                 ALLOC_ZERO, TG_SKIP, NO_ATM_SYMBOL)
#   ENTRY         full entry snapshot (fill, qty, SL/TP, ml_prob, regime,
#                 features, signal delay)
#   EXIT          full exit snapshot (reason, pnl, MFE/MAE, capture, exit
#                 classification, market context)
#   EXIT_HOLD     MIN_PROFIT gate held a profitable exit (profit give-back)
#   SL_UPDATE     trailing stop raised (ladder progression)
#   SCALP_ENTRY   scalp layer entry
#   SCALP_EXIT    scalp layer exit
#
# Safety contract: diagnostics must NEVER break the trading loop.
# Every public function swallows all exceptions.  Appends are locked,
# flushed immediately, and opened/closed per write to survive crashes.

import os
import json
import logging
import threading
from datetime import datetime, date, time as dtime

logger = logging.getLogger("session_activity")

# ── Storage ───────────────────────────────────────────────────────────
_ACTIVITY_DIR = os.path.join("data", "diagnostics", "activity")

_lock      = threading.Lock()
_context   = {}          # live market context injected into every event
_last_dec  = {}          # last DECISION minute + signature (throttle)
_last_gate = {}          # (gate, side) -> last minute logged (per-min dedup)
_counters  = {}          # in-memory census: event type -> count
_gate_census = {}        # gate -> count (for session summary)
_blocker_census = {}     # blocker-key -> count (for session summary)
_session_started = None  # datetime of first event this process


# ══════════════════════════════════════════════════════════════════════
# INTERNALS
# ══════════════════════════════════════════════════════════════════════

def _activity_path(dt: date | None = None) -> str:
    if dt is None:
        dt = date.today()
    os.makedirs(_ACTIVITY_DIR, exist_ok=True)
    return os.path.join(_ACTIVITY_DIR, f"activity_{dt.isoformat()}.jsonl")


def _clean(obj):
    """Make a value JSON-serializable (recursively, cheaply)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    try:
        return round(float(obj), 6)   # numpy scalars etc.
    except (TypeError, ValueError):
        return str(obj)


def _write(event_type: str, **fields):
    """Append one event line.  Never raises."""
    try:
        now = datetime.now()
        rec = {
            "ts":   now.isoformat(timespec="seconds"),
            "type": event_type,
        }
        # Inject live context (never overrides event-specific fields)
        for k, v in _context.items():
            if k not in fields:
                rec[k] = v
        for k, v in fields.items():
            if v is not None:
                rec[k] = v

        line = json.dumps(_clean(rec), separators=(",", ":"))
        with _lock:
            _counters[event_type] = _counters.get(event_type, 0) + 1
            if _session_started is None:
                globals()["_session_started"] = now
            with open(_activity_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass   # diagnostics must never break trading

# ══════════════════════════════════════════════════════════════════════
# CONTEXT  (set once per engine-loop cycle by master_runner)
# ══════════════════════════════════════════════════════════════════════

def set_context(**fields):
    """
    Update the live context attached to every subsequent event.
    Call once per engine-loop cycle with current market state, e.g.:
        set_context(nifty=..., ce=..., pe=..., day_type=..., regime=...,
                    in_trade=..., day_pnl=..., trades_today=...)
    """
    try:
        with _lock:
            _context.update(_clean(fields))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# EVENT LOGGERS  (all safe to call from anywhere in the trading path)
# ══════════════════════════════════════════════════════════════════════

def log_lifecycle(event: str, **fields):
    """Engine start/stop, kill switch, auto-pause, feed stale, ATM drift..."""
    _write("LIFECYCLE", event=event, **fields)
    try:
        logger.info(f"[ACT] LIFECYCLE {event} {fields if fields else ''}")
    except Exception:
        pass


def log_decision(
    ce: float, pe: float, dir_side: str, thr: float,
    htf5: int, pvwap: float, blockers: list, signal_fired: bool,
    price: float, nifty: float | None = None, **extra,
):
    """
    Record one ML decision evaluation.
    Throttled internally: one line per new minute, plus immediate lines when
    the blocker signature changes or a signal fires (no transition lost).
    """
    try:
        now = datetime.now()
        minute = now.replace(second=0, microsecond=0).isoformat(timespec="seconds")
        sig = tuple(sorted(b.split("(")[0].split(" ")[0].strip() for b in blockers))

        with _lock:
            new_minute = (minute != _last_dec.get("minute"))
            changed    = (sig != _last_dec.get("sig"))
            prev_fired = bool(_last_dec.get("fired"))
            _last_dec.update(minute=minute, sig=sig, fired=signal_fired)

        # Census every evaluation (not throttled) for the analyzer.
        # Key = blocker name only: "ML_BELOW_THR(CE 0.55 < 0.40)" -> "ML_BELOW_THR"
        for b in blockers:
            _bk = b.split("(")[0].split(" ")[0].strip()
            if _bk:
                _blocker_census[_bk] = _blocker_census.get(_bk, 0) + 1

        if signal_fired or prev_fired or changed or new_minute:
            _extra = {"price": price} if price else {}
            _write(
                "DECISION",
                nifty=(nifty if nifty is not None else (price or None)),
                ce=ce, pe=pe, dir=dir_side, thr=thr,
                htf5=htf5, pvwap=pvwap,
                blockers=list(blockers),
                signal_fired=bool(signal_fired),
                **extra, **_extra,
            )
    except Exception:
        pass


def log_gate(gate: str, side: str | None = None, detail: str | None = None, **fields):
    """
    A fired signal was rejected by an entry gate.
    Dedup: one event per (gate, side) per minute so a 300 s cooldown logs
    ~5 lines, not 300.
    """
    try:
        now = datetime.now()
        minute = now.replace(second=0, microsecond=0)
        key = (gate, side or "")
        with _lock:
            repeat = (_last_gate.get(key) == minute)
            _last_gate[key] = minute
        _gate_census[gate] = _gate_census.get(gate, 0) + 1
        if not repeat:
            _write("GATE_BLOCKED", gate=gate, side=side, detail=detail, **fields)
    except Exception:
        pass


def log_entry(position: dict, nifty: float | None = None, **extra):
    """Full snapshot when an entry fills."""
    try:
        _write(
            "ENTRY",
            symbol=position.get("symbol"),
            side=position.get("side"),
            fill=position.get("entry"),
            qty=position.get("qty"),
            lot_size=position.get("lot_size"),
            sl=position.get("stop_loss"),
            tp=position.get("target"),
            ml_prob=position.get("ml_prob"),
            regime=position.get("regime"),
            reason=position.get("reason"),
            nifty=nifty,
            features=position.get("features", {}),
            **extra,
        )
    except Exception:
        pass

def log_exit(
    position: dict | None,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    exit_ts: datetime | None = None,
    exit_type: str | None = None,
    entry_time: datetime | None = None,
    held_seconds: float | None = None,
    nifty: float | None = None,
    **extra,
):
    """Full snapshot when an exit fills (the loss/money trail)."""
    try:
        pos = position or {}
        _write(
            "EXIT",
            symbol=pos.get("symbol"),
            side=pos.get("side"),
            entry=pos.get("entry"),
            exit_price=exit_price,
            qty=pos.get("qty"),
            pnl=pnl,
            exit_reason=exit_reason,
            exit_type=exit_type,
            ml_prob=pos.get("ml_prob"),
            regime=pos.get("regime"),
            mfe=pos.get("max_pnl"),
            mae=pos.get("min_pnl"),
            sl_final=pos.get("stop_loss"),
            held_seconds=held_seconds,
            entry_time=entry_time,
            exit_ts=exit_ts,
            nifty=nifty,
            **extra,
        )
    except Exception:
        pass


def log_exit_hold(exit_reason: str, cur_pnl: float, threshold: float = 250.0,
                  pos_ltp: float | None = None, **fields):
    """
    MIN_PROFIT gate HELD a profitable exit — profit give-back tracking.
    These events explain 'why did the winner become a loser' days.
    """
    _write("EXIT_HOLD", held_exit_reason=exit_reason, cur_pnl=cur_pnl,
           threshold=threshold, pos_ltp=pos_ltp, **fields)


def log_sl_update(symbol: str | None, new_sl: float, old_sl: float,
                  ltp: float | None = None, reason: str | None = None, **fields):
    """Trailing stop / ladder raised the stop (profit-lock progression)."""
    _write("SL_UPDATE", symbol=symbol, new_sl=new_sl, old_sl=old_sl,
           ltp=ltp, reason=reason, **fields)


def log_scalp_entry(position: dict, nifty: float | None = None, **extra):
    _write("SCALP_ENTRY", symbol=position.get("symbol"),
           side=position.get("side"), fill=position.get("entry"),
           qty=position.get("qty"), sl=position.get("stop_loss"),
           tp=position.get("target"), nifty=nifty, **extra)


def log_scalp_exit(position: dict | None, exit_price: float, exit_reason: str,
                   pnl: float, nifty: float | None = None, **extra):
    pos = position or {}
    _write("SCALP_EXIT", symbol=pos.get("symbol"), side=pos.get("side"),
           entry=pos.get("entry"), exit_price=exit_price, qty=pos.get("qty"),
           pnl=pnl, exit_reason=exit_reason, nifty=nifty, **extra)


# ══════════════════════════════════════════════════════════════════════
# SESSION SUMMARY  (for EOD report / engine shutdown)
# ══════════════════════════════════════════════════════════════════════

def session_summary() -> dict:
    """In-memory census of everything logged this process."""
    with _lock:
        return {
            "started":         _session_started.isoformat(timespec="seconds") if _session_started else None,
            "events":          dict(_counters),
            "gates":           dict(_gate_census),
            "blockers":        dict(_blocker_census),
            "activity_file":   _activity_path(),
        }