# master_runner.py
# FIXED v2 — Production Safe
#
# Fixes applied:
#   FIX-1 : Position management (SL, trailing, kill switch) moved OUT of except block
#   FIX-2 : entry_time defined at entry, not undefined
#   FIX-3 : CandleBuilder replaces DataManager.get_latest_window() in live mode
#   FIX-4 : lot_size from executor.get_lot_size(), not from get_atm_option()
#   FIX-5 : execute_exit receives side= kwarg for correct BUY/SELL direction
#   FIX-6 : poll_manual_exit() called every cycle
#   FIX-7 : Telegram rate limiting per message type
#   FIX-8 : MAX_TRADES_PER_DAY enforced
#   FIX-9 : learner.record_trade_result() called after every exit
#   FIX-10: OI wall filter wired before entry
#   FIX-11: daily loss limit checked every cycle (not only in except)
#   FIX-12: broker.start_feed() token passed correctly

from dotenv import load_dotenv
load_dotenv()

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import time
import atexit
import logging
import threading
import collections
from datetime import datetime, timedelta

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
_fh = logging.FileHandler("logs/master_runner.log", mode="a", encoding="utf-8")
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_fh)
logger = logging.getLogger("master")

# ── ENV flags ─────────────────────────────────────────────────────────
PAPER_MODE = os.getenv("PAPER_MODE", "0") == "1"
TEST_MODE  = os.getenv("TEST_MODE",  "0") == "1"

HIST_CSV   = "data/historical/banknifty_1m_full.csv"

# ── Imports ───────────────────────────────────────────────────────────
from engine.execution.execution_engine import ExecutionEngine
from engine.core.context import TradingContext
from engine.config.config import Config
from engine.core.health_monitor import update_health, snapshot
from engine.live_engine import LiveEngine
from engine.portfolio.allocator import CapitalAllocator
from engine.execution.filters import has_oi_wall
from engine.risk.risk_manager import compute_entry_stops
from engine.execution.profit_manager import ladder_stop
from engine.core.state_store import save_state, load_state, deserialize_position
from engine.services.trade_logger import log_trade, today_summary
from engine.diagnostics import TradeJournal, generate_eod_report
from engine.scalping.scalp_engine import ScalpEngine

# Analytics suite (Features 1–8) — observational only, no trading logic
from engine.analytics.trade_replay import TradeReplay
from engine.analytics.slippage import log_slip, slippage_stats
from engine.analytics.performance import (
    eod_review,
    regime_breakdown,
    ml_bucket_breakdown,
    drift_check,
    setup_breakdown,
    equity_curve_stats,
)

from ml.ml_intraday_learner import IntradayMLLearner

from engine.services.dashboard import render_engine, render_market

from telegram.messages import (
    format_trade_entry,
    format_trade_exit,
    format_trade_live,
    format_engine_dashboard,
    format_scalp_entry,
    format_scalp_live,
    format_scalp_exit,
)
from telegram.notifier import (
    send_trade_entry_with_exit_button,
    update_trade_live,
    delete_trade_message,
    repost_engine_dashboard,
    send_bot,
    send_trade_channel,
    remove_exit_button,
    poll_commands,
    ask_trade_permission,
    send_or_edit_market_dashboard,
    send_eod_summary,
    MANUAL_EXIT_REQUESTED,
    send_scalp_entry,
    update_scalp_live,
    delete_scalp_message,
    freeze_trade_message,
    freeze_scalp_message,
)

# ── Candle builder (live + paper) ─────────────────────────────────────
from engine.data.candle_builder import CandleBuilder

_engine_thread = None


# ══════════════════════════════════════════════════════════════════════
# TELEGRAM RATE LIMITER
# ══════════════════════════════════════════════════════════════════════

class _TelegramThrottle:
    """Per-message-type rate limiter.  Prevents Telegram bans."""

    def __init__(self):
        self._last: dict = {}

    def can_send(self, key: str, min_interval: float = 10.0) -> bool:
        now = time.time()
        if now - self._last.get(key, 0) >= min_interval:
            self._last[key] = now
            return True
        return False

_tg = _TelegramThrottle()


def _log_stop_audit(tag: str, ltp: float, position: dict) -> None:
    """Task 5 — prove which stop was active at a stop-based exit.

    Logs LTP / stop_loss / entry / max_pnl / current pnl just before exit.
    """
    try:
        entry = position.get("entry", 0.0)
        sl    = position.get("stop_loss", 0.0)
        qty   = position.get("qty", position.get("lot_size", 1))
        mp    = position.get("max_pnl", 0.0)
        cur   = (ltp - entry) * qty
        logger.info(
            f"[{tag}]\nLTP={ltp:.2f}\nSL={sl:.2f}\nENTRY={entry:.2f}\n"
            f"MAXPNL={mp:.0f}\nCURPNL={cur:.0f}"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# BROKER-SIDE PROTECTIVE STOP LIFECYCLE  (true stop enforcement)
# ══════════════════════════════════════════════════════════════════════
# Wraps ExecutionEngine SL-M primitives with audit logging + failsafe.
# Failsafe policy (FAILSAFE spec): on any create/modify/repair failure ->
# CRITICAL log + Telegram alert + PAUSE new entries, while KEEPING the
# existing protection (virtual polling stop, and any prior broker stop) so a
# position is never left unprotected.

def _sl_pause_entries() -> None:
    try:
        import telegram.notifier as _tn
        _tn.ENGINE_PAUSED = True
    except Exception:
        pass


def _sl_failsafe(op: str, position: dict) -> None:
    sym = position.get("symbol", "")
    sl  = position.get("stop_loss", 0.0)
    logger.critical(
        f"[BROKER SL FAILSAFE] {op} FAILED for {sym} SL={sl:.2f} — "
        f"virtual stop retained, NEW ENTRIES PAUSED"
    )
    try:
        tg_force(
            f"🚨 BROKER STOP {op.upper()} FAILED\n{sym}  SL={sl:.2f}\n"
            f"Virtual stop still active. Entries PAUSED — send /resume."
        )
    except Exception:
        pass
    _sl_pause_entries()


def _sl_create(ctx, position: dict) -> None:
    """Place the protective SL-M when a trade opens."""
    try:
        oid = ctx.executor.place_protective_stop(
            position["symbol"], position["qty"], position["stop_loss"]
        )
    except Exception as e:
        logger.critical(f"[BROKER SL CREATED] exception: {e}")
        oid = None
    if oid:
        position["sl_order_id"] = oid
        logger.info(
            f"[BROKER SL CREATED]\n{position['symbol']}  "
            f"trigger={position['stop_loss']:.2f}  id={oid}"
        )
    else:
        position["sl_order_id"] = None
        _sl_failsafe("create", position)


def _sl_modify(ctx, position: dict, new_stop: float) -> None:
    """Raise the broker stop when the ladder tightens. No cancel-first (atomic)."""
    oid = position.get("sl_order_id")
    if not oid:
        # Broker stop missing (earlier create failed) — try to establish one now.
        _sl_create(ctx, position)
        return
    if ctx.executor.modify_protective_stop(oid, new_stop):
        logger.info(
            f"[BROKER SL MODIFIED]\n{position['symbol']}  "
            f"trigger->{new_stop:.2f}  id={oid}"
        )
    else:
        _sl_failsafe("modify", position)


def _sl_cancel(ctx, position: dict) -> None:
    """Cancel the protective stop on exit and clear the saved id."""
    oid = position.get("sl_order_id")
    if not oid:
        return
    ctx.executor.cancel_protective_stop(oid)
    logger.info(f"[BROKER SL CANCELLED]\n{position.get('symbol','')}  id={oid}")
    position["sl_order_id"] = None


def _sl_verify_or_repair(ctx, position: dict) -> None:
    """Restart recovery: confirm broker stop matches expected stop_loss; repair if not."""
    sym  = position.get("symbol", "")
    want = position.get("stop_loss", 0.0)
    oid  = position.get("sl_order_id")

    found = None
    info  = ctx.executor.get_order_info(oid) if oid else None
    if info and info.get("status") in ("TRIGGER PENDING", "OPEN"):
        found = {"order_id": oid, "trigger_price": info.get("trigger_price", 0.0)}
    if not found:
        found = ctx.executor.find_open_stop_order(sym)

    if found:
        position["sl_order_id"] = found["order_id"]
        want_t = ctx.executor._round_tick(want)
        if abs(found["trigger_price"] - want_t) <= 0.05 + 1e-9:
            logger.info(
                f"[BROKER SL VERIFIED]\n{sym}  trigger={found['trigger_price']:.2f}  "
                f"id={found['order_id']}"
            )
        elif ctx.executor.modify_protective_stop(found["order_id"], want):
            logger.info(
                f"[BROKER SL REPAIRED]\n{sym}  "
                f"{found['trigger_price']:.2f}->{want:.2f}  id={found['order_id']}"
            )
        else:
            _sl_failsafe("repair", position)
    else:
        # No protective order at the broker at all — create one now.
        _sl_create(ctx, position)
        if position.get("sl_order_id"):
            logger.info(
                f"[BROKER SL REPAIRED]\n{sym}  created missing stop "
                f"trigger={want:.2f}  id={position['sl_order_id']}"
            )

_WATCHDOG_MAX_RESTARTS = int(os.getenv("WATCHDOG_MAX_RESTARTS", "5"))
_WATCHDOG_INTERVAL_S   = 30


# ══════════════════════════════════════════════════════════════════════
# F1 — ENGINE WATCHDOG
# ══════════════════════════════════════════════════════════════════════

class EngineWatchdog:
    """
    Supervisor thread: monitors _engine_thread every 30 s, auto-restarts
    on death/crash. Respects three safety guards before any restart:
      - open broker position  - daily loss lock  - emergency /stop
    Stops permanently after WATCHDOG_MAX_RESTARTS consecutive restarts.
    """

    def __init__(self, ctx: TradingContext, builder):
        self._ctx             = ctx
        self._builder         = builder
        self._restarts        = 0
        self._active          = True
        self._notified_guards: set = set()   # guards already alerted via Telegram
        self._thread          = threading.Thread(
            target=self._loop, name="engine-watchdog", daemon=True
        )
        self._thread.start()
        logger.info("[ENGINE WATCHDOG] Engine healthy")

    def stop(self):
        self._active = False

    def _safe_to_restart(self) -> tuple:
        import telegram.notifier as _tn
        if _tn.ENGINE_STOP_REQUESTED:
            return False, "emergency_shutdown"
        daily_limit = getattr(self._ctx.config, "DAILY_LOSS_LIMIT", -99999)
        if self._ctx.pnl <= daily_limit:
            return False, "daily_loss_lock"
        # NOTE: an open broker position no longer blocks restart.  On restart
        # engine_loop reconciliation reloads runtime_state.json and ADOPTS the
        # open position (resuming stop management), or flattens it if no state
        # exists.  Refusing to restart used to leave the position frozen and
        # unmanaged — the opposite of safe.  (Task 8)
        return True, ""

    def _loop(self):
        while self._active:
            time.sleep(_WATCHDOG_INTERVAL_S)
            if not self._active:
                break
            if _engine_thread is None or not _engine_thread.is_alive():
                reason = "thread_died"
                if self._restarts >= _WATCHDOG_MAX_RESTARTS:
                    msg = (
                        f"[ENGINE WATCHDOG] Max restarts ({_WATCHDOG_MAX_RESTARTS})"
                        " reached — manual intervention required"
                    )
                    logger.critical(msg)
                    tg_force(msg)
                    self._active = False
                    break
                ok, guard = self._safe_to_restart()
                if not ok:
                    logger.warning(
                        f"[ENGINE WATCHDOG] Thread dead — NOT restarting: {guard}"
                    )
                    if guard not in self._notified_guards:
                        self._notified_guards.add(guard)
                        tg_force(
                            f"[ENGINE WATCHDOG]\nEngine dead — NOT restarting\nReason: {guard}"
                        )
                    continue
                self._restarts += 1
                logger.warning(
                    f"[ENGINE RECOVERY] Reason={reason} "
                    f"Restart={self._restarts}/{_WATCHDOG_MAX_RESTARTS}"
                )
                tg_force(
                    f"[ENGINE RECOVERY]\n"
                    f"Reason={reason}\n"
                    f"Restart={self._restarts}/{_WATCHDOG_MAX_RESTARTS}"
                )
                start_engine(self._ctx, self._builder)


# ══════════════════════════════════════════════════════════════════════
# F2 — WEEKEND RETRAIN LOCK
# ══════════════════════════════════════════════════════════════════════

def _retrain_lock_path() -> str:
    from datetime import date
    today = date.today()
    year, week, _ = today.isocalendar()
    os.makedirs("data", exist_ok=True)
    return os.path.join("data", f".retrain_lock_{year}_W{week:02d}")


def retrain_lock_exists() -> bool:
    """True if this weekend's retrain has already been completed."""
    return os.path.exists(_retrain_lock_path())


def write_retrain_lock():
    """Call after a successful weekend retrain to prevent duplicate runs."""
    path = _retrain_lock_path()
    with open(path, "w") as f:
        f.write(datetime.now().isoformat())
    logger.info(f"[RETRAIN] Lock written: {path}")


def check_retrain_lock() -> bool:
    """Log and return True if retrain should be skipped."""
    if retrain_lock_exists():
        logger.info("[RETRAIN] Already completed this weekend — skipping")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════
# F5/F6 — EXIT ANALYTICS HELPERS
# ══════════════════════════════════════════════════════════════════════

def _classify_exit_type(reason: str, stop_loss: float, entry: float) -> str:
    if reason in ("STOP", "Stop Loss"):
        diff = stop_loss - entry
        if diff > 0.5:
            return "TRAILING_STOP"
        if abs(diff) <= 0.5:
            return "BREAK_EVEN_STOP"
        return "STOP_LOSS_HIT"
    _map = {
        "Drawdown":       "PROFIT_PROTECTION",
        "TARGET_HIT":     "TARGET_HIT",
        "TIME_EXIT_WEAK": "TIME_EXIT",
        "MANUAL":         "MANUAL_EXIT",
    }
    if reason in _map:
        return _map[reason]
    if any(x in reason for x in ("ML_", "FAST_REVERSAL", "VOLATILE_DAY",
                                   "RANGE_DAY", "TREND_DAY", "ML_EDGE")):
        return "ML_EXIT"
    return "OTHER"


# ══════════════════════════════════════════════════════════════════════
# F7 — PROFIT RETENTION SIMULATION  (analysis-only, never changes exits)
# ══════════════════════════════════════════════════════════════════════

_LADDER_LEVELS = [
    (2.0,  0.0),   # +2 pts → break-even
    (4.0,  1.0),   # +4 pts → lock +1 pt
    (6.0,  3.0),   # +6 pts → lock +3 pts
    (10.0, 6.0),   # +10 pts → lock +6 pts
]


def _profit_retention_sim(ctx: TradingContext) -> str | None:
    ea = getattr(ctx, "exit_analytics", None)
    if not ea or not ea.get("realized_list"):
        return None
    n = len(ea["realized_list"])

    actual_total = sum(ea["realized_list"])
    sim_total    = 0.0
    lines        = []

    for i in range(n):
        actual   = ea["realized_list"][i]
        mfe_pts  = ea.get("mfe_pts_list", [0] * n)[i]
        qty      = ea.get("qty_list",     [30] * n)[i]

        best_lock_pts = None
        for trigger_pts, lock_pts in reversed(_LADDER_LEVELS):
            if mfe_pts >= trigger_pts:
                best_lock_pts = lock_pts
                break

        sim_pnl = (
            max(actual, best_lock_pts * qty)
            if best_lock_pts is not None
            else actual
        )
        sim_total += sim_pnl

        act_str = f"+Rs{actual:,.0f}" if actual >= 0 else f"-Rs{abs(actual):,.0f}"
        sim_str = f"+Rs{sim_pnl:,.0f}" if sim_pnl >= 0 else f"-Rs{abs(sim_pnl):,.0f}"
        chg     = f"  → {sim_str}" if abs(sim_pnl - actual) > 1 else "  (same)"
        lines.append(f"T{i+1}: MFE {mfe_pts:+.1f}pt  {act_str}{chg}")

    diff  = sim_total - actual_total
    sign  = "+" if diff >= 0 else ""
    diff_str = f"{sign}Rs{abs(diff):,.0f}" if diff >= 0 else f"-Rs{abs(diff):,.0f}"
    act_str  = f"+Rs{actual_total:,.0f}" if actual_total >= 0 else f"-Rs{abs(actual_total):,.0f}"
    sim_str  = f"+Rs{sim_total:,.0f}" if sim_total >= 0 else f"-Rs{abs(sim_total):,.0f}"

    return (
        "📊 <b>PROFIT RETENTION SIMULATION</b>\n"
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        "Ladder: +2→BE | +4→+1pt | +6→+3pt | +10→+6pt\n\n"
        + "\n".join(lines)
        + f"\n\n<b>Current PnL     : {act_str}</b>\n"
        f"<b>Simulated PnL   : {sim_str}</b>\n"
        f"<b>Difference      : {diff_str}</b>\n"
        "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        "<i>Simulation only — no live behavior changed</i>"
    )


# ══════════════════════════════════════════════════════════════════════
# F8 — FEED HEALTH SNAPSHOT
# ══════════════════════════════════════════════════════════════════════

def _get_feed_health(broker) -> dict:
    od = {}
    try:
        od = broker.get_option_feed_diagnostics()
    except Exception:
        pass
    ws_connected = broker.ticker is not None
    last_tick    = getattr(broker, "_last_tick_time", time.time())
    return {
        "ws_connected":  ws_connected,
        "tick_age_s":    round(time.time() - last_tick, 1),
        "token_count":   len(getattr(broker, "_option_tokens", [])) + 1,
        "chain_live":    od.get("chain_tokens_live", 0),
        "chain_total":   od.get("chain_tokens_total", 0),
        "ce_oi":         od.get("ce_oi", 0),
        "pe_oi":         od.get("pe_oi", 0),
    }


def tg_bot(msg: str, key: str = "", interval: float = 10.0):
    """Rate-limited send_bot wrapper."""
    if _tg.can_send(key, interval):
        try:
            send_bot(msg)
        except Exception as e:
            logger.warning(f"[TELEGRAM] send failed: {e}")


def tg_force(msg: str):
    """Unconditional send — for trade entry/exit alerts."""
    try:
        send_bot(msg)
    except Exception as e:
        logger.warning(f"[TELEGRAM] force send failed: {e}")


def _log_feed_health(broker, ctx, builder) -> None:
    """
    Log and Telegram a single startup feed-health snapshot.
    Called once after all initialisation is complete.
    """
    ws_connected    = broker.ticker is not None
    ws_mode         = "CONNECTED" if ws_connected else "DISCONNECTED"
    tick_ts         = (
        datetime.fromtimestamp(broker._last_tick_time).strftime("%H:%M:%S")
        if broker._last_ticks else "none"
    )
    subscribed_cnt  = len(broker._option_tokens) + 1   # +1 BANKNIFTY index
    orb_status      = (
        "RECONSTRUCTED" if (ctx.live_engine.orb_done and
                            ctx.live_engine.orb_high is not None)
        else ("LIVE-BUILD" if not ctx.live_engine.orb_done else "UNAVAILABLE")
    )
    wip             = builder.current_wip()
    learner_candles = len(ctx.ml_learner.first_30min_closes)
    clf_candles     = len(ctx.live_engine._day_candles_30m)

    lines = [
        "[FEED HEALTH]",
        f"  WS mode             : {ws_mode}",
        f"  First tick received : {tick_ts}",
        f"  Subscribed tokens   : {subscribed_cnt}",
        f"  ORB status          : {orb_status}",
        f"  WIP candle active   : {'YES' if wip else 'NO'}",
        f"  Learner candles     : {learner_candles}",
        f"  Day clf candles     : {clf_candles}",
    ]
    report = "\n".join(lines)
    logger.info(report)
    tg_bot(report, key="feed_health", interval=0.0)


# ══════════════════════════════════════════════════════════════════════
# HISTORICAL DATA — fetch from Zerodha + append live candles
# ══════════════════════════════════════════════════════════════════════

_BANKNIFTY_INDEX_TOKEN = 260105   # NSE:NIFTY BANK instrument token (fixed)
_csv_write_lock    = threading.Lock()
_last_appended_ts  = None     # guard against double-append


def update_historical_data(broker, csv_path: str, lookback_days: int = 5):
    """
    Pull recent BANKNIFTY 1-minute candles from Zerodha historical API and
    append to the local CSV.  Called at startup and can be called any time.
    """
    try:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=lookback_days)

        logger.info(
            f"[HIST] Fetching BANKNIFTY 1m  {from_dt.date()} -> {to_dt.date()} ..."
        )
        raw = broker.kite.historical_data(
            _BANKNIFTY_INDEX_TOKEN, from_dt, to_dt, "minute", oi=False
        )

        if not raw:
            logger.warning("[HIST] Zerodha returned no data — using existing CSV")
            return

        new_df = pd.DataFrame(raw)
        # Zerodha timestamps may be tz-aware (Asia/Kolkata) — strip tz
        col = pd.to_datetime(new_df["date"])
        if col.dt.tz is not None:
            col = col.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        new_df["date"] = col
        new_df = new_df[["date", "open", "high", "low", "close", "volume"]]

        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

        if os.path.exists(csv_path):
            existing = pd.read_csv(csv_path)
            existing["date"] = pd.to_datetime(existing["date"], format="mixed", dayfirst=False)
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df

        combined = (
            combined
            .sort_values("date")
            .drop_duplicates("date")
            .reset_index(drop=True)
        )
        with _csv_write_lock:
            combined.to_csv(csv_path, index=False)

        logger.info(
            f"[HIST] CSV updated: {len(combined):,} rows  "
            f"latest={combined['date'].max()}"
        )

    except Exception as e:
        logger.error(f"[HIST UPDATE] Failed (non-fatal): {e}")


def _append_candle_to_csv(candle: dict, csv_path: str):
    """Append one completed live candle to the historical CSV (no duplicates)."""
    global _last_appended_ts
    ts = candle.get("ts")
    if ts is None or ts == _last_appended_ts:
        return
    # Only append real market-hours candles — not flat after-hours data
    from datetime import time as dtime
    t = ts.time() if hasattr(ts, "time") else None
    if t and not (dtime(9, 15) <= t <= dtime(15, 31)):
        return
    _last_appended_ts = ts

    try:
        row = pd.DataFrame([{
            "date":   ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "open":   candle["open"],
            "high":   candle["high"],
            "low":    candle["low"],
            "close":  candle["close"],
            "volume": candle.get("volume", 0),
        }])
        with _csv_write_lock:
            row.to_csv(csv_path, mode="a", header=False, index=False)
    except Exception as e:
        logger.debug(f"[HIST] Candle append failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# BROKER INIT
# ══════════════════════════════════════════════════════════════════════
def init_broker():
    from engine.execution.broker import ZerodhaBroker

    token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("KITE_ACCESS_TOKEN missing in .env")

    logger.info("Initializing ZerodhaBroker")

    broker = ZerodhaBroker()

    # ALLOW_BROKER_POSITION_ON_START=1 skips this gate for lunch-break resume
    # (engine_loop reconciliation then adopts the position safely).
    # In DRY_RUN/PAPER mode the live broker may hold real positions unrelated
    # to paper trades — skip the gate to avoid blocking paper sessions.
    _is_dry = os.getenv("DRY_RUN", "0") == "1"
    _resume_ok = os.getenv("ALLOW_BROKER_POSITION_ON_START", "0") == "1" or PAPER_MODE or _is_dry
    if not _resume_ok and hasattr(broker, "has_open_position") and broker.has_open_position():
        tg_force("🚨 SAFETY ALERT\nOpen position detected — engine blocked.")
        raise RuntimeError("Open broker position exists.")

    logger.info("Starting market feed")
    broker.start_feed(["NIFTY BANK"])

    # Wait up to 10 s for the first tick so the engine doesn't start
    # with a stale/flat REST price in the candle buffer.
    _nifty_token = _BANKNIFTY_INDEX_TOKEN
    _waited = 0
    while _waited < 10:
        time.sleep(1)
        _waited += 1
        if _nifty_token in broker._last_ticks:
            _ltp_check = broker._last_ticks[_nifty_token].get("last_price", 0)
            if _ltp_check > 0:
                logger.info(f"Feed ready — first tick received: BANKNIFTY={_ltp_check:.2f}")
                break
    else:
        logger.warning(
            "[BROKER] No live tick received in 10 s after WebSocket connect. "
            "This is expected outside market hours (9:15–15:30 IST). "
            "CandleBuilder will fall back to REST price until ticks flow."
        )

    # Subscribe ATM option chain (±5 strikes, CE + PE) to the live WebSocket.
    # Requires a valid BANKNIFTY spot price; safe to call even if ticks haven't
    # arrived yet — falls back to REST LTP for ATM computation.
    # Once subscribed, get_option_chain_near_atm() will return live OI values
    # from _last_ticks instead of defaulting to zero.
    try:
        broker.subscribe_options(strikes_range=5)
    except Exception as _opt_e:
        logger.warning(f"[OPTIONS FEED] Subscription failed (non-fatal): {_opt_e}")

    return broker


# ══════════════════════════════════════════════════════════════════════
# CONTEXT BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_context(broker) -> TradingContext:
    ctx = TradingContext()

    ctx.broker    = broker
    ctx.config    = Config()
    ctx.executor  = ExecutionEngine(broker, ctx.config)

    ctx.ml_learner = IntradayMLLearner()
    ctx._last_daily_reset = None
    ctx.live_engine = LiveEngine(ctx)
    ctx.allocator   = CapitalAllocator(ctx.config)

    # ── Restore prior same-day runtime state (else start fresh) ───────
    # A snapshot from a previous day is ignored by load_state(), so PnL and
    # trades_today never leak across sessions.  The engine_loop performs the
    # authoritative broker reconciliation; this just primes the counters so
    # dashboards and the daily-loss / max-trades gates are correct from cycle 1.
    _snap = load_state()
    ctx.pnl          = float(_snap.get("pnl", 0.0))
    ctx.positions    = list(_snap.get("positions", []))
    ctx.cycle_count  = 0
    ctx.trades_today = int(_snap.get("trades_today", 0))
    if _snap:
        logger.info(
            f"[STATE] Restored session: pnl={ctx.pnl:.0f} "
            f"trades_today={ctx.trades_today}"
        )

    # F5 — profit capture analytics
    ctx.exit_analytics = {
        "mfe_rs_list":  [],
        "mae_rs_list":  [],
        "mfe_pts_list": [],
        "realized_list":[],
        "capture_list": [],
        "qty_list":     [],
    }
    # F6 — exit type counts
    ctx.exit_type_counts: dict = {}

    # Diagnostics journal — observability only, zero strategy impact
    ctx.journal = TradeJournal()
    ctx.journal.log_startup(ctx.config)

    return ctx


# ══════════════════════════════════════════════════════════════════════
# CANDLE BUILDER INIT
# ══════════════════════════════════════════════════════════════════════

def init_candle_builder(broker) -> CandleBuilder:
    token = CandleBuilder.nifty_token()   # 260105 (NSE:NIFTY BANK)

    builder = CandleBuilder(broker, instrument_token=token, max_candles=300)

    if PAPER_MODE:
        builder.seed_paper_mode(HIST_CSV, n=200)
    else:
        # Warm buffer with recent historical data so ML features are
        # available from first live tick (avoids cold-start NaN features)
        builder.seed_from_csv(HIST_CSV, n=200)

    return builder


# ══════════════════════════════════════════════════════════════════════
# MAIN ENGINE LOOP
# ══════════════════════════════════════════════════════════════════════

def engine_loop(ctx: TradingContext, builder: CandleBuilder):
    logger.info("Engine loop started")
    tg_force("Engine Loop Started — Monitoring Market...")

    position          = None    # current open position dict
    entry_time        = None    # FIX-2: defined at entry, used for held_seconds
    entry_order_rec   = None    # saved order dict for trade_logger
    max_trades        = ctx.config.MAX_TRADES_PER_DAY
    consecutive_stops = 0       # auto-pause trigger
    _eod_sent         = False   # send EOD summary once at 15:30
    _last_feed_warn   = 0.0     # watchdog: last time we warned about stale feed
    _journal_id       = None    # diagnostics journal id for current open trade
    _last_atm_check   = 0.0     # epoch time of last ATM drift check
    _last_opt_diag    = 0.0     # epoch time of last [OPTION FEED] log
    _last_heartbeat   = 0.0     # epoch time of last alive-ping to Telegram
    ltp_current       = 0.0     # last known BANKNIFTY spot LTP (avoids UnboundLocalError on first tick)
    _signal_first_ts  = None    # ts when this signal was first produced (entry delay tracking)
    _last_exit_symbol = None    # symbol of most-recently exited ML trade
    _last_exit_epoch  = 0.0     # epoch time of that exit (for same-symbol cooldown)
    scalp_position        = None                              # active scalp trade dict (flat when main trades)
    _scalp_ltp_history    = collections.deque(maxlen=120)    # (datetime, float) pairs — 120s of BANKNIFTY spot
    _scalp_trades_today   = 0                                # scalp-only exit counter (capped at SCALP_MAX_TRADES)

    # ══════════════════════════════════════════════════════════════════
    # RESTART RECOVERY + BROKER RECONCILIATION  (Tasks 3, 4, 8)
    # Runs on every engine_loop start (process restart AND watchdog thread
    # restart).  Reloads runtime_state.json (same-day only) and reconciles
    # against the live broker so a position is never left unmanaged.
    # ══════════════════════════════════════════════════════════════════
    try:
        _snap = load_state()
        if _snap:
            ctx.pnl             = float(_snap.get("pnl", ctx.pnl))
            ctx.trades_today    = int(_snap.get("trades_today", ctx.trades_today))
            ctx.positions       = list(_snap.get("positions", ctx.positions))
            _scalp_trades_today = int(_snap.get("scalp_trades_today", 0))
        _restored_pos   = deserialize_position(_snap.get("open_position"))   if _snap else None
        _restored_scalp = deserialize_position(_snap.get("scalp_position"))  if _snap else None

        try:
            # In DRY_RUN mode the live broker may hold real positions unrelated
            # to paper trades — skip reconciliation to avoid false PAUSING.
            _is_dry_run = (os.getenv("DRY_RUN", "0") == "1"
                           or getattr(ctx.config, "DRY_RUN", False)
                           or getattr(ctx.executor, "is_dry_run", False))
            if _is_dry_run:
                _broker_open = False
                logger.info("[RECOVERY] DRY_RUN mode — skipping live broker position check")
            else:
                _broker_open = ctx.broker.has_open_position()
        except Exception as _bo_e:
            logger.warning(f"[RECOVERY] broker position check failed: {_bo_e}")
            _broker_open = False

        if _broker_open and _restored_pos and _restored_pos.get("symbol"):
            # Case A (main): broker position + saved state → resume management.
            position        = _restored_pos
            entry_time      = position.get("entry_ts")
            entry_order_rec = {"symbol": position["symbol"],
                               "price":  position.get("entry", 0.0),
                               "qty":    position.get("qty", 0)}
            logger.critical(
                f"[RECOVERY] Adopted open position {position['symbol']} "
                f"entry={position.get('entry',0):.2f} SL={position.get('stop_loss',0):.2f} "
                f"max_pnl={position.get('max_pnl',0):.0f} — management resumed"
            )
            try:
                tg_force(
                    f"♻️ RECOVERY: resumed management of {position.get('side','')} "
                    f"{position['symbol']}\nSL={position.get('stop_loss',0):.2f} "
                    f"maxPnL={position.get('max_pnl',0):.0f}"
                )
            except Exception:
                pass
            # Verify the broker protective stop still matches; repair if not.
            try:
                _sl_verify_or_repair(ctx, position)
            except Exception as _vr_e:
                logger.critical(f"[BROKER SL] verify/repair failed: {_vr_e}")

        elif _broker_open and _restored_scalp and _restored_scalp.get("symbol"):
            # Case A (scalp): broker position + saved scalp state → resume.
            scalp_position = _restored_scalp
            logger.critical(
                f"[RECOVERY] Adopted scalp position {scalp_position['symbol']} "
                f"SL={scalp_position.get('stop_loss',0):.2f} — management resumed"
            )

        elif _broker_open:
            # Case B: broker holds a position we have NO state for.  Never run
            # blind — pause new entries and flatten the orphan safely.
            logger.critical(
                "[RECOVERY] Broker position with NO saved state — "
                "PAUSING entries and flattening orphan"
            )
            try:
                import telegram.notifier as _tn
                _tn.ENGINE_PAUSED = True
            except Exception:
                pass
            try:
                for _p in ctx.broker.get_positions():
                    _q   = int(_p.get("quantity", 0))
                    if _q == 0:
                        continue
                    _sym  = _p.get("tradingsymbol", "")
                    _side = "CE" if _sym.endswith("CE") else ("PE" if _sym.endswith("PE") else "CE")
                    # Cancel any dangling protective stop first so it cannot fire
                    # after we flatten (which would open a short).
                    _dangling = ctx.executor.find_open_stop_order(_sym)
                    if _dangling:
                        ctx.executor.cancel_protective_stop(_dangling["order_id"])
                        logger.info(f"[BROKER SL CANCELLED]\n{_sym} (orphan) id={_dangling['order_id']}")
                    ctx.executor.execute_exit(symbol=_sym, qty=abs(_q), side=_side)
                tg_force(
                    "⚠️ RECOVERY: orphan broker position flattened.\n"
                    "Entries PAUSED — send /resume to re-enable."
                )
            except Exception as _flat_e:
                logger.critical(f"[RECOVERY] flatten failed: {_flat_e}")

        elif _restored_pos or _restored_scalp:
            # Broker is flat but state had a position → it already closed.
            logger.info("[RECOVERY] Saved position but broker flat — treated as already closed")
    except Exception as _rec_e:
        logger.critical(f"[RECOVERY] reconciliation error (non-fatal): {_rec_e}")

    def _status_cb():
        import telegram.notifier as _tn
        paused   = "PAUSED" if _tn.ENGINE_PAUSED else "ACTIVE"
        stop_req = " | STOP REQUESTED" if _tn.ENGINE_STOP_REQUESTED else ""
        ltp      = builder.ltp() or 0
        pos_str  = (f"IN TRADE: {position['symbol']} @ {position['entry']:.1f}"
                    if position else "NO POSITION")
        ce_thr   = f"{_tn.CE_THRESHOLD_OVERRIDE:.2f}" if _tn.CE_THRESHOLD_OVERRIDE else "model"
        pe_thr   = f"{_tn.PE_THRESHOLD_OVERRIDE:.2f}" if _tn.PE_THRESHOLD_OVERRIDE else "model"
        return (
            f"<b>Engine: {paused}{stop_req}</b>\n"
            f"BANKNIFTY LTP: {ltp:.1f}\n"
            f"{pos_str}\n"
            f"PnL: {ctx.pnl:.0f} | Trades: {ctx.trades_today}\n"
            f"CE thr: {ce_thr}  PE thr: {pe_thr}"
        )

    while True:
        try:
            ts  = datetime.now()
            now = ts.time()
            today = ts.date()

            if now.hour == 9 and now.minute == 15:
                if ctx._last_daily_reset != today:
                    logger.info("[DAILY RESET]")

                    ctx.ml_learner.reset_day()
                    _scalp_trades_today = 0   # reset scalp-only counter

                    ctx._last_daily_reset = today
            # ── Poll Telegram: buttons + text commands (every cycle) ──
            poll_commands(status_cb=_status_cb)

            # ── Feed watchdog: alert if WebSocket silent >60s in market hours ──
            _mkt_open  = __import__("datetime").time(9, 15)
            _mkt_close = __import__("datetime").time(15, 30)
            if _mkt_open <= now <= _mkt_close:
                # Only watch after first tick received (skip startup grace period)
                if getattr(ctx.broker, "_last_ticks", {}):
                    _last_tick = getattr(ctx.broker, "_last_tick_time", time.time())
                    _stale_s   = time.time() - _last_tick
                    if _stale_s > 60 and time.time() - _last_feed_warn > 120:
                        _last_feed_warn = time.time()
                        logger.warning(f"[WATCHDOG] Feed stale {_stale_s:.0f}s — reconnecting")
                        tg_bot(
                            f"⚠️ Feed stale ({_stale_s:.0f}s) — reconnecting...",
                            key="watchdog", interval=120.0
                        )
                        try:
                            ctx.broker.start_feed(["NIFTY BANK"])
                        except Exception as _wde:
                            logger.error(f"[WATCHDOG] Reconnect failed: {_wde}")

            # ── Dynamic ATM re-subscription (every 5 min, market hours) ──
            # When BANKNIFTY drifts ≥100 pts the subscribed option-chain becomes
            # stale.  Re-subscribe without restarting the engine.
            if (time.time() - _last_atm_check > 300
                    and _mkt_open <= now <= _mkt_close):
                _last_atm_check = time.time()
                try:
                    _refreshed = ctx.broker.refresh_atm_if_drifted(drift_points=100)
                    if _refreshed:
                        logger.info(
                            f"[ATM DRIFT] Re-subscribed options; "
                            f"new ATM={ctx.broker._subscribed_atm}"
                        )
                except Exception as _atm_e:
                    logger.warning(f"[ATM DRIFT] Check failed (non-fatal): {_atm_e}")

            # ── Alive heartbeat (every 15 min in market hours) ───────
            # New message (not edit) so the phone buzzes to confirm the bot is live.
            if (time.time() - _last_heartbeat > 900
                    and _mkt_open <= now <= _mkt_close):
                _last_heartbeat = time.time()
                import telegram.notifier as _tn
                _hb_pos = (
                    f"IN TRADE: {position['symbol']} @ {position['entry']:.1f}"
                    if position else "No open position"
                )
                _hb_paused = " | PAUSED" if _tn.ENGINE_PAUSED else ""
                tg_force(
                    f"💓 Engine alive — {ts.strftime('%H:%M')}\n"
                    f"BANKNIFTY {ltp_current:,.1f} | PnL ₹{ctx.pnl:+.0f}\n"
                    f"{_hb_pos}{_hb_paused}"
                )

            # ── [OPTION FEED] periodic diagnostics (every 60 s) ──────
            if (time.time() - _last_opt_diag > 60
                    and _mkt_open <= now <= _mkt_close):
                _last_opt_diag = time.time()
                try:
                    _od = ctx.broker.get_option_feed_diagnostics()
                    logger.info(
                        f"[OPTION FEED] "
                        f"ce_oi={_od.get('ce_oi', 0):,}  "
                        f"pe_oi={_od.get('pe_oi', 0):,}  "
                        f"chain_tokens_live={_od.get('chain_tokens_live', 0)}"
                        f"/{_od.get('chain_tokens_total', 0)}"
                    )
                except Exception as _od_e:
                    logger.warning(f"[OPTION FEED] Diagnostics failed (non-fatal): {_od_e}")

            # ── Engine stop requested by user (/stop command) ─────────
            import telegram.notifier as _tn
            if _tn.ENGINE_STOP_REQUESTED and position is None:
                logger.info("[CONTROL] /stop received — engine halted cleanly")
                tg_force("Engine halted by /stop command.")
                break

            # ── Process latest WebSocket tick into candle buffer ──────
            # FIX-3: replaces CSV read every loop
            new_candle_ready = builder.process_tick(ts)

            # Persist each completed candle to historical CSV so the
            # dataset grows continuously during the trading session.
            if new_candle_ready:
                _append_candle_to_csv(builder.latest_candle(), HIST_CSV)

            df_window = builder.get_window(120)

            if df_window is None or len(df_window) < 26:
                time.sleep(0.5)
                ctx.cycle_count += 1
                continue

            ltp_current = builder.ltp() or df_window["close"].iloc[-1]

            # Use the live WIP (in-progress) candle for OHLC so that
            # intrabar highs/lows are visible to the learner and VWAP.
            # Falling back to the last completed candle only if WIP is absent.
            _wip = builder.current_wip()
            if _wip is not None:
                latest_candle = {
                    "open":   float(_wip["open"]),
                    "high":   float(_wip["high"]),
                    "low":    float(_wip["low"]),
                    "close":  ltp_current,
                    "volume": int(_wip.get("volume", 0)),
                    "ts":     ts,
                }
            else:
                latest_candle = {
                    "open":   float(df_window["open"].iloc[-1]),
                    "high":   float(df_window["high"].iloc[-1]),
                    "low":    float(df_window["low"].iloc[-1]),
                    "close":  ltp_current,
                    "volume": int(df_window["volume"].iloc[-1]) if "volume" in df_window.columns else 0,
                    "ts":     ts,
                }

            df_decision_window = df_window
            try:
                _live_row = {
                    "ts": latest_candle["ts"],
                    "open": latest_candle["open"],
                    "high": latest_candle["high"],
                    "low": latest_candle["low"],
                    "close": latest_candle["close"],
                    "volume": latest_candle["volume"],
                }
                if not df_window.empty:
                    _last_ts = pd.to_datetime(df_window["ts"].iloc[-1])
                    _live_ts = pd.to_datetime(_live_row["ts"])
                    if _live_ts > _last_ts:
                        df_decision_window = pd.concat(
                            [df_window, pd.DataFrame([_live_row])],
                            ignore_index=True,
                        ).tail(120).reset_index(drop=True)
                    elif _live_ts == _last_ts:
                        df_decision_window = df_window.copy()
                        for _k in ("open", "high", "low", "close", "volume"):
                            df_decision_window.at[df_decision_window.index[-1], _k] = _live_row[_k]
            except Exception as _dw_e:
                logger.debug(f"[DECISION WINDOW] live candle merge failed: {_dw_e}")

            market_data = {
                "candle":    latest_candle,
                "df_window": df_decision_window,
                # Legacy keys for dashboard compat
                "candles": df_decision_window["close"].tolist(),
                "highs":   df_decision_window["high"].tolist(),
                "lows":    df_decision_window["low"].tolist(),
            }

            # ── Run decision engine ───────────────────────────────────
            decision = ctx.live_engine.step(market_data, ts)

            # Track when this signal was first generated (for entry-delay logging)
            if decision is not None and _signal_first_ts is None:
                _signal_first_ts = ts
            elif decision is None:
                _signal_first_ts = None

            # ══════════════════════════════════════════════════════════
            # POSITION MANAGEMENT — runs every cycle, NOT in except block
            # FIX-1: was inside except block — completely broken before
            # ══════════════════════════════════════════════════════════

            if position is not None:
                # CRITICAL FIX: use option premium LTP, NOT BANKNIFTY spot.
                # builder.ltp() returns BANKNIFTY spot (~52000) which is always
                # above the option target (~42), causing TARGET_HIT every cycle.
                _opt_ltp = ctx.broker.ltp(position["symbol"])

                if _opt_ltp and _opt_ltp > 0:
                    pos_ltp = _opt_ltp
                    position["_last_ltp"] = _opt_ltp
                else:
                    logger.warning(
                        f"[LTP MISSING] {position['symbol']} using last known LTP"
                    )
                    pos_ltp = position.get("_last_ltp", position["entry"])
                held_seconds = (ts - entry_time).total_seconds() if entry_time else 0

                # ── Track MAE (maximum adverse excursion) ────────────
                _cycle_pnl = (pos_ltp - position["entry"]) * position["qty"]
                position["min_pnl"] = min(position.get("min_pnl", 0.0), _cycle_pnl)

                # ── Diagnostics tick ─────────────────────────────────
                if _journal_id:
                    ctx.journal.on_tick(_journal_id, pos_ltp, position)

                # F1: replay tick (observational — no effect on pos mgmt)
                if position.get("_replay"):
                    try:
                        position["_replay"].on_tick(ts, pos_ltp, position)
                    except Exception:
                        pass

                # ── Live trade card update (every 2s) ────────────────
                if _tg.can_send("trade_live", 2.0) and entry_time is not None:
                    try:
                        live_msg = format_trade_live(position, pos_ltp, entry_time)
                        update_trade_live(live_msg)
                    except Exception as _lm_e:
                        logger.debug(f"[TG] live update failed: {_lm_e}")

                # ── Trailing / profit_manager via live_engine.check_exit ──
                _sl_before = position.get("stop_loss", 0.0)
                exit_flag, exit_reason = ctx.live_engine.check_exit(
                    position, pos_ltp, held_seconds
                )

                # ── Minimum ₹250 profit gate — never exit on profit-taking
                #    until this trade has made at least ₹250
                if exit_flag and exit_reason in ("TARGET_HIT", "Drawdown"):
                    _cur_pnl = (pos_ltp - position["entry"]) * position["qty"]
                    if _cur_pnl < 250:
                        logger.info(
                            f"[GATE] MIN_PROFIT blocked {exit_reason}: "
                            f"pnl={_cur_pnl:.0f} < 250 (holding)"
                        )
                        exit_flag  = False
                        exit_reason = ""

                # ── Hard stop loss (belt + suspenders) ────────────────
                if pos_ltp <= position.get("stop_loss", position["entry"] * 0.90):
                    exit_flag  = True
                    exit_reason = "STOP"

                # ── Manual exit from Telegram button ──────────────────
                # FIX-6: MANUAL_EXIT_REQUESTED now actually reachable
                import telegram.notifier as _tn
                if _tn.MANUAL_EXIT_REQUESTED:
                    exit_flag  = True
                    exit_reason = "MANUAL"
                    _tn.MANUAL_EXIT_REQUESTED = False

                # ── Ladder raised the stop → push it to the broker SL-M ──
                # Only when we are NOT exiting this cycle (atomic modify).
                if not exit_flag and position.get("stop_loss", 0.0) > _sl_before + 1e-6:
                    _sl_modify(ctx, position, position["stop_loss"])

                # ── Execute exit ───────────────────────────────────────
                if exit_flag:
                    # Task 5 — audit log proving which stop was active at exit.
                    if exit_reason in ("STOP", "Stop Loss"):
                        _log_stop_audit("STOP HIT", pos_ltp, position)

                    # ── Broker stop reconciliation before exiting ─────────
                    # Cancel the protective SL-M.  If it ALREADY fired (broker
                    # flat), the SL did the exit — do NOT place a second sell
                    # (would open a short).  Otherwise place the market exit.
                    _sl_already_filled = False
                    _sl_fill_price     = 0.0
                    if position.get("sl_order_id"):
                        _sl_info = ctx.executor.get_order_info(position["sl_order_id"])
                        ctx.executor.cancel_protective_stop(position["sl_order_id"])
                        logger.info(
                            f"[BROKER SL CANCELLED]\n{position['symbol']}  "
                            f"id={position['sl_order_id']}"
                        )
                        if (_sl_info and _sl_info.get("status") == "COMPLETE"
                                and _sl_info.get("average_price", 0) > 0
                                and ctx.executor.verify_flat(position["symbol"])):
                            _sl_already_filled = True
                            _sl_fill_price     = _sl_info["average_price"]
                            logger.critical(
                                f"[BROKER SL FILLED] {position['symbol']} "
                                f"fill={_sl_fill_price:.2f} — broker stop executed the exit"
                            )
                        position["sl_order_id"] = None

                    # F5: capture exit signal price before market order
                    _exit_signal_ltp = pos_ltp

                    if _sl_already_filled:
                        # Broker SL-M already closed the position — reuse its fill,
                        # do NOT send another order.  Clear the executor's
                        # duplicate-order guard that execute_exit would normally
                        # clear, so the next entry is not blocked.
                        exit_order = None
                        exit_price = _sl_fill_price
                        ctx.executor._active_order_id = None
                    else:
                        exit_order = ctx.executor.execute_exit(
                            symbol=position["symbol"],
                            qty=position["qty"],
                            side=position["side"],     # both CE & PE close with SELL
                        )

                        exit_price = (
                            exit_order["price"] if exit_order and exit_order["price"] > 0
                            else pos_ltp
                        )

                    # Task 7 — VIRTUAL STOP: the stop is a trigger level, not a
                    # resting broker order, so the market fill can be BELOW the
                    # trigger on option gaps.  Warn when slippage is material.
                    if exit_reason in ("STOP", "Stop Loss"):
                        _slip_pts = _exit_signal_ltp - exit_price
                        _warn_pts = float(os.getenv("SLIPPAGE_WARN_PTS", "1.5"))
                        if _slip_pts > _warn_pts:
                            logger.warning(
                                f"[SLIPPAGE] {position['symbol']} stop slippage "
                                f"{_slip_pts:.2f}pt (trigger={_exit_signal_ltp:.2f} "
                                f"fill={exit_price:.2f} SL={position.get('stop_loss',0):.2f})"
                            )

                    pnl = (exit_price - position["entry"]) * position["qty"]

                    ctx.pnl         += pnl
                    ctx.positions.append(pnl)

                    # ── Persist trade to CSV ───────────────────────────
                    try:
                        import telegram.notifier as _tn
                        log_trade(
                            entry_order  = entry_order_rec or {},
                            exit_price   = exit_price,
                            exit_reason  = exit_reason,
                            position     = position,
                            entry_time   = entry_time,
                            exit_time    = ts,
                            ce_threshold = _tn.CE_THRESHOLD_OVERRIDE or 0.62,
                            pe_threshold = _tn.PE_THRESHOLD_OVERRIDE or 0.66,
                        )
                    except Exception as _log_e:
                        logger.warning(f"[TRADE LOG] Write failed: {_log_e}")

                    # FIX-9: record trade result in learner
                    ctx.ml_learner.record_trade_result(
                        side=position["side"],
                        pnl=pnl,
                        ml_prob=position.get("ml_prob", 0.5),
                        features=position.get("features", {}),
                        reason=exit_reason,
                    )

                    # ── AI BRAIN review (advisory) after 2+ consecutive losses ──
                    # OpenAI-compatible LLM analyses recent trades and trims the
                    # weaker side's multiplier (more selective only — never sizes
                    # up or loosens stops). Runs in a daemon thread so it never
                    # blocks the trading loop. No-op if LLM_API_KEY is unset.
                    if getattr(ctx.ml_learner, "ai_review_pending", False):
                        def _do_ai_review(_learner=ctx.ml_learner,
                                          _trades=list(ctx.ml_learner.trades_today),
                                          _regime=position.get("regime", "UNKNOWN"),
                                          _spot=ltp_current):
                            try:
                                from ml.ml_intraday_learner import run_ai_brain_review
                                _txt = run_ai_brain_review(_learner, _trades, _regime, _spot)
                                if _txt:
                                    tg_force("🧠 <b>AI BRAIN</b>\n" + _txt)
                            except Exception as _aie:
                                logger.warning(f"[AI BRAIN] review failed: {_aie}")
                        threading.Thread(target=_do_ai_review, daemon=True,
                                         name="ai_brain").start()

                    _mfe_pts = position.get("max_pnl", 0.0) / max(position["qty"], 1)
                    _mae_pts = position.get("min_pnl", 0.0) / max(position["qty"], 1)

                    # F5 — profit capture analytics
                    _mfe_rs   = position.get("max_pnl", 0.0)
                    _mae_rs   = position.get("min_pnl", 0.0)
                    _capture  = round(pnl / _mfe_rs, 3) if _mfe_rs > 0.01 else 0.0
                    ctx.exit_analytics["mfe_rs_list"].append(_mfe_rs)
                    ctx.exit_analytics["mae_rs_list"].append(_mae_rs)
                    ctx.exit_analytics["mfe_pts_list"].append(_mfe_pts)
                    ctx.exit_analytics["realized_list"].append(pnl)
                    ctx.exit_analytics["capture_list"].append(_capture)
                    ctx.exit_analytics["qty_list"].append(position["qty"])

                    # F6 — exit type classification
                    _exit_type = _classify_exit_type(
                        exit_reason,
                        position.get("stop_loss", 0.0),
                        position.get("entry", 0.0),
                    )
                    ctx.exit_type_counts[_exit_type] = (
                        ctx.exit_type_counts.get(_exit_type, 0) + 1
                    )

                    exit_msg = format_trade_exit({
                        "symbol":        position["symbol"],
                        "side":          position["side"],
                        "entry_price":   position["entry"],
                        "exit_price":    exit_price,
                        "trigger_price": pos_ltp,
                        "qty":           position["qty"],
                        "pnl":           pnl,
                        "ml_prob":       position.get("ml_prob", 0),
                        "reason":        exit_reason,
                        "regime":        position.get("regime", "UNKNOWN"),
                        "stop_level":    position.get("stop_loss", 0),
                        "mfe_pts":       _mfe_pts,
                        "mae_pts":       _mae_pts,
                        "held_seconds":  held_seconds,
                    })
                    # Freeze live trade card to final exit summary (stays in chat),
                    # then post exit summary to channel as a separate message.
                    freeze_trade_message(exit_msg)
                    send_trade_channel(exit_msg)
                    # Repost engine dashboard so it appears fresh at bottom
                    try:
                        _ms_exit = ctx.live_engine.get_market_state(ts)
                        try:
                            _ms_exit["feed_health"] = _get_feed_health(ctx.broker)
                        except Exception:
                            pass
                        _eng_msg = format_engine_dashboard(ctx, _ms_exit, ltp_current)
                        repost_engine_dashboard(_eng_msg)
                    except Exception as _edash_e:
                        logger.debug(f"[TG] engine repost failed: {_edash_e}")

                    _held_str = f"{int(held_seconds)//60}m {int(held_seconds)%60:02d}s"
                    logger.info(
                        f"[EXIT] {exit_reason} | {position['symbol']} | "
                        f"pnl={pnl:.2f} | MFE={_mfe_pts:+.1f}pts | "
                        f"MAE={_mae_pts:+.1f}pts | held={_held_str} | "
                        f"total={ctx.pnl:.2f}"
                    )

                    # ── Diagnostics on_exit ───────────────────────────
                    if _journal_id:
                        try:
                            # HTF state: recompute whether 30m gate would block
                            _closes_j = df_window["close"].values if df_window is not None else []
                            _htf_would_block = None
                            if len(_closes_j) >= 30:
                                import numpy as _np
                                _htf_would_block = not (
                                    float(_np.mean(_closes_j[-15:])) >
                                    float(_np.mean(_closes_j[-30:]))
                                )
                            ctx.journal.on_exit(
                                jid         = _journal_id,
                                position    = position,
                                exit_price  = exit_price,
                                exit_reason = exit_reason,
                                pnl         = pnl,
                                exit_ts     = ts,
                                htf_would_block = _htf_would_block,
                            )
                        except Exception as _je:
                            logger.warning(f"[JOURNAL] on_exit failed (non-fatal): {_je}")
                        _journal_id = None

                    # F1: finalize replay timeline + send to Telegram
                    if position is not None and position.get("_replay"):
                        try:
                            position["_replay"].on_exit(
                                ts, exit_price, exit_reason, pnl, _mae_pts,
                                position=position,
                            )
                            position["_replay"].save(str(ctx.trades_today))
                            _replay_msg = position["_replay"].format_timeline()
                            tg_force(_replay_msg)
                        except Exception as _r_e:
                            logger.warning(f"[REPLAY] Finalize failed: {_r_e}")

                    # F5: log complete round-trip slippage record
                    if position is not None:
                        try:
                            log_slip(
                                symbol       = position["symbol"],
                                side         = position["side"],
                                entry_signal = position.get("_signal_entry_ltp", 0.0),
                                entry_fill   = position["entry"],
                                exit_signal  = _exit_signal_ltp,
                                exit_fill    = exit_price,
                                qty          = position["qty"],
                                entry_time   = entry_time,
                            )
                        except Exception as _slip_e:
                            logger.debug(f"[SLIP] Log failed: {_slip_e}")

                    _exited_symbol  = position["symbol"]
                    position        = None
                    entry_time      = None
                    entry_order_rec = None
                    # Stamp exit time for re-entry cooldown in live_engine
                    ctx.live_engine._last_exit_ts = time.time()
                    # Same-symbol cooldown tracking
                    _last_exit_symbol = _exited_symbol
                    _last_exit_epoch  = time.time()
                    # Persist closed state immediately (open_position -> None)
                    save_state(ctx, None, scalp_position, _scalp_trades_today)

                    # Auto-pause after 2 consecutive LOSING stops
                    # A profitable trailing-stop exit is NOT a consecutive loss.
                    if exit_reason in ("STOP", "Stop Loss", "Drawdown") and pnl < 0:
                        consecutive_stops += 1
                        logger.info(
                            f"[GATE] Consecutive losing stops: {consecutive_stops}"
                        )
                        if consecutive_stops >= 2:
                            import telegram.notifier as _tn
                            _tn.ENGINE_PAUSED = True
                            logger.warning(
                                "[AUTO-PAUSE] 2 consecutive losing stops — entries blocked."
                                " Restart or /resume to re-enable."
                            )
                            tg_force(
                                "AUTO-PAUSE: 2 consecutive losing stops hit.\n"
                                "Send /resume to re-enable entries."
                            )
                    else:
                        consecutive_stops = 0

            # ══════════════════════════════════════════════════════════
            # DAILY LOSS KILL SWITCH — every cycle
            # FIX-11: was inside except block
            # ══════════════════════════════════════════════════════════

            daily_loss_limit = ctx.config.DAILY_LOSS_LIMIT
            if ctx.pnl <= daily_loss_limit:
                tg_force(
                    f"🛑 DAILY LOSS LIMIT HIT ({ctx.pnl:.0f}) — SYSTEM STOPPED"
                )
                logger.critical(
                    f"[KILL SWITCH] Daily loss limit breached: {ctx.pnl:.2f}"
                )
                break

            # ══════════════════════════════════════════════════════════
            # ENTRY — only if no open position
            # ══════════════════════════════════════════════════════════

            if decision is not None and position is None:

                # /pause command — block all new entries
                import telegram.notifier as _tn
                if _tn.ENGINE_PAUSED:
                    logger.debug("[GATE] Engine paused — skipping entry")
                    ctx.live_engine.record_block("PAUSED")
                    decision = None

            if decision is not None and position is None:

                # Telegram threshold overrides (/ce X /pe X)
                import telegram.notifier as _tn
                _side    = decision.get("side", "")
                _ml_prob = decision.get("ml_prob", 0.0)
                _thr_ov  = (_tn.CE_THRESHOLD_OVERRIDE if _side == "CE"
                            else _tn.PE_THRESHOLD_OVERRIDE)
                if _thr_ov is not None and _ml_prob < _thr_ov:
                    logger.info(
                        f"[GATE] TG threshold override: {_side} prob={_ml_prob:.2f}"
                        f" < override={_thr_ov:.2f}"
                    )
                    ctx.live_engine.record_block("TG_THRESHOLD")
                    decision = None

            if decision is not None and position is None:

                # Same-symbol re-entry cooldown after any exit on same option.
                # Prevents immediate re-entry into a reversing option after a
                # stop. Raised 90s->300s (config): trade #3 on 2026-06-17
                # re-entered 24100CE 3 min after a stop and lost Rs481.
                _SAME_SYMBOL_COOLDOWN = getattr(ctx.config, "SAME_SYMBOL_COOLDOWN", 300)
                _cand_sym, _ = ctx.broker.get_atm_option(decision.get("side", "CE"))
                if (
                    _cand_sym is not None
                    and _cand_sym == _last_exit_symbol
                    and (time.time() - _last_exit_epoch) < _SAME_SYMBOL_COOLDOWN
                ):
                    _remaining = int(_SAME_SYMBOL_COOLDOWN - (time.time() - _last_exit_epoch))
                    logger.info(
                        f"[GATE] Same-symbol cooldown: {_cand_sym} "
                        f"({_remaining}s remaining)"
                    )
                    ctx.live_engine.record_block("SAME_SYMBOL_COOLDOWN")
                    decision = None

            if decision is not None and position is None:

                # FIX-8: max trades per day guard
                if ctx.trades_today >= max_trades:
                    logger.debug(f"[GATE] Max trades reached: {ctx.trades_today}")
                    ctx.live_engine.record_block("MAX_TRADES")
                    decision = None

            if decision is not None and position is None:

                side   = decision["side"]
                symbol, _price_ignored = ctx.broker.get_atm_option(side)

                if symbol is None:
                    logger.warning("[ENTRY] get_atm_option returned None symbol")
                    decision = None

            if decision is not None and position is None:

                side   = decision["side"]
                symbol, _ = ctx.broker.get_atm_option(side)

                # FIX-4: lot_size from instrument map, not from get_atm_option()
                lot_size = ctx.executor.get_lot_size(symbol)

                current_price = builder.ltp() or 0.0

                # ── OI wall filter ────────────────────────────────────
                # FIX-10: was never called
                try:
                    atm_strike = round(current_price / 100) * 100
                    option_chain = ctx.broker.get_option_chain_near_atm(strikes_range=5)

                    if has_oi_wall(option_chain, atm_strike, side):
                        logger.info(
                            f"[GATE] OI wall blocked {side} entry | ATM={atm_strike}"
                        )
                        ctx.live_engine.record_block("OI_WALL")
                        decision = None
                except Exception as _oi_e:
                    logger.warning(f"[OI FILTER] Error (non-fatal): {_oi_e}")

            if decision is not None and position is None:

                side     = decision["side"]
                symbol, _ = ctx.broker.get_atm_option(side)
                lot_size  = ctx.executor.get_lot_size(symbol)

                # Position sizing via allocator (SINGLE computation — the
                # result is reused below; do NOT recompute with a fallback).
                atr_val = decision.get("features", {}).get("atr", current_price * 0.01)
                qty = ctx.allocator.size_position(
                    capital=ctx.config.INITIAL_CAPITAL,
                    ml_prob=decision["ml_prob"],
                    atr=atr_val,
                    price=builder.ltp() or 0,
                    lot_size=lot_size,
                    current_pnl=ctx.pnl,
                )

                # Respect allocator rejection.  A 0 return means "do not trade".
                # The old `or lot_size` fallback turned a reject into lot_size
                # lots (qty*lot_size = 4225 units) — a catastrophic oversize.
                if not qty or qty <= 0:
                    logger.info("[GATE] Allocator returned 0 — skipping trade (ALLOC_ZERO)")
                    ctx.live_engine.record_block("ALLOC_ZERO")
                    decision = None

            if decision is not None and position is None:

                side     = decision["side"]
                symbol, _ = ctx.broker.get_atm_option(side)
                lot_size  = ctx.executor.get_lot_size(symbol)
                # qty was computed and validated in the block above — single
                # source of truth, no fallback recompute here.

                # ── Telegram confirmation (3s timeout → auto-execute) ──
                _confirmed = ask_trade_permission(
                    side    = side,
                    price   = builder.ltp() or 0.0,
                    ml_prob = decision["ml_prob"],
                    stop    = decision.get("stop_loss", 0.0),
                    target  = decision.get("target",    0.0),
                )
                if not _confirmed:
                    logger.info(f"[GATE] Trade SKIPPED by user (Telegram SKIP)")
                    ctx.live_engine.record_block("TG_SKIP")
                    decision = None  # fall through cleanly

                if decision is not None:
                    pass  # proceed to execute below

                # F5: capture option LTP just before market order (slippage baseline)
                _signal_opt_ltp = 0.0
                if decision is not None:
                    try:
                        _signal_opt_ltp = ctx.broker.ltp(symbol) or 0.0
                    except Exception:
                        pass

                order = (
                    ctx.executor.execute_entry(symbol, side, qty * lot_size)
                    if decision is not None else None
                )

                if order and order.get("price", 0) > 0:

                    # Premium-space stops computed from the ACTUAL fill premium.
                    # (The signal's spot-based stop is unusable here: position
                    #  entry/LTP are option premiums, not spot — mixing them
                    #  made every trade instant-stop.)
                    fill_premium = order["price"]
                    atr_val = decision.get("features", {}).get("atr", 1.0)
                    regime  = decision.get("regime", "UNKNOWN")
                    stop_loss, target, _stop_pct = compute_entry_stops(
                        fill_premium, atr_val, regime
                    )

                    position = {
                        "symbol":   symbol,
                        "side":     side,
                        "qty":      order["qty"],
                        "lot_size": lot_size,
                        "entry":    order["price"],
                        "stop_loss": stop_loss,
                        "target":   target,
                        "max_pnl":  0.0,
                        "min_pnl":  0.0,   # tracks MAE (maximum adverse excursion)
                        "ml_prob":  decision.get("ml_prob", 0.0),
                        "features": decision.get("features", {}),
                        "regime":   decision.get("regime", "UNKNOWN"),
                        "reason":   decision.get("reason", ""),
                        "entry_ts": ts,   # for held-time display in dashboard
                        "sl_order_id": None,   # broker protective SL-M id (set below)
                    }
                    entry_time      = ts    # FIX-2
                    entry_order_rec = order
                    ctx.trades_today += 1

                    # ── Place broker-side protective SL-M immediately ──────
                    # True stop enforcement — no longer depends on the polling
                    # loop seeing the price (eliminates virtual-stop gap risk).
                    _sl_create(ctx, position)

                    # Persist new open position immediately (survives restart)
                    save_state(ctx, position, scalp_position, _scalp_trades_today)
                    _signal_first_ts = None   # reset for next trade

                    # F1: start replay timeline (observational)
                    try:
                        _ms_entry = ctx.live_engine.get_market_state(ts)
                        position["_replay"] = TradeReplay(
                            position, ts, ltp_current, _ms_entry
                        )
                        # also store signal price for slippage (F5)
                        position["_signal_entry_ltp"] = _signal_opt_ltp
                    except Exception as _rep_e:
                        logger.debug(f"[REPLAY] Init failed: {_rep_e}")

                    # Fix empty journal: wire on_entry so diagnostics CSV populates
                    try:
                        _ms_j   = ctx.live_engine.get_market_state(ts)
                        _htf_j  = "bullish" if _ms_j.get("htf_bullish", True) else "bearish"
                        _delay_ms = int((_signal_first_ts and (ts - _signal_first_ts).total_seconds() * 1000) or 0)
                        _jid    = ctx.journal.on_entry(
                            position    = position,
                            market_state= _ms_j,
                            ts          = ts,
                            nifty_spot  = ltp_current,
                            ce_prob_raw = _ms_j.get("ce_prob", 0.0),
                            pe_prob_raw = _ms_j.get("pe_prob", 0.0),
                            htf_state   = _htf_j,
                            entry_delay_ms = _delay_ms,
                        )
                        _journal_id = _jid
                    except Exception as _je:
                        logger.debug(f"[JOURNAL] on_entry failed: {_je}")

                    entry_msg = format_trade_entry({
                        "symbol":  symbol,
                        "side":    side,
                        "price":   position["entry"],
                        "qty":     order["qty"],
                        "stop":    stop_loss,
                        "target":  target,
                        "ml_prob": position["ml_prob"],
                        "regime":  position["regime"],
                    })
                    send_trade_entry_with_exit_button(entry_msg)

                    logger.info(
                        f"[ENTRY] {side} {symbol} "
                        f"qty={order['qty']} fill={order['price']:.2f} "
                        f"SL={stop_loss:.2f} "
                        f"ml={position['ml_prob']:.3f} "
                        f"reason={position['reason']} "
                        f"signal_delay={_delay_ms}ms"
                    )
                else:
                    logger.warning("[ENTRY] Order returned invalid fill — position not opened")

            # ══════════════════════════════════════════════════════════
            # SCALP ENGINE — momentum layer (only when main is flat)
            # ══════════════════════════════════════════════════════════

            if ctx.scalp_engine and position is None:
                # Append spot LTP to rolling 120-second history
                if ltp_current > 0:
                    _scalp_ltp_history.append((ts, ltp_current))

                # ── Scalp live card update (every 2s while in scalp) ──
                if scalp_position is not None and _tg.can_send("scalp_live", 2.0):
                    try:
                        _sl_live_ltp = ctx.broker.ltp(scalp_position["symbol"]) or scalp_position["entry"]
                        update_scalp_live(format_scalp_live(scalp_position, _sl_live_ltp))
                    except Exception as _sl_live_e:
                        logger.debug(f"[SCALP] live update failed: {_sl_live_e}")

                # ── Scalp exit management ─────────────────────────────
                if scalp_position is not None:
                    _s_ltp = ctx.broker.ltp(scalp_position["symbol"]) or scalp_position["entry"]
                    scalp_position["max_pnl"] = max(
                        scalp_position.get("max_pnl", 0.0),
                        (_s_ltp - scalp_position["entry"]) * scalp_position["qty"],
                    )
                    scalp_position["min_pnl"] = min(
                        scalp_position.get("min_pnl", 0.0),
                        (_s_ltp - scalp_position["entry"]) * scalp_position["qty"],
                    )

                    # Centralized profit-lock ladder also governs scalp trades
                    # (single source of truth — identical to normal trades).
                    _sc_new_stop, _sc_stage, _sc_lock = ladder_stop(
                        scalp_position["entry"],
                        scalp_position["qty"],
                        scalp_position["max_pnl"],
                        scalp_position["stop_loss"],
                    )
                    if _sc_new_stop > scalp_position["stop_loss"] + 1e-6:
                        logger.info(
                            f"[LADDER]\nMFE={scalp_position['max_pnl']:.0f}\n"
                            f"LOCK={_sc_lock:.0f}\nstage={_sc_stage} (scalp)  "
                            f"SL {scalp_position['stop_loss']:.2f}->{_sc_new_stop:.2f}"
                        )
                        scalp_position["stop_loss"] = _sc_new_stop

                    # ── Lock at +2pt then trail 2pt behind peak (all lots held) ──
                    _s_move = _s_ltp - scalp_position["entry"]
                    if _s_move >= ctx.config.SCALP_LOCK_PTS:
                        if not scalp_position.get("lock_triggered"):
                            scalp_position["stop_loss"]      = scalp_position["entry"] + 1.0
                            scalp_position["lock_triggered"] = True
                            logger.info(
                                f"[SCALP LOCK] +{ctx.config.SCALP_LOCK_PTS:.0f}pt → "
                                f"SL locked at entry+1={scalp_position['entry'] + 1.0:.1f} | "
                                f"trailing {ctx.config.SCALP_TRAIL_PTS:.0f}pt below peak"
                            )
                            import telegram.notifier as _tgn
                            _tgn.send_bot(
                                f"🔒 <b>SCALP LOCK</b> +{ctx.config.SCALP_LOCK_PTS:.0f}pt\n"
                                f"SL → entry+1pt  |  trailing {ctx.config.SCALP_TRAIL_PTS:.0f}pt  |  riding all lots"
                            )
                        # Trail SL 2pt below current ltp — ratchet up only, never down
                        _trail_sl = _s_ltp - ctx.config.SCALP_TRAIL_PTS
                        if _trail_sl > scalp_position["stop_loss"]:
                            scalp_position["stop_loss"] = _trail_sl

                    _s_exit, _s_reason = ctx.scalp_engine.check_exit(scalp_position, _s_ltp, ts)
                    # Honor the ladder-raised stop (virtual trigger) in addition
                    # to the scalp engine's fixed initial stop / target / time.
                    if not _s_exit and _s_ltp <= scalp_position["stop_loss"]:
                        _s_exit, _s_reason = True, "STOP"
                    if _s_exit:
                        if _s_reason in ("STOP",):
                            _log_stop_audit("SCALP STOP HIT", _s_ltp, scalp_position)
                        _s_exit_order = ctx.executor.execute_exit(
                            scalp_position["symbol"],
                            scalp_position["qty"],
                            side=scalp_position["side"],
                        )
                        _s_fill = _s_exit_order["price"] if _s_exit_order else _s_ltp
                        _s_pnl  = (_s_fill - scalp_position["entry"]) * scalp_position["qty"]
                        ctx.pnl          += _s_pnl
                        ctx.trades_today += 1
                        try:
                            log_trade(
                                entry_order  = {"symbol": scalp_position["symbol"],
                                                "price":  scalp_position["entry"],
                                                "qty":    scalp_position["qty"]},
                                exit_price   = _s_fill,
                                exit_reason  = f"SCALP_{_s_reason}",
                                position     = scalp_position,
                                entry_time   = scalp_position["entry_ts"],
                                exit_time    = ts,
                                ce_threshold = 0.0,
                                pe_threshold = 0.0,
                            )
                        except Exception as _sl_e:
                            logger.warning(f"[SCALP] log_trade failed: {_sl_e}")
                        logger.info(
                            f"[SCALP EXIT] {_s_reason} | {scalp_position['symbol']} "
                            f"| pnl={_s_pnl:+.0f} | day={ctx.pnl:+.0f}"
                        )
                        _scalp_exit_msg = format_scalp_exit(scalp_position, _s_fill,
                                                           f"SCALP_{_s_reason}", _s_pnl)
                        # Freeze scalp card to final exit summary (stays in chat),
                        # then post exit to channel as separate message.
                        freeze_scalp_message(_scalp_exit_msg)
                        send_trade_channel(_scalp_exit_msg)
                        scalp_position = None
                        _scalp_trades_today += 1
                        ctx.scalp_engine.on_exit()

                # ── Scalp entry ───────────────────────────────────────
                _scalp_max = getattr(ctx.config, "SCALP_MAX_TRADES", 10)
                if (scalp_position is None
                        and ctx.trades_today < max_trades
                        and _scalp_trades_today < _scalp_max
                        and ctx.pnl > ctx.config.DAILY_LOSS_LIMIT):
                    _s_sig = ctx.scalp_engine.check_entry(
                        ltp_current, _scalp_ltp_history, ts
                    )
                    if _s_sig:
                        _s_side   = _s_sig["side"]
                        _s_symbol, _s_opt_ltp = ctx.broker.get_atm_option(_s_side)
                        if _s_symbol and _s_opt_ltp:
                            if _s_opt_ltp < ctx.config.SCALP_MIN_OPT_PTS:
                                logger.info(
                                    f"[SCALP SKIP] {_s_side} {_s_symbol} @ {_s_opt_ltp:.1f} "
                                    f"< min {ctx.config.SCALP_MIN_OPT_PTS:.0f}pt — too cheap, skipped"
                                )
                            else:
                                # ── Fetch ML state BEFORE order (filter + logging) ──
                                _ms_s = ctx.live_engine.get_market_state(ts)
                                _s_ml_prob = (
                                    _ms_s.get("ce_adj", 0.0)
                                    if _s_side == "CE"
                                    else _ms_s.get("pe_adj", 0.0)
                                )
                                _s_opp_prob = (
                                    _ms_s.get("pe_adj", 0.0)
                                    if _s_side == "CE"
                                    else _ms_s.get("ce_adj", 0.0)
                                )
                                _s_ml_edge = _s_ml_prob - _s_opp_prob
                                _ml_disagree_margin = getattr(ctx.config, "SCALP_ML_DISAGREE_MARGIN", 0.12)
                                _ml_min_prob        = getattr(ctx.config, "SCALP_ML_MIN_PROB", 0.40)
                                _ml_min_edge        = getattr(ctx.config, "SCALP_ML_MIN_EDGE", 0.08)
                                _range_mom_thresh   = getattr(ctx.config, "SCALP_RANGE_MOM_THRESH", 15.0)
                                _day_type_str       = str(ctx.ml_learner.get_day_type()).upper()
                                _is_range_day       = (ctx.live_engine._day_classified
                                                       and "RANGE" in _day_type_str)

                                if _s_ml_edge < -_ml_disagree_margin:
                                    # ML strongly favours opposite direction — skip
                                    logger.info(
                                        f"[SCALP SKIP] ML disagrees {_s_side}: "
                                        f"ml={_s_ml_prob:.3f} opp={_s_opp_prob:.3f} "
                                        f"edge={_s_ml_edge:.3f} < -{_ml_disagree_margin}"
                                    )
                                elif _s_ml_prob < _ml_min_prob:
                                    # Absolute ML conviction too low
                                    logger.info(
                                        f"[SCALP SKIP] ML weak {_s_side}: "
                                        f"ml={_s_ml_prob:.3f} < {_ml_min_prob}"
                                    )
                                elif _s_ml_edge < _ml_min_edge:
                                    # ML edge too thin — models have no clear view
                                    logger.info(
                                        f"[SCALP SKIP] ML edge thin {_s_side}: "
                                        f"edge={_s_ml_edge:.3f} < {_ml_min_edge}"
                                    )
                                elif _is_range_day and abs(_s_sig["move_pts"]) < _range_mom_thresh:
                                    # RANGE_DAY: require stronger momentum to reduce chop
                                    logger.info(
                                        f"[SCALP SKIP] RANGE_DAY weak move {_s_side}: "
                                        f"{_s_sig['move_pts']:+.1f}pt < {_range_mom_thresh:.0f}pt"
                                    )
                                else:
                                    _scalp_entry_qty = ctx.config.SCALP_LOTS * ctx.config.LOT_SIZE
                                    _s_order = ctx.executor.execute_entry(_s_symbol, _s_side, _scalp_entry_qty)
                                    if _s_order:
                                        scalp_position = {
                                            "symbol":         _s_symbol,
                                            "side":           _s_side,
                                            "qty":            _scalp_entry_qty,
                                            "lot_size":       ctx.config.LOT_SIZE,
                                            "entry":          _s_order["price"],
                                            "stop_loss":      _s_order["price"] - ctx.config.SCALP_SL_PTS,
                                            "target":         _s_order["price"] + ctx.config.SCALP_TARGET_PTS,
                                            "max_pnl":        0.0,
                                            "min_pnl":        0.0,
                                            "ml_prob":        float(_s_ml_prob or 0.0),
                                            "features":       ctx.live_engine._last_features or {},
                                            "regime":         "SCALP",
                                            "reason":         _s_sig["reason"],
                                            "entry_ts":       ts,
                                            "lock_triggered": False,
                                        }
                                        logger.info(
                                            f"[SCALP ENTRY] {_s_side} {_s_symbol} "
                                            f"@ {_s_order['price']:.1f} "
                                            f"| BANKNIFTY move={_s_sig['move_pts']:+.1f}pt "
                                            f"| ml={float(_s_ml_prob or 0.0):.3f} "
                                            f"opp_ml={float(_s_opp_prob or 0.0):.3f} "
                                            f"| scalp={_scalp_trades_today+1}/{_scalp_max}"
                                        )
                                        _scalp_entry_msg = format_scalp_entry(scalp_position, _s_sig["move_pts"])
                                        send_scalp_entry(_scalp_entry_msg)
                                        send_trade_channel(_scalp_entry_msg)

            # ══════════════════════════════════════════════════════════
            # DUAL DASHBOARD (two persistent edit-in-place messages)
            # ══════════════════════════════════════════════════════════

            # Pause both dashboards while any trade card is active.
            # Trade/scalp live cards update every 2s and take over in-trade.
            _in_trade = position is not None or scalp_position is not None
            dash_interval = 60.0

            if not _in_trade and _tg.can_send("dashboard", dash_interval):
                market_state = ctx.live_engine.get_market_state(ts)
                # F8 — augment with live feed health + orb mode
                try:
                    market_state["feed_health"] = _get_feed_health(ctx.broker)
                    _orb_mode = (
                        "RECONSTRUCTED" if (ctx.live_engine.orb_done and
                                            ctx.live_engine.orb_high is not None)
                        else ("BUILDING" if not ctx.live_engine.orb_done else "UNAVAIL")
                    )
                    market_state["orb_mode"] = _orb_mode
                except Exception:
                    pass

                # Dashboard 1: AI Engine — delete old, post fresh at bottom
                try:
                    engine_msg = format_engine_dashboard(ctx, market_state, ltp_current)
                    repost_engine_dashboard(engine_msg)
                except Exception as _ed_e:
                    logger.debug(f"[TG] engine dashboard failed: {_ed_e}")

                # Dashboard 2: Live Status
                market_msg = render_market(ctx, market_state, None, ltp_current)
                send_or_edit_market_dashboard(market_msg)

            # ── EOD summary at 15:30 ─────────────────────────────────
            if not _eod_sent and now >= __import__("datetime").time(15, 30):
                _eod_sent = True
                summary = today_summary()
                send_eod_summary(summary)
                logger.info(
                    f"[EOD] Trades={summary['trades']} PnL=₹{summary['pnl']:.0f} "
                    f"WR={summary.get('win_rate',0):.0f}%"
                )

                # ── F2: comprehensive daily review ─────────────────────
                try:
                    tg_force(eod_review())
                except Exception as _e2:
                    logger.warning(f"[EOD F2] {_e2}")

                # ── F7: profit retention simulation ────────────────────
                try:
                    _ret_report = _profit_retention_sim(ctx)
                    if _ret_report:
                        tg_force(_ret_report)
                except Exception as _rr_e:
                    logger.warning(f"[EOD F7 SIM] {_rr_e}")

                # ── F6: strategy drift check (alert if degrading) ──────
                try:
                    _drift_rpt, _drift_alerts = drift_check()
                    if _drift_alerts:
                        tg_force(_drift_rpt)
                        for _da in _drift_alerts:
                            logger.warning(f"[DRIFT] {_da}")
                except Exception as _e6:
                    logger.warning(f"[EOD F6] {_e6}")

                # ── F8: equity curve drawdown alert ────────────────────
                try:
                    _eq_rpt, _eq_alerts = equity_curve_stats()
                    if _eq_alerts:
                        for _ea in _eq_alerts:
                            tg_force(_ea)
                except Exception as _e8:
                    logger.warning(f"[EOD F8] {_e8}")

                # ── F5: slippage summary ────────────────────────────────
                try:
                    _slip_rpt = slippage_stats(n_trades=50)
                    if _slip_rpt and "No slippage" not in _slip_rpt:
                        tg_force(_slip_rpt)
                except Exception as _e5:
                    logger.warning(f"[EOD F5] {_e5}")

                # ── F3/F4/F7 deep analytics — log only (not Telegram spam)
                try:
                    logger.info("[EOD F3 REGIME]\n" + regime_breakdown())
                    logger.info("[EOD F4 ML_BUCKETS]\n" + ml_bucket_breakdown())
                    logger.info("[EOD F7 SETUP]\n" + setup_breakdown())
                except Exception as _e34:
                    logger.warning(f"[EOD F3/F4/F7] {_e34}")

                # ── Weekly model retrain (Friday after close) ──────────
                try:
                    if ts.weekday() == 4 and not check_retrain_lock():
                        import threading as _thr
                        from ml.feedback_trainer import retrain_with_feedback

                        def _do_retrain():
                            try:
                                tg_force("[RETRAIN] Starting weekly model retrain...")
                                retrain_with_feedback(live_weight=3)
                                write_retrain_lock()
                                tg_force("[RETRAIN] Weekly retrain complete. Models updated.")
                                logger.info("[RETRAIN] Weekly retrain finished successfully")
                            except Exception as _re:
                                logger.warning(f"[RETRAIN] Failed: {_re}")
                                tg_force(f"[RETRAIN] Failed: {_re}")

                        _thr.Thread(target=_do_retrain, daemon=True, name="weekly_retrain").start()
                except Exception as _retrain_e:
                    logger.warning(f"[RETRAIN] Trigger error: {_retrain_e}")

            # ── Health file update ────────────────────────────────────
            update_health(snapshot(ctx))

            # ── Persist runtime state every cycle (survives restart) ──
            # Captures stop_loss / target / max_pnl / pnl / trades_today as they
            # change, so a restart resumes from at most ~1s ago.
            try:
                save_state(ctx, position, scalp_position, _scalp_trades_today)
            except Exception:
                pass

            ctx.cycle_count += 1
            time.sleep(1)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt in engine loop")
            break

        except Exception as e:
            logger.error(f"[ENGINE LOOP ERROR] {e}", exc_info=True)
            time.sleep(1)
            # NOTE: position management is NOT here — it runs in the main try block above
            # This except only handles unexpected crashes and resumes the loop


# ══════════════════════════════════════════════════════════════════════
# THREAD LAUNCHER
# ══════════════════════════════════════════════════════════════════════

def start_engine(ctx: TradingContext, builder: CandleBuilder):
    global _engine_thread
    _engine_thread = threading.Thread(
        target=engine_loop,
        args=(ctx, builder),
        daemon=True,
    )
    _engine_thread.start()


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

_PIDFILE = os.path.join("data", ".master_runner.pid")


def _pid_is_live_master(pid: int) -> bool:
    """True if `pid` is a running master_runner.py process (not a reused pid)."""
    if pid <= 0:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        try:
            cmd = " ".join(psutil.Process(pid).cmdline()).lower()
        except Exception:
            return False  # process vanished / access denied → treat as not ours
        return "master_runner" in cmd
    except ImportError:
        # No psutil: fall back to a liveness probe. Cannot verify identity, so a
        # reused pid could cause a false "already running" — acceptable vs the
        # alternative (multiple engines corrupting shared state). See
        # [[project-multi-instance-footgun]].
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _acquire_singleton_lock():
    """
    Refuse to start if another master_runner is already running.

    Multiple concurrent instances each append to data/historical/banknifty_1m_full.csv
    every minute, producing duplicate candles that corrupt the feature-seed window
    and zero out ML predictions (root-caused 2026-06-18). One engine only.
    """
    try:
        os.makedirs(os.path.dirname(_PIDFILE), exist_ok=True)
        if os.path.exists(_PIDFILE):
            try:
                existing = int(open(_PIDFILE).read().strip() or "0")
            except (ValueError, OSError):
                existing = 0
            if existing and existing != os.getpid() and _pid_is_live_master(existing):
                logger.error(
                    "[SINGLETON] master_runner already running (pid=%d). "
                    "Refusing to start a second instance. Kill the existing one "
                    "first, or delete %s if it is stale.", existing, _PIDFILE
                )
                sys.exit(1)
            # stale or our own → reclaim
        with open(_PIDFILE, "w") as f:
            f.write(str(os.getpid()))

        def _release():
            try:
                if os.path.exists(_PIDFILE) and \
                   open(_PIDFILE).read().strip() == str(os.getpid()):
                    os.remove(_PIDFILE)
            except Exception:
                pass

        atexit.register(_release)
        logger.info("[SINGLETON] Lock acquired (pid=%d).", os.getpid())
    except SystemExit:
        raise
    except Exception as e:
        # Never let lock bookkeeping crash startup — log and proceed.
        logger.warning("[SINGLETON] Lock setup failed (non-fatal): %s", e)


def main():
    _acquire_singleton_lock()
    logger.info("MASTER STARTED")

    # ── Startup Telegram ──────────────────────────────────────────────
    from datetime import time as _dtime
    _now_t       = datetime.now().time()
    mode_str     = "PAPER MODE" if PAPER_MODE else "LIVE MODE (DRY_RUN)"
    _is_early    = _now_t < _dtime(9, 15)
    _mid_orb     = _dtime(9, 15) <= _now_t < _dtime(9, 30)
    _after_orb   = _now_t >= _dtime(9, 30)
    if _is_early:
        _orb_line = (
            "Ready for ORB capture at 9:15.\n"
            "Engine will monitor 9:15–9:30 for ORB range,\n"
            "then trade from 9:30 onward."
        )
    elif _mid_orb:
        _orb_line = (
            f"Started mid-ORB window ({_now_t.strftime('%H:%M')}).\n"
            "Partial ORB seeded from Zerodha — live ticks will complete it."
        )
    else:
        _orb_line = (
            f"Started after ORB window ({_now_t.strftime('%H:%M')}).\n"
            "ORB will be reconstructed from Zerodha historical candles.\n"
            "CE + PE ORB breakout entries remain active."
        )
    _ready_msg = (
        f"<b>AI Trading System Started</b>\n"
        f"Mode: {mode_str}\n"
        f"DRY_RUN: No real orders will be placed\n\n"
        + _orb_line
        + "\n\n/help for commands"
    )
    try:
        tg_force(_ready_msg)
    except Exception as e:
        logger.warning(f"Telegram startup message failed: {e}")

    # ── Broker ────────────────────────────────────────────────────────
    try:
        broker = init_broker()
        logger.info("Broker initialized")
    except Exception as e:
        logger.critical(f"Broker init failed: {e}")
        return

    # ── Historical data update (Zerodha → CSV before seeding) ─────────
    # Must run after broker auth and before CandleBuilder seed so
    # today's intraday candles warm up indicators from the first tick.
    update_historical_data(broker, HIST_CSV)

    # ── Candle builder ────────────────────────────────────────────────
    try:
        builder = init_candle_builder(broker)
        logger.info(
            f"CandleBuilder ready | seeded={builder.candle_count()} candles"
        )
    except Exception as e:
        logger.critical(f"CandleBuilder init failed: {e}")
        return

    # ── Context ───────────────────────────────────────────────────────
    try:
        ctx = build_context(broker)
        logger.info("Context built")
    except Exception as e:
        logger.critical(f"Context build failed: {e}")
        return

    # ── Scalp engine init ─────────────────────────────────────────────
    if ctx.config.SCALP_ENABLED:
        ctx.scalp_engine = ScalpEngine(ctx.config)
    else:
        logger.info("[SCALP] Scalp engine disabled (SCALP_ENABLED=0)")

    # ── ORB reconstruction ────────────────────────────────────────────
    # If startup is after 9:30 (or mid-window), fetch today's 9:15–9:29
    # candles from Zerodha so ORB breakout signals are available
    # immediately rather than staying disabled all day.
    try:
        ctx.live_engine.reconstruct_orb_if_needed(broker)
    except Exception as e:
        logger.warning(f"[ORB] Reconstruction failed (non-fatal): {e}")

    # ── Post-reconstruction Telegram status ──────────────────────────
    # Send a second, accurate ORB status message now that reconstruction
    # has either succeeded or failed, so the operator sees the real state.
    try:
        _le = ctx.live_engine
        if _le.orb_done and _le.orb_high is not None and _le.orb_low is not None:
            _orb_result_msg = (
                f"<b>ORB RECONSTRUCTED</b>\n"
                f"High={_le.orb_high:.2f}  Low={_le.orb_low:.2f}\n"
                f"CE + PE breakout entries active."
            )
        elif datetime.now().time() >= __import__("datetime").time(9, 30):
            _orb_result_msg = (
                "⚠️ <b>ORB UNAVAILABLE</b>\n"
                "Zerodha API returned no candles for 9:15-9:29.\n"
                "ML-only entries remain active."
            )
        else:
            _orb_result_msg = None   # Before 9:30 — no follow-up needed

        if _orb_result_msg:
            tg_force(_orb_result_msg)
    except Exception as _orb_tg_e:
        logger.warning(f"[ORB] Telegram status failed (non-fatal): {_orb_tg_e}")

    # ── Engine thread ─────────────────────────────────────────────────
    try:
        start_engine(ctx, builder)
        logger.info("Engine thread started")
    except Exception as e:
        logger.critical(f"Engine start failed: {e}")
        return

    # ── F1: Engine watchdog (starts after engine thread) ─────────────
    try:
        _watchdog = EngineWatchdog(ctx, builder)
        logger.info(
            f"[ENGINE WATCHDOG] Supervisor started — "
            f"interval={_WATCHDOG_INTERVAL_S}s max_restarts={_WATCHDOG_MAX_RESTARTS}"
        )
    except Exception as _wd_e:
        logger.warning(f"[ENGINE WATCHDOG] Failed to start (non-fatal): {_wd_e}")

    # ── Startup feed health report ────────────────────────────────────
    try:
        _log_feed_health(broker, ctx, builder)
    except Exception as _fh_e:
        logger.warning(f"[FEED HEALTH] Report failed (non-fatal): {_fh_e}")

    # ── Keep-alive ────────────────────────────────────────────────────
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping system...")
        try:
            tg_force("🛑 System stopped manually")
        except Exception:
            pass


if __name__ == "__main__":
    main()
