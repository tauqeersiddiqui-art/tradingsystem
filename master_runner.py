from dotenv import load_dotenv
load_dotenv()

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import time
import threading
from datetime import datetime

PAPER_MODE = os.getenv("PAPER_MODE", "0") == "1"
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

from engine.execution.execution_engine import ExecutionEngine
from engine.data.data_manager import DataManager
from engine.core.context import TradingContext
from engine.config.config import Config
from engine.core.health_monitor import update_health, snapshot

from engine.live_engine import LiveEngine
from engine.portfolio.allocator import CapitalAllocator
from ml.ml_intraday_learner import IntradayMLLearner

from engine.services.dashboard import render as render_dashboard

from telegram.messages import format_trade_entry, format_trade_exit
from telegram.notifier import (
    send_trade_entry_with_exit_button,
    send_bot,
    remove_exit_button,
    poll_manual_exit,
    MANUAL_EXIT_REQUESTED
)

_engine_thread = None
_stop_event = threading.Event()


def log(msg):
    print(f"[MASTER] {datetime.now().strftime('%H:%M:%S')} | {msg}")


def init_broker():
    if PAPER_MODE:
        log("Initializing CSV Mock Broker...")
        from engine.execution.mock_broker import MockBroker
        broker = MockBroker()
        broker.start_feed(["NIFTY"])
        return broker

    from engine.execution.broker import ZerodhaBroker

    if not os.getenv("KITE_ACCESS_TOKEN"):
        raise RuntimeError("Missing access token")

    broker = ZerodhaBroker()
    broker.start_feed(["NIFTY 50"])
    time.sleep(3)
    return broker


def build_context(broker):
    ctx = TradingContext()

    ctx.broker = broker
    ctx.config = Config()

    ctx.executor = ExecutionEngine(broker, ctx.config)
    ctx.ml_learner = IntradayMLLearner()
    ctx.live_engine = LiveEngine(ctx)
    ctx.allocator = CapitalAllocator(ctx.config)

    ctx.pnl = 0
    ctx.positions = []
    ctx.cycle_count = 0

    return ctx


def engine_loop(ctx):

    log("Engine loop started")
    send_bot("⚡ Engine Loop Started — Monitoring Market...")

    data_manager = DataManager(ctx.broker)

    position = None
    last_dashboard_time = 0

    while True:

        try:
            df = data_manager.get_latest_window(120)

            if df is None or len(df) < 50:
                time.sleep(0.5)
                continue

            candles = df["close"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()

            market_data = {
                "candles": candles,
                "highs": highs,
                "lows": lows
            }

            decision = ctx.live_engine.step(market_data)

            # ================= DASHBOARD (THROTTLED) ================= #
            now = time.time()
            if now - last_dashboard_time > 30:   # ⬅️ every 30 sec
                msg = render_dashboard(ctx, market_data, decision)
                send_bot(msg)
                last_dashboard_time = now

            # ================= ENTRY ================= #
            if decision and position is None:

                symbol, lot_size = ctx.broker.get_atm_option(decision["side"])
                price = ctx.broker.ltp(symbol)

                qty = lot_size

                order = ctx.executor.execute_entry(symbol, decision["side"], qty)

                print(f"[ORDER DEBUG] {order}")

                if order:

                    position = {
                        "symbol": symbol,
                        "side": decision["side"],
                        "qty": qty,
                        "entry": order["price"],
                        "ml_prob": decision.get("ml_prob")
                    }

                    send_trade_entry_with_exit_button(
                        format_trade_entry({
                            "symbol": symbol,
                            "side": decision["side"],
                            "price": position["entry"],
                            "qty": qty,
                            "stop": position["entry"] * 0.9,
                            "ml_prob": position["ml_prob"],
                            "regime": "TREND"
                        })
                    )

            # ================= EXIT ================= #
            if position:

                ltp = ctx.broker.ltp(position["symbol"])

                if ltp <= position["entry"] * 0.9:

                    exit_order = ctx.executor.execute_exit(
                        position["symbol"],
                        position["qty"]
                    )

                    if exit_order:

                        pnl = (exit_order["price"] - position["entry"]) * position["qty"]

                        ctx.pnl += pnl
                        ctx.positions.append(pnl)

                        send_bot(format_trade_exit({
                            "symbol": position["symbol"],
                            "side": position["side"],
                            "entry_price": position["entry"],
                            "exit_price": exit_order["price"],
                            "qty": position["qty"],
                            "pnl": pnl,
                            "ml_prob": position["ml_prob"],
                            "reason": "STOP",
                            "regime": "TREND"
                        }))

                        position = None

            time.sleep(1)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(1)
            # ================= POSITION MANAGEMENT ================= #
            if position:

                ltp = ctx.broker.ltp(position["symbol"])

                # 🔥 TRAILING (FIXED)
                new_sl = ctx.executor.update_trailing(position["symbol"], ltp)
                if new_sl:
                    position["stop"] = new_sl

                # 🔴 STOP LOSS HIT
                if ltp <= position["stop"]:
                    exit_flag, reason = True, "STOP"

                else:
                    held_time = time.time() - entry_time
                    exit_flag, reason = ctx.live_engine.check_exit(position, ltp, held_time)

                if MANUAL_EXIT_REQUESTED:
                    exit_flag, reason = True, "MANUAL"

                if exit_flag:

                    exit_order = ctx.executor.execute_exit(position["symbol"], position["qty"])

                    exit_price = ltp  # fallback

                    pnl = (exit_price - position["entry"]) * position["qty"]

                    ctx.pnl += pnl
                    ctx.positions.append(pnl)

                    send_bot(format_trade_exit({
                        "symbol": position["symbol"],
                        "side": position["side"],
                        "entry_price": position["entry"],
                        "exit_price": exit_price,
                        "qty": position["qty"],
                        "pnl": pnl,
                        "ml_prob": position["ml_prob"],
                        "reason": reason,
                        "regime": "TREND"
                    }))

                    remove_exit_button()
                    position = None

            # ================= GLOBAL RISK ================= #
            if ctx.pnl < -2000:
                send_bot("🛑 DAILY LOSS LIMIT HIT — SYSTEM STOPPED")
                break

            update_health(snapshot(ctx))

            ctx.cycle_count += 1
            time.sleep(0.5)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(1)


def start_engine(ctx):
    global _engine_thread
    _engine_thread = threading.Thread(target=engine_loop, args=(ctx,), daemon=True)
    _engine_thread.start()


def main():

    log("MASTER STARTED")

    # ================= TELEGRAM START TEST ================= #
    try:
        send_bot(
            "🚀 AI Trading System Started (PAPER MODE)"
            if PAPER_MODE else
            "🚀 AI Trading System Started (LIVE MODE)"
        )
        log("Telegram start message sent")
    except Exception as e:
        log(f"Telegram failed at startup: {e}")

    # ================= INIT ================= #
    try:
        broker = init_broker()
        log("Broker initialized")
    except Exception as e:
        log(f"Broker init failed: {e}")
        return

    try:
        ctx = build_context(broker)
        log("Context built")
    except Exception as e:
        log(f"Context build failed: {e}")
        return

    # ================= ENGINE START ================= #
    try:
        start_engine(ctx)
        log("Engine thread started")

        # 🔥 Force Telegram test from engine layer
        send_bot("⚡ Engine Loop Started — Monitoring Market...")

    except Exception as e:
        log(f"Engine start failed: {e}")
        return

    # ================= MAIN LOOP ================= #
    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        log("Stopping system...")

        try:
            send_bot("🛑 System stopped manually")
        except:
            pass

if __name__ == "__main__":
    main()