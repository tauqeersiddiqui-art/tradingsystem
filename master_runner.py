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
from datetime import datetime

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

from ml.ml_intraday_learner import IntradayMLLearner

from engine.services.dashboard import render as render_dashboard

from telegram.messages import format_trade_entry, format_trade_exit
from telegram.notifier import (
    send_trade_entry_with_exit_button,
    send_bot,
    remove_exit_button,
    poll_manual_exit,
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
# BROKER INIT
# ══════════════════════════════════════════════════════════════════════

def init_broker():
    if PAPER_MODE:
        logger.info("Paper mode — MockBroker")
        from engine.execution.mock_broker import MockBroker
        broker = MockBroker()
        broker.start_feed(["NIFTY"])
        return broker

    from engine.execution.broker import ZerodhaBroker

    token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("KITE_ACCESS_TOKEN missing in .env")

    broker = ZerodhaBroker()
    # Pass NIFTY 50 index + NFO segment for WebSocket
    broker.start_feed(["NIFTY 50"])
    time.sleep(3)   # allow WebSocket handshake
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
    tg_force("⚡ Engine Loop Started — Monitoring Market...")

    position   = None      # current open position dict
    entry_time = None      # FIX-2: defined at entry, used for held_seconds
    max_trades = ctx.config.MAX_TRADES_PER_DAY

    while True:
        try:
            ts  = datetime.now()
            now = ts.time()

            # ── Poll manual exit button (every cycle) ─────────────────
            # FIX-6: was never called
            poll_manual_exit()

            # ── Process latest WebSocket tick into candle buffer ──────
            # FIX-3: replaces CSV read every loop
            builder.process_tick(ts)

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

                    position   = None
                    entry_time = None

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

                order = ctx.executor.execute_entry(symbol, side, qty * lot_size)

                if order and order.get("price", 0) > 0:

                    stop_loss = decision.get("stop_loss", order["price"] * 0.90)

                    position = {
                        "symbol":   symbol,
                        "side":     side,
                        "qty":      order["qty"],
                        "lot_size": lot_size,
                        "entry":    order["price"],
                        "stop_loss": stop_loss,
                        "target":   decision.get("target", order["price"] * 1.05),
                        "max_pnl":  0.0,
                        "ml_prob":  decision.get("ml_prob", 0.0),
                        "features": decision.get("features", {}),
                        "regime":   decision.get("regime", "UNKNOWN"),
                        "reason":   decision.get("reason", ""),
                    }
                    entry_time = ts    # FIX-2
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
            # DASHBOARD (throttled — every 30 sec)
            # FIX-7: rate limited
            # ══════════════════════════════════════════════════════════

            dash_msg = render_dashboard(ctx, market_data, decision)
            tg_bot(dash_msg, key="dashboard", interval=30.0)

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
    mode_str = "PAPER MODE" if PAPER_MODE else "LIVE MODE"
    try:
        tg_force(f"🚀 AI Trading System Started ({mode_str})")
    except Exception as e:
        logger.warning(f"Telegram startup message failed: {e}")

    # ── Broker ────────────────────────────────────────────────────────
    try:
        broker = init_broker()
        logger.info("Broker initialized")
    except Exception as e:
        logger.critical(f"Broker init failed: {e}")
        return

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
