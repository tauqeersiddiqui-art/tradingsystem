#!/usr/bin/env python3
# RUN_ANALYSIS.py
#
# SINGLE COMMAND to analyze system profitability.
#
# Usage:
#   python RUN_ANALYSIS.py
#
# Extracts complete truth from PostgreSQL and delivers final verdict.

import os
import sys
from pathlib import Path
from datetime import datetime

# Fix Windows console encoding
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Add project root
sys.path.insert(0, str(Path(__file__).parent))

from engine.analytics.performance_analyzer import analyze_performance
from scripts.analysis.generate_performance_report import format_console_report


def main():
    print("")
    print("=" * 80)
    print(" " * 20 + "TRADING SYSTEM PROFITABILITY ANALYSIS")
    print("=" * 80)
    print("")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("")
    print("Extracting truth from PostgreSQL...")
    print("")

    # Check database connection (optional - will fallback to CSV)
    use_postgres = False
    try:
        from engine.storage.postgres_client import get_client

        client = get_client()
        if client.connect():
            health = client.health_check()

            if health['status'] == 'healthy':
                print(f"[OK] PostgreSQL connected")
                print(f"  Total orders: {health['stats']['total_orders']}")
                print(f"  Today's trades: {health['stats']['today_trades']}")
                print("")
                use_postgres = True
                client.close()
            else:
                print(f"[WARNING] PostgreSQL unhealthy, using CSV fallback")
                print("")
        else:
            print("[WARNING] PostgreSQL not available, using CSV fallback")
            print("")

    except Exception as e:
        print(f"[WARNING] PostgreSQL not available ({type(e).__name__}), using CSV fallback")
        print("")

    # Run analysis (PostgreSQL or CSV fallback)
    print("Running complete performance analysis...")
    print("")

    report = None

    if use_postgres:
        try:
            print("Using PostgreSQL database...")
            report = analyze_performance(
                start_date=None,
                end_date=None,
                include_reality_check=True
            )
        except Exception as e:
            print(f"[WARNING] PostgreSQL analysis failed: {e}")
            print("Falling back to CSV analysis...")
            print("")
            use_postgres = False

    if not use_postgres:
        try:
            print("Using CSV trade logs...")
            from engine.analytics.csv_analyzer import analyze_from_csv
            report = analyze_from_csv()

            if report is None:
                print("")
                print("=" * 80)
                print(" " * 32 + "NO TRADES FOUND")
                print("=" * 80)
                print("")
                print("No trade data found in:")
                print("  - PostgreSQL database")
                print("  - backtest/results/trade_log.csv")
                print("  - data/trades/trade_log_*.csv")
                print("")
                print("Next steps:")
                print("  1. Run the trading system to generate trades")
                print("  2. Or import historical trades into PostgreSQL")
                print("")
                return 0

            print("[OK] Loaded trades from CSV files")
            print("")

        except Exception as csv_err:
            print(f"[FAIL] CSV analysis failed: {csv_err}")
            import traceback
            traceback.print_exc()
            return 1

    if report is None:
        print("[FAIL] Analysis produced no results")
        return 1

    # Check if we have data
    if report.core.total_trades == 0:
        print("")
        print("=" * 80)
        print(" " * 32 + "NO TRADES FOUND")
        print("=" * 80)
        print("")
        print("The system has not executed any trades yet.")
        print("")
        print("Next steps:")
        print("  1. Run the trading system in paper mode")
        print("  2. Let it execute trades")
        print("  3. Run this analysis again")
        print("")
        return 0

    # Display full report
    console_report = format_console_report(report)
    print(console_report)

    # Save report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("data/reports", exist_ok=True)

    report_file = f"data/reports/analysis_{timestamp}.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(console_report)

    print(f"Report saved: {report_file}")
    print("")

    # Summary verdict
    print("")
    print("=" * 80)
    print(" " * 32 + "FINAL VERDICT")
    print("=" * 80)
    print("")

    if report.verdict == "PROFITABLE & STABLE":
        print("[OK] STATUS: PROFITABLE & STABLE")
        print("")
        print(f"  Net PnL: Rs.{report.core.net_pnl:,.2f}")
        print(f"  Win Rate: {report.core.win_rate}%")
        print(f"  Profit Factor: {report.core.profit_factor}")
        print(f"  Expectancy: Rs.{report.core.expectancy:.2f}")
        print("")
        print("  -> System makes money consistently.")
        print("  -> Continue trading with current parameters.")
        print("")
        exit_code = 0

    elif report.verdict == "PROFITABLE BUT UNSTABLE":
        print("[WARNING] STATUS: PROFITABLE BUT UNSTABLE")
        print("")
        print(f"  Net PnL: Rs.{report.core.net_pnl:,.2f}")
        print(f"  Win Rate: {report.core.win_rate}%")
        print(f"  Max Drawdown: {report.core.max_drawdown_pct}%")
        print("")
        print("  -> System is profitable but volatile.")
        print("  -> Reduce position size or tighten risk controls.")
        print("")

        if report.warnings:
            print("  Warnings:")
            for warning in report.warnings[:3]:
                print(f"    - {warning}")
            print("")

        exit_code = 2

    elif report.verdict == "BREAK-EVEN":
        print("[WARNING] STATUS: BREAK-EVEN")
        print("")
        print(f"  Net PnL: Rs.{report.core.net_pnl:,.2f}")
        print(f"  Profit Factor: {report.core.profit_factor}")
        print("")
        print("  -> System neither makes nor loses significantly.")
        print("  -> Re-evaluate strategy parameters.")
        print("")
        exit_code = 2

    elif report.verdict == "LOSING SYSTEM":
        print("[FAIL] STATUS: LOSING SYSTEM")
        print("")
        print(f"  Net PnL: Rs.{report.core.net_pnl:,.2f}")
        print(f"  Profit Factor: {report.core.profit_factor}")
        print(f"  Expectancy: Rs.{report.core.expectancy:.2f}")
        print("")
        print("  -> System loses money. DO NOT TRADE.")
        print("  -> Fundamental strategy flaw detected.")
        print("")

        if report.warnings:
            print("  Critical issues:")
            for warning in report.warnings:
                print(f"    - {warning}")
            print("")

        exit_code = 1

    else:
        print(f"[?] STATUS: {report.verdict}")
        exit_code = 2

    # Recommendations
    if report.recommendations:
        print("-" * 80)
        print("RECOMMENDATIONS:")
        print("")
        for rec in report.recommendations:
            print(f"  -> {rec}")
        print("")

    print("=" * 80)
    print("")

    return exit_code


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nAnalysis interrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n[FATAL ERROR]: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
