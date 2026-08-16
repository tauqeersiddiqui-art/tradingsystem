#!/usr/bin/env python3
# scripts/test_obsidian_logger.py
# Simple smoke test for utils.obsidian_logger — safe, deterministic, prints any exceptions.

import traceback
import sys
sys.path.insert(0, "D:/All Bots/trading_system")

try:
    from utils.obsidian_logger import initialize_vault, log_trade, log_daily_summary, check_and_log_patterns
    from datetime import datetime, date
except Exception:
    traceback.print_exc()
    raise SystemExit("Could not import obsidian_logger. Ensure utils/obsidian_logger.py exists and you're running from repo root.")

def main():
    try:
        # initialize (creates folders)
        initialize_vault()

        # sample trade payload (structure must match your log_trade expected keys)
        log_trade(
            entry_price=107.50,
            exit_price=110.00,
            pnl=1434.0,
            mfe=200.0,
            ml_score=0.85,
            strategy="PE_ORB_ML",
            side="PE",
            symbol="BANKNIFTY",
            entry_ts=datetime(2026, 7, 1, 9, 20, 0),
            exit_ts=datetime(2026, 7, 1, 9, 40, 0),
            exit_reason="TARGET",
            held_seconds=1200,
        )

        log_daily_summary(
            total_trades=1,
            net_pnl=1434.0,
            win_rate=100.0,
            avg_mfe=200.0,
            ce_trades=0,
            ce_wr=0.0,
            pe_trades=1,
            pe_wr=100.0,
            trade_date=date(2026, 7, 1),
        )

        print("OK: obsidian logger ran — check trading_brain/ for files (trades + daily).")
    except Exception:
        traceback.print_exc()
        print("ERROR: obsidian logger test failed.")

if __name__ == '__main__':
    main()