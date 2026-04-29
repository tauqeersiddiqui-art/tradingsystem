# engine/analytics/trade_logger.py
# Trade logging + analytics

import os
import csv
from datetime import datetime


LOG_PATH = "data/trades_log.csv"


class TradeLogger:

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self._ensure_file()

        # rolling stats (in-memory)
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.pnl = 0.0
        self.equity_peak = 0.0
        self.max_drawdown = 0.0

    def _ensure_file(self):
        if not os.path.exists(LOG_PATH):
            with open(LOG_PATH, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "symbol",
                    "side",
                    "qty",
                    "entry",
                    "exit",
                    "pnl",
                    "latency_entry_ms",
                    "latency_exit_ms",
                    "reason"
                ])

    # ---------------- RECORD TRADE ---------------- #

    def log_trade(self, trade):

        ts = datetime.now().isoformat()

        row = [
            ts,
            trade.get("symbol"),
            trade.get("side"),
            trade.get("qty"),
            round(trade.get("entry", 0), 2),
            round(trade.get("exit", 0), 2),
            round(trade.get("pnl", 0), 2),
            trade.get("latency_entry", 0),
            trade.get("latency_exit", 0),
            trade.get("reason")
        ]

        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(row)

        # ---- update stats ---- #
        pnl = trade.get("pnl", 0)
        self.pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        # drawdown tracking
        self.equity_peak = max(self.equity_peak, self.pnl)
        dd = self.pnl - self.equity_peak
        self.max_drawdown = min(self.max_drawdown, dd)

    # ---------------- SNAPSHOT ---------------- #

    def get_stats(self):

        win_rate = (self.wins / self.total_trades) if self.total_trades else 0

        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(win_rate, 3),
            "pnl": round(self.pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2)
        }