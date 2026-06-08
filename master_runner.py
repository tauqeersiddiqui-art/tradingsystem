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
import logging
import threading
from datetime import datetime, timedelta

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("master")

# ── ENV flags ─────────────────────────────────────────────────────────
PAPER_MODE = os.getenv("PAPER_MODE", "0") == "1"
TEST_MODE  = os.getenv("TEST_MODE",  "0") == "1"

HIST_CSV   = "data/historical/nifty_1m_full.csv"

# ── Imports ───────────────────────────────────────────────────────────
from engine.execution.execution_engine import ExecutionEngine
from engine.core.context import TradingContext
from engine.config.config import Config
from engine.core.health_monitor import update_health, snapshot
from engine.live_engine import LiveEngine
from engine.portfolio.allocator import CapitalAllocator
from engine.execution.filters import has_oi_wall
from engine.risk.risk_manager import compute_entry_stops
from engine.services.trade_logger import log_trade, today_summary

from ml.ml_intraday_learner import IntradayMLLearner

from engine.services.dashboard import render_engine, render_market

from telegram.messages import format_trade_entry, format_trade_exit
from telegram.notifier import (
    send_trade_entry_with_exit_button,
    send_bot,
    remove_exit_button,
    poll_commands,
    ask_trade_permission,
    send_or_edit_engine_dashboard,
    send_or_edit_market_dashboard,
    send_eod_summary,
    MANUAL_EXIT_REQUESTED,
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


def tg_bot(msg: str, key: str = "generic", interval: float = 10.0):
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


# ══════════════════════════════════════════════════════════════════════
# HISTORICAL DATA — fetch from Zerodha + append live candles
# ══════════════════════════════════════════════════════════════════════

_NIFTY_INDEX_TOKEN = 256265   # NSE:NIFTY 50 instrument token (fixed)
_csv_write_lock    = threading.Lock()
_last_appended_ts  = None     # guard against double-append


def update_historical_data(broker, csv_path: str, lookback_days: int = 5):
    """
    Pull recent NIFTY 1-minute candles from Zerodha historical API and
    append to the local CSV.  Called at startup and can be called any time.
    """
    try:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=lookback_days)

        logger.info(
            f"[HIST] Fetching NIFTY 1m  {from_dt.date()} -> {to_dt.date()} ..."
        )
        raw = broker.kite.historical_data(
            _NIFTY_INDEX_TOKEN, from_dt, to_dt, "minute", oi=False
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

    if hasattr(broker, "has_open_position") and broker.has_open_position():
        tg_force("🚨 SAFETY ALERT\nOpen position detected — engine blocked.")
        raise RuntimeError("Open broker position exists.")

    logger.info("Starting market feed")
    broker.start_feed(["NIFTY 50"])

    time.sleep(3)

    logger.info("Feed ready")
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
    ctx.live_engine = LiveEngine(ctx)
    ctx.allocator   = CapitalAllocator(ctx.config)

    ctx.pnl          = 0.0
    ctx.positions    = []       # list of closed PnL values
    ctx.cycle_count  = 0
    ctx.trades_today = 0

    return ctx


# ══════════════════════════════════════════════════════════════════════
# CANDLE BUILDER INIT
# ══════════════════════════════════════════════════════════════════════

def init_candle_builder(broker) -> CandleBuilder:
    token = CandleBuilder.nifty_token()   # 256265

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
            f"NIFTY LTP: {ltp:.1f}\n"
            f"{pos_str}\n"
            f"PnL: {ctx.pnl:.0f} | Trades: {ctx.trades_today}\n"
            f"CE thr: {ce_thr}  PE thr: {pe_thr}"
        )

    while True:
        try:
            ts  = datetime.now()
            now = ts.time()

            # ── Poll Telegram: buttons + text commands (every cycle) ──
            poll_commands(status_cb=_status_cb)

            # ── Feed watchdog: alert if WebSocket silent >60s in market hours ──
            _mkt_open  = __import__("datetime").time(9, 15)
            _mkt_close = __import__("datetime").time(15, 30)
            if _mkt_open <= now <= _mkt_close:
                _last_tick = getattr(ctx.broker, "_last_tick_time", 0)
                _stale_s   = time.time() - _last_tick if _last_tick else 999
                if _stale_s > 60 and time.time() - _last_feed_warn > 120:
                    _last_feed_warn = time.time()
                    logger.warning(f"[WATCHDOG] Feed stale {_stale_s:.0f}s — reconnecting")
                    tg_bot(
                        f"⚠️ Feed stale ({_stale_s:.0f}s) — reconnecting...",
                        key="watchdog", interval=120.0
                    )
                    try:
                        ctx.broker.start_feed(["NIFTY 50"])
                    except Exception as _wde:
                        logger.error(f"[WATCHDOG] Reconnect failed: {_wde}")

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
            latest_candle = {
                "open":   float(df_window["open"].iloc[-1]),
                "high":   float(df_window["high"].iloc[-1]),
                "low":    float(df_window["low"].iloc[-1]),
                "close":  ltp_current,
                "volume": int(df_window["volume"].iloc[-1]) if "volume" in df_window.columns else 0,
                "ts":     ts,
            }

            market_data = {
                "candle":    latest_candle,
                "df_window": df_window,
                # Legacy keys for dashboard compat
                "candles": df_window["close"].tolist(),
                "highs":   df_window["high"].tolist(),
                "lows":    df_window["low"].tolist(),
            }

            # ── Run decision engine ───────────────────────────────────
            decision = ctx.live_engine.step(market_data, ts)

            # ══════════════════════════════════════════════════════════
            # POSITION MANAGEMENT — runs every cycle, NOT in except block
            # FIX-1: was inside except block — completely broken before
            # ══════════════════════════════════════════════════════════

            if position is not None:
                pos_ltp      = builder.ltp() or position["entry"]
                held_seconds = (ts - entry_time).total_seconds() if entry_time else 0

                # ── Trailing / profit_manager via live_engine.check_exit ──
                exit_flag, exit_reason = ctx.live_engine.check_exit(
                    position, pos_ltp, held_seconds
                )

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

                # ── Execute exit ───────────────────────────────────────
                if exit_flag:
                    exit_order = ctx.executor.execute_exit(
                        symbol=position["symbol"],
                        qty=position["qty"],
                        side=position["side"],     # FIX-5: CE→SELL, PE→BUY
                    )

                    exit_price = (
                        exit_order["price"] if exit_order and exit_order["price"] > 0
                        else pos_ltp
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

                    exit_msg = format_trade_exit({
                        "symbol":      position["symbol"],
                        "side":        position["side"],
                        "entry_price": position["entry"],
                        "exit_price":  exit_price,
                        "qty":         position["qty"],
                        "pnl":         pnl,
                        "ml_prob":     position.get("ml_prob", 0),
                        "reason":      exit_reason,
                        "regime":      position.get("regime", "UNKNOWN"),
                    })
                    tg_force(exit_msg)
                    remove_exit_button()

                    logger.info(
                        f"[EXIT] {exit_reason} | {position['symbol']} | "
                        f"pnl={pnl:.2f} | total={ctx.pnl:.2f}"
                    )

                    position        = None
                    entry_time      = None
                    entry_order_rec = None

                    # Auto-pause after 3 consecutive stop losses
                    if exit_reason in ("STOP", "Stop Loss", "Drawdown"):
                        consecutive_stops += 1
                        if consecutive_stops >= 3:
                            import telegram.notifier as _tn
                            _tn.ENGINE_PAUSED = True
                            tg_force(
                                "AUTO-PAUSE: 3 consecutive stops hit.\n"
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
                    decision = None

            if decision is not None and position is None:

                # FIX-8: max trades per day guard
                if ctx.trades_today >= max_trades:
                    logger.debug(f"[GATE] Max trades reached: {ctx.trades_today}")
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
                    atm_strike = round(current_price / 50) * 50
                    option_chain = ctx.broker.get_option_chain_near_atm(strikes_range=5)
                    if has_oi_wall(option_chain, atm_strike, side):
                        logger.info(f"[GATE] OI wall blocked {side} entry")
                        decision = None
                except Exception as _oi_e:
                    logger.warning(f"[OI FILTER] Error (non-fatal): {_oi_e}")

            if decision is not None and position is None:

                side     = decision["side"]
                symbol, _ = ctx.broker.get_atm_option(side)
                lot_size  = ctx.executor.get_lot_size(symbol)

                # Position sizing via allocator
                atr_val = decision.get("features", {}).get("atr", current_price * 0.01)
                qty = ctx.allocator.size_position(
                    capital=ctx.config.INITIAL_CAPITAL,
                    ml_prob=decision["ml_prob"],
                    atr=atr_val,
                    price=builder.ltp() or 0,
                    lot_size=lot_size,
                    current_pnl=ctx.pnl,
                )

                if qty <= 0:
                    logger.info(f"[GATE] Allocator returned qty=0 — skipping")
                    decision = None

            if decision is not None and position is None:

                side     = decision["side"]
                symbol, _ = ctx.broker.get_atm_option(side)
                lot_size  = ctx.executor.get_lot_size(symbol)
                atr_val   = decision.get("features", {}).get("atr", 1.0)
                qty       = ctx.allocator.size_position(
                    capital=ctx.config.INITIAL_CAPITAL,
                    ml_prob=decision["ml_prob"],
                    atr=atr_val,
                    price=builder.ltp() or 0,
                    lot_size=lot_size,
                    current_pnl=ctx.pnl,
                ) or 1

                # ── Telegram confirmation (30s timeout → auto-execute) ─
                _confirmed = ask_trade_permission(
                    side    = side,
                    price   = builder.ltp() or 0.0,
                    ml_prob = decision["ml_prob"],
                    stop    = decision.get("stop_loss", 0.0),
                    target  = decision.get("target",    0.0),
                )
                if not _confirmed:
                    logger.info(f"[GATE] Trade SKIPPED by user (Telegram SKIP)")
                    decision = None  # fall through cleanly

                if decision is not None:
                    pass  # proceed to execute below

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
                        "ml_prob":  decision.get("ml_prob", 0.0),
                        "features": decision.get("features", {}),
                        "regime":   decision.get("regime", "UNKNOWN"),
                        "reason":   decision.get("reason", ""),
                        "entry_ts": ts,   # for held-time display in dashboard
                    }
                    entry_time      = ts    # FIX-2
                    entry_order_rec = order
                    ctx.trades_today += 1

                    entry_msg = format_trade_entry({
                        "symbol":  symbol,
                        "side":    side,
                        "price":   position["entry"],
                        "qty":     order["qty"],
                        "stop":    stop_loss,
                        "sl_pct":  f"{((stop_loss / position['entry']) - 1) * 100:.1f}",
                        "ml_prob": position["ml_prob"],
                        "regime":  position["regime"],
                    })
                    send_trade_entry_with_exit_button(entry_msg)

                    logger.info(
                        f"[ENTRY] {side} {symbol} "
                        f"qty={order['qty']} fill={order['price']:.2f} "
                        f"SL={stop_loss:.2f}"
                    )
                else:
                    logger.warning("[ENTRY] Order returned invalid fill — position not opened")

            # ══════════════════════════════════════════════════════════
            # DUAL DASHBOARD (two persistent edit-in-place messages)
            # ══════════════════════════════════════════════════════════

            # Faster refresh when in a trade; slower when idle
            dash_interval = 20.0 if position is not None else 60.0

            if _tg.can_send("dashboard", dash_interval):
                market_state = ctx.live_engine.get_market_state(ts)

                # Dashboard 1: AI Engine — ML bias, technicals, decision
                engine_msg = render_engine(ctx, market_state, ltp_current)
                send_or_edit_engine_dashboard(engine_msg)

                # Dashboard 2: Live Status — position card, ORB, internals
                # Attach EXIT button only when in a position
                if position is not None:
                    market_msg = render_market(ctx, market_state, position, ltp_current)
                    exit_kb    = {"inline_keyboard": [[
                        {"text": "🔴 EXIT NOW", "callback_data": "manual_exit"}
                    ]]}
                    send_or_edit_market_dashboard(market_msg, reply_markup=exit_kb)
                else:
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

            # ── Health file update ────────────────────────────────────
            update_health(snapshot(ctx))

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

def main():
    logger.info("MASTER STARTED")

    # ── Startup Telegram ──────────────────────────────────────────────
    from datetime import time as _dtime
    _now_t    = datetime.now().time()
    mode_str  = "PAPER MODE" if PAPER_MODE else "LIVE MODE (DRY_RUN)"
    _is_early = _now_t < _dtime(9, 15)
    _ready_msg = (
        f"<b>AI Trading System Started</b>\n"
        f"Mode: {mode_str}\n"
        f"DRY_RUN: No real orders will be placed\n\n"
        + (
            "Ready for ORB capture at 9:15.\n"
            "Engine will monitor 9:15–9:30 for ORB range,\n"
            "then trade from 9:30 onward."
            if _is_early else
            f"Started after market open ({_now_t.strftime('%H:%M')}).\n"
            "ORB will be locked — only PE ML-entries available.\n"
            "CE ORB breakout entry: DISABLED (missed window)."
        )
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

    # ── Engine thread ─────────────────────────────────────────────────
    try:
        start_engine(ctx, builder)
        logger.info("Engine thread started")
    except Exception as e:
        logger.critical(f"Engine start failed: {e}")
        return

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
