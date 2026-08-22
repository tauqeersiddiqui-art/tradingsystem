# engine/diagnostics/trade_journal.py
#
# Trade Intelligence & Diagnostics Framework — Part 1-2-5-6
#
# Purpose: maximum observability with ZERO strategy impact.
# No filters, no sizing changes, no threshold changes.
# Everything here is write-only from the trading path's perspective.

import os
import csv
import json
import time
import threading
import logging
import subprocess
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("trade_journal")

# ── Storage paths ─────────────────────────────────────────────────────
_BASE_DIR     = os.path.join("data", "diagnostics")
_JOURNAL_DIR  = os.path.join(_BASE_DIR, "journals")
_SHADOW_DIR   = os.path.join(_BASE_DIR, "shadow")
_VERSION_FILE = os.path.join(_BASE_DIR, "session_version.json")

for _d in (_JOURNAL_DIR, _SHADOW_DIR):
    os.makedirs(_d, exist_ok=True)


# ── Schema ────────────────────────────────────────────────────────────
_JOURNAL_COLUMNS = [
    # --- identity ---
    "journal_id", "session_id", "trade_date",
    "git_commit", "ce_model_mtime", "pe_model_mtime", "config_version",
    # --- entry snapshot ---
    "entry_ts", "symbol", "side", "regime", "signal_reason",
    "entry_price", "nifty_spot_at_entry",
    "ml_prob", "ce_prob_raw", "pe_prob_raw",
    "atr_at_entry", "vwap_state",           # 'above'|'below'|'unknown'
    "htf_trend_state",                       # 'bullish'|'bearish'|'unknown'
    "orb_state",                             # 'above'|'below'|'inside'|'no_orb'
    "qty",
    # --- intra-trade LTP snapshots ---
    "ltp_5s", "ltp_10s", "ltp_30s", "ltp_60s",
    # --- during-trade extremes ---
    "mfe_rs", "mae_rs", "peak_drawdown_rs",
    "ladder_stage",          # highest rung reached: INITIAL|S0_LOCK32|S1_LOCK65|…
    "entry_delay_ms",        # ms from signal (first [ENTRY SIGNAL]) to fill
    # --- exit ---
    "exit_ts", "exit_price", "exit_reason",
    "realized_pnl", "holding_seconds",
    # --- loss classification (Part 2) ---
    "loss_class",
    # --- shadow analysis (Part 6) ---
    "shadow_htf_would_block",    # True/False/None
    "shadow_ml_95_would_block",  # True/False
    "shadow_be3_outcome",        # 'same'|'better'|'worse'
    "shadow_trail10_outcome",    # 'same'|'better'|'worse'
    "shadow_notes",
]

_SHADOW_COLUMNS = [
    "journal_id", "session_id", "trade_date",
    "side", "entry_price", "ml_prob",
    "actual_pnl", "actual_exit_reason",
    "shadow_htf_block",
    "shadow_ml90_block", "shadow_ml95_block",
    "shadow_be3_pnl",    # estimated PnL if BE@3 used
    "shadow_be2_pnl",
    "shadow_trail10_pnl",
    "notes",
]


# ── Loss classifier ───────────────────────────────────────────────────

def classify_loss(trade: dict) -> str:
    """
    Classify a losing trade into one of nine categories.
    Uses: mfe_rs, entry_price, exit_price, ltp_5s, ltp_30s, nifty_spot context.
    """
    pnl       = trade.get("realized_pnl", 0)
    mfe_rs    = trade.get("mfe_rs", 0) or 0
    mae_rs    = trade.get("mae_rs", 0) or 0
    qty       = trade.get("qty", 1) or 1
    entry     = trade.get("entry_price", 0) or 0
    exit_p    = trade.get("exit_price", 0) or 0
    ltp_5s    = trade.get("ltp_5s")
    ltp_30s   = trade.get("ltp_30s")
    side      = trade.get("side", "")
    exit_r    = trade.get("exit_reason", "")

    if pnl > 0:
        return "winner"

    mfe_pts  = mfe_rs / qty if qty > 0 else 0
    loss_pts = abs(pnl) / qty if qty > 0 else 0

    # Immediate adverse — option moved against us within 5 seconds
    if ltp_5s and entry > 0:
        move_5s = (ltp_5s - entry) * (1 if side == "CE" else -1)
        if move_5s < -1.0:
            return "immediate_adverse_move"

    # Spread loss — option never moved, loss ≈ stop distance
    if mfe_pts <= 0.3 and loss_pts <= 4.0:
        return "spread_loss"

    # Good trade reversed — peaked well, then reversed to stop
    if mfe_pts >= 5.0 and exit_r == "STOP":
        return "good_trade_reversed"

    # Stop too tight — peaked at 1–4 pts then stopped
    if 0.3 < mfe_pts < 5.0 and exit_r == "STOP":
        return "stop_too_tight"

    # Wrong direction — option moved immediately and continuously against us
    if ltp_30s and entry > 0:
        move_30s = (ltp_30s - entry) * (1 if side == "CE" else -1)
        if move_30s < -2.0 and mfe_pts <= 0:
            return "wrong_directional_signal"

    # Theta / flat — option decayed without directional move
    if abs(exit_p - entry) < 2.0 and loss_pts > 0:
        return "theta_decay"

    return "other"


# ── Shadow analysis ───────────────────────────────────────────────────

def _shadow_be_pnl(trade: dict, trigger_pts: float) -> Optional[float]:
    """Estimate PnL if a different BE trigger had been active."""
    mfe_rs  = trade.get("mfe_rs", 0) or 0
    qty     = trade.get("qty", 1) or 1
    actual  = trade.get("realized_pnl", 0)
    is_stop = trade.get("exit_reason") == "STOP"
    mfe_pts = mfe_rs / qty if qty > 0 else 0
    if is_stop and mfe_pts >= trigger_pts:
        return 0.0   # would have exited at break-even
    return actual


def _shadow_trail10_pnl(trade: dict) -> Optional[float]:
    """
    Estimate PnL if first trail triggered at 10 pts instead of 15.
    For STOP trades with 10 <= mfe_pts < 15: would have locked 40% of peak.
    """
    mfe_rs  = trade.get("mfe_rs", 0) or 0
    qty     = trade.get("qty", 1) or 1
    actual  = trade.get("realized_pnl", 0)
    is_stop = trade.get("exit_reason") == "STOP"
    entry   = trade.get("entry_price", 0) or 0
    mfe_pts = mfe_rs / qty if qty > 0 else 0
    if is_stop and 10.0 <= mfe_pts < 15.0:
        locked = entry + mfe_pts * 0.40
        return locked * qty - entry * qty   # exit at 40% of peak
    return actual


def compute_shadow(trade: dict, htf_would_block: Optional[bool]) -> dict:
    """Return shadow analysis dict for one trade."""
    ml_prob = trade.get("ml_prob", 0) or 0
    actual  = trade.get("realized_pnl", 0)
    be3     = _shadow_be_pnl(trade, 3.0)
    be2     = _shadow_be_pnl(trade, 2.0)
    tr10    = _shadow_trail10_pnl(trade)

    def _outcome(shadow_pnl):
        if shadow_pnl is None or shadow_pnl == actual:
            return "same"
        return "better" if shadow_pnl > actual else "worse"

    return {
        "shadow_htf_block":    htf_would_block,
        "shadow_ml90_block":   ml_prob < 0.90,
        "shadow_ml95_block":   ml_prob < 0.95,
        "shadow_be3_pnl":      be3,
        "shadow_be2_pnl":      be2,
        "shadow_trail10_pnl":  tr10,
        "shadow_be3_outcome":  _outcome(be3),
        "shadow_trail10_outcome": _outcome(tr10),
    }


# ── Version info ──────────────────────────────────────────────────────

def _get_version_info() -> dict:
    """Collect git hash, model mtimes, and config version. Called once at startup."""
    git_commit = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            git_commit = result.stdout.strip()
    except Exception:
        pass

    def _mtime(path):
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
        except Exception:
            return "missing"

    info = {
        "git_commit":      git_commit,
        "ce_model_mtime":  _mtime("ml/models/champion_ce_lgbm.pkl"),
        "pe_model_mtime":  _mtime("ml/models/champion_pe_lgbm.pkl"),
        "config_version":  "272b3fb_be4_htf30m_recency3x",   # bump this string on each strategy change
        "session_start":   datetime.now().isoformat(),
    }

    try:
        with open(_VERSION_FILE, "w") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        logger.warning(f"[JOURNAL] Could not write version file: {e}")

    return info


# ══════════════════════════════════════════════════════════════════════
# MAIN CLASS
# ══════════════════════════════════════════════════════════════════════

class TradeJournal:
    """
    Passive observability layer.

    Call sequence per trade:
        journal.on_entry(position, market_state, ts)
        journal.on_tick(position, ltp, elapsed_s)   # called by engine_loop every cycle
        journal.on_exit(position, exit_price, exit_reason, pnl, exit_ts, htf_block)

    Thread-safe: all file I/O uses a lock.
    """

    def __init__(self):
        self._lock           = threading.Lock()
        self._version        = _get_version_info()
        self._session_id     = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._seq            = 0
        self._active: dict   = {}   # journal_id → in-progress record

        self._journal_path   = self._make_path(_JOURNAL_DIR, "journal")
        self._shadow_path    = self._make_path(_SHADOW_DIR,  "shadow")
        self._ensure_headers()

        logger.info(
            f"[JOURNAL] Started | session={self._session_id} | "
            f"commit={self._version['git_commit']} | "
            f"config={self._version['config_version']}"
        )

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _make_path(directory: str, prefix: str) -> str:
        today = date.today().strftime("%Y_%m_%d")
        return os.path.join(directory, f"{prefix}_{today}.csv")

    def _ensure_headers(self):
        for path, cols in [
            (self._journal_path, _JOURNAL_COLUMNS),
            (self._shadow_path,  _SHADOW_COLUMNS),
        ]:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(cols)

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._session_id}_{self._seq:04d}"

    def _write_row(self, path: str, columns: list, record: dict):
        row = [record.get(col, "") for col in columns]
        with self._lock:
            with open(path, "a", newline="") as f:
                csv.writer(f).writerow(row)

    # ── Part 5: log version at startup ───────────────────────────────

    def log_startup(self, config: object = None):
        v = self._version
        logger.info(
            f"[VERSION] commit={v['git_commit']} "
            f"ce_model={v['ce_model_mtime']} "
            f"pe_model={v['pe_model_mtime']} "
            f"config={v['config_version']}"
        )

    # ── Part 1: entry snapshot ────────────────────────────────────────

    def on_entry(
        self,
        position: dict,
        market_state: dict,
        ts: datetime,
        ce_prob_raw: float = 0.0,
        pe_prob_raw: float = 0.0,
        nifty_spot: float  = 0.0,
        htf_state: str     = "unknown",
        entry_delay_ms: int = 0,
    ) -> str:
        """
        Record entry snapshot. Returns journal_id for this trade.
        market_state is the dict from live_engine.get_market_state().
        """
        jid = self._next_id()

        orb_high = market_state.get("orb_high")
        orb_low  = market_state.get("orb_low")
        price    = position.get("entry", 0)
        if orb_high and orb_low:
            if price > orb_high:
                orb_state = "above"
            elif price < orb_low:
                orb_state = "below"
            else:
                orb_state = "inside"
        else:
            orb_state = "no_orb"

        price_vs_vwap = market_state.get("price_vs_vwap", 0)
        vwap_state = "above" if price_vs_vwap > 0 else ("below" if price_vs_vwap < 0 else "unknown")

        record = {
            # identity
            "journal_id":      jid,
            "session_id":      self._session_id,
            "trade_date":      ts.date().isoformat(),
            "git_commit":      self._version["git_commit"],
            "ce_model_mtime":  self._version["ce_model_mtime"],
            "pe_model_mtime":  self._version["pe_model_mtime"],
            "config_version":  self._version["config_version"],
            # entry
            "entry_ts":        ts.isoformat(),
            "symbol":          position.get("symbol", ""),
            "side":            position.get("side", ""),
            "regime":          position.get("regime", ""),
            "signal_reason":   position.get("reason", ""),
            "entry_price":     round(price, 2),
            "nifty_spot_at_entry": round(nifty_spot, 2),
            "ml_prob":         round(position.get("ml_prob", 0), 4),
            "ce_prob_raw":     round(ce_prob_raw, 4),
            "pe_prob_raw":     round(pe_prob_raw, 4),
            "atr_at_entry":    round(market_state.get("atr", 0), 2),
            "vwap_state":      vwap_state,
            "htf_trend_state": htf_state,
            "orb_state":       orb_state,
            "qty":             position.get("qty", 0),
            # placeholders filled later
            "ltp_5s": "", "ltp_10s": "", "ltp_30s": "", "ltp_60s": "",
            "mfe_rs": "", "mae_rs": "", "peak_drawdown_rs": "",
            "ladder_stage": "INITIAL", "entry_delay_ms": entry_delay_ms,
            "exit_ts": "", "exit_price": "", "exit_reason": "",
            "realized_pnl": "", "holding_seconds": "",
            "loss_class": "",
            "shadow_htf_would_block": "", "shadow_ml_95_would_block": "",
            "shadow_be3_outcome": "", "shadow_trail10_outcome": "", "shadow_notes": "",
        }

        self._active[jid] = {
            "record": record,
            "entry_ts_epoch": time.time(),
            "mfe_rs": 0.0,
            "mae_rs": 0.0,
            "peak_pnl": 0.0,
            "min_pnl":  0.0,
        }

        logger.info(
            f"[JOURNAL] ENTRY jid={jid} {position.get('side')} {position.get('symbol')} "
            f"entry={price:.2f} ml={position.get('ml_prob',0):.3f} "
            f"htf={htf_state} vwap={vwap_state} orb={orb_state}"
        )
        return jid

    # ── Part 1: intra-trade tick ──────────────────────────────────────

    def on_tick(self, jid: str, ltp: float, position: dict):
        """
        Call on every engine loop cycle while a position is open.
        Updates LTP snapshots and running MFE/MAE.
        Zero file I/O — all in memory until on_exit().
        """
        if jid not in self._active:
            return

        state   = self._active[jid]
        elapsed = time.time() - state["entry_ts_epoch"]
        entry   = position.get("entry", ltp)
        qty     = position.get("qty", 1) or 1
        pnl     = (ltp - entry) * qty

        # LTP snapshots — set once, never overwrite
        rec = state["record"]
        if elapsed >= 5  and rec["ltp_5s"]  == "": rec["ltp_5s"]  = round(ltp, 2)
        if elapsed >= 10 and rec["ltp_10s"] == "": rec["ltp_10s"] = round(ltp, 2)
        if elapsed >= 30 and rec["ltp_30s"] == "": rec["ltp_30s"] = round(ltp, 2)
        if elapsed >= 60 and rec["ltp_60s"] == "": rec["ltp_60s"] = round(ltp, 2)

        # Running extremes
        if pnl > state["peak_pnl"]:
            state["peak_pnl"] = pnl
            state["mfe_rs"]   = pnl
        if pnl < state["min_pnl"]:
            state["min_pnl"]  = pnl
            state["mae_rs"]   = pnl

        # Track highest ladder rung reached (from position dict — updated by profit_manager)
        _ladder_stage = position.get("_ladder_stage", "")
        if _ladder_stage and _ladder_stage != "INITIAL":
            rec["ladder_stage"] = _ladder_stage

        state["last_pnl"] = pnl

    # ── Part 1+2+6: exit, classify, shadow ───────────────────────────

    def on_exit(
        self,
        jid: str,
        position: dict,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        exit_ts: datetime,
        htf_would_block: Optional[bool] = None,
        loss_class: str = "",
    ):
        """
        Finalise the journal record: exit fields, loss classification, shadow analysis.
        Writes one row to journal CSV and one row to shadow CSV.

        loss_class — optional Phase-4 entry-quality class (LATE_ENTRY /
        REVERSAL / UNKNOWN) supplied by master_runner. Persisted into the
        existing free-text shadow_notes column (no schema change); the
        loss_class column keeps the internal Part-2 classifier output.
        """
        if jid not in self._active:
            logger.warning(f"[JOURNAL] on_exit: unknown jid={jid}")
            return

        state   = self._active.pop(jid)
        rec     = state["record"]
        entry   = position.get("entry", 0)
        qty     = position.get("qty", 1) or 1
        held    = (exit_ts - datetime.fromisoformat(rec["entry_ts"])).total_seconds()
        peak_dd = state["min_pnl"] - state["peak_pnl"]   # always <= 0

        # Fill exit fields
        rec.update({
            "exit_ts":         exit_ts.isoformat(),
            "exit_price":      round(exit_price, 2),
            "exit_reason":     exit_reason,
            "realized_pnl":    round(pnl, 2),
            "holding_seconds": round(held, 1),
            "mfe_rs":          round(state["mfe_rs"], 2),
            "mae_rs":          round(state["mae_rs"], 2),
            "peak_drawdown_rs": round(peak_dd, 2),
        })

        # Part 2: loss classification
        rec["loss_class"] = classify_loss(rec) if pnl <= 0 else "winner"

        # Phase 4: entry-quality loss class → existing free-text column,
        # keeping _JOURNAL_COLUMNS unchanged for already-rotated files.
        if loss_class:
            rec["shadow_notes"] = f"eq_loss_class={loss_class}"

        # Part 6: shadow analysis
        shadow = compute_shadow(rec, htf_would_block)
        rec["shadow_htf_would_block"]   = shadow["shadow_htf_block"]
        rec["shadow_ml_95_would_block"] = shadow["shadow_ml95_block"]
        rec["shadow_be3_outcome"]       = shadow["shadow_be3_outcome"]
        rec["shadow_trail10_outcome"]   = shadow["shadow_trail10_outcome"]

        # Write journal row
        self._write_row(self._journal_path, _JOURNAL_COLUMNS, rec)

        # Write shadow row
        shadow_rec = {
            "journal_id":        jid,
            "session_id":        self._session_id,
            "trade_date":        rec["trade_date"],
            "side":              rec["side"],
            "entry_price":       rec["entry_price"],
            "ml_prob":           rec["ml_prob"],
            "actual_pnl":        rec["realized_pnl"],
            "actual_exit_reason":exit_reason,
            "shadow_htf_block":  shadow["shadow_htf_block"],
            "shadow_ml90_block": shadow["shadow_ml90_block"],
            "shadow_ml95_block": shadow["shadow_ml95_block"],
            "shadow_be3_pnl":    shadow["shadow_be3_pnl"],
            "shadow_be2_pnl":    shadow["shadow_be2_pnl"],
            "shadow_trail10_pnl":shadow["shadow_trail10_pnl"],
            "notes":             f"loss_class={rec['loss_class']}",
        }
        self._write_row(self._shadow_path, _SHADOW_COLUMNS, shadow_rec)

        logger.info(
            f"[JOURNAL] EXIT jid={jid} {rec['side']} pnl={pnl:+.2f} "
            f"reason={exit_reason} class={rec['loss_class']} "
            f"eq_class={loss_class or '-'} "
            f"mfe={rec['mfe_rs']} mae={rec['mae_rs']} held={held:.0f}s"
        )

    # ── Convenience: current path accessors ─────────────────────────

    @property
    def journal_path(self) -> str:
        return self._journal_path

    @property
    def shadow_path(self) -> str:
        return self._shadow_path
