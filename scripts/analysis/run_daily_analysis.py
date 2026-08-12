# scripts/analysis/run_daily_analysis.py
#
# AUTOMATED DAILY ANALYSIS — Runs at EOD to generate performance report
# and send Telegram summary.
#
# NO MANUAL INTERVENTION. Fully automated via cron/scheduler.
#
# Usage:
#   python scripts/analysis/run_daily_analysis.py
#
# Cron example (run at 4:30 PM daily):
#   30 16 * * 1-5 cd /path/to/trading_system && python scripts/analysis/run_daily_analysis.py

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.analytics.performance_analyzer import analyze_performance


def send_telegram_summary(report):
    """Send concise performance summary to Telegram."""
    try:
        from telegram.notifier import send_alert

        # Build message
        emoji = "✓" if report.verdict == "PROFITABLE & STABLE" else "⚠" if "UNSTABLE" in report.verdict else "✗"

        msg = f"{emoji} DAILY PERFORMANCE REPORT\n"
        msg += f"{datetime.now().strftime('%Y-%m-%d')}\n\n"

        msg += f"TRADES: {report.core.total_trades}\n"
        msg += f"WIN RATE: {report.core.win_rate}%\n"
        msg += f"NET PnL: ₹{report.core.net_pnl:,.0f}\n"
        msg += f"PROFIT FACTOR: {report.core.profit_factor}\n"
        msg += f"EXPECTANCY: ₹{report.core.expectancy:.0f}\n\n"

        msg += f"MAX DD: ₹{report.core.max_drawdown:,.0f} ({report.core.max_drawdown_pct}%)\n\n"

        if report.strategies:
            best = report.strategies[0]
            msg += f"BEST: {best.strategy} (₹{best.net_pnl:,.0f})\n"

        msg += f"\nSTATUS: {report.verdict}"

        # Add warnings
        if report.warnings:
            msg += f"\n\n⚠ WARNINGS:\n"
            for warning in report.warnings[:2]:  # First 2
                msg += f"• {warning}\n"

        send_alert(msg)

        print("[TELEGRAM] Summary sent")

    except Exception as e:
        print(f"[TELEGRAM] Failed to send summary: {e}")


def main():
    print("=" * 60)
    print("AUTOMATED DAILY ANALYSIS")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("")

    # Analyze today's performance
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    print("Analyzing today's trades...")
    report = analyze_performance(
        start_date=today_start,
        end_date=datetime.now(),
        include_reality_check=True
    )

    print(f"Trades analyzed: {report.core.total_trades}")
    print(f"Net PnL: ₹{report.core.net_pnl:,.2f}")
    print(f"Verdict: {report.verdict}")
    print("")

    # Send Telegram summary
    if report.core.total_trades > 0:
        send_telegram_summary(report)
    else:
        print("No trades today — skipping Telegram notification")

    # Save detailed report
    timestamp = datetime.now().strftime("%Y%m%d")

    os.makedirs("data/reports", exist_ok=True)

    # Generate console report
    from generate_performance_report import format_console_report, save_json_report

    console_report = format_console_report(report)

    # Save to file
    report_file = f"data/reports/daily_{timestamp}.txt"
    with open(report_file, 'w') as f:
        f.write(console_report)

    print(f"Report saved: {report_file}")

    # Save JSON
    json_file = f"data/reports/daily_{timestamp}.json"
    save_json_report(report, json_file)
    print(f"JSON saved: {json_file}")

    print("")
    print("=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
