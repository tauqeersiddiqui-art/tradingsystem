#!/usr/bin/env python3
# scripts/analysis/generate_performance_report.py
#
# AUTOMATED PERFORMANCE REPORT GENERATOR
#
# Extracts truth from PostgreSQL trades table and generates complete
# profitability report with verdict.
#
# Usage:
#   python scripts/analysis/generate_performance_report.py
#   python scripts/analysis/generate_performance_report.py --days 7
#   python scripts/analysis/generate_performance_report.py --output json
#
# NO MANUAL INTERVENTION. FULLY AUTOMATED.

import os
import sys
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.analytics.performance_analyzer import (
    analyze_performance,
    PerformanceReport,
    CoreMetrics,
    StrategyMetrics,
    TimeMetrics,
    RegimeMetrics
)


def format_console_report(report: PerformanceReport) -> str:
    """Format report for console output."""

    lines = [
        "",
        "=" * 80,
        "TRADING SYSTEM PERFORMANCE REPORT",
        "=" * 80,
        "",
        f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Period: {report.date_range[0].strftime('%Y-%m-%d')} to {report.date_range[1].strftime('%Y-%m-%d')}",
        "",
        "━" * 80,
        "CORE METRICS",
        "━" * 80,
        "",
        f"Total Trades:        {report.core.total_trades}",
        f"Win Rate:            {report.core.win_rate}%",
        f"Winners:             {report.core.winning_trades}",
        f"Losers:              {report.core.losing_trades}",
        "",
        f"Net PnL:             ₹{report.core.net_pnl:,.2f}",
        f"Gross PnL:           ₹{report.core.gross_pnl:,.2f}",
        f"Total Costs:         ₹{report.core.total_costs:,.2f}",
        "",
        f"Profit Factor:       {report.core.profit_factor}",
        f"Expectancy:          ₹{report.core.expectancy:.2f}",
        f"Risk/Reward:         {report.core.risk_reward_ratio}",
        "",
        f"Avg Win:             ₹{report.core.avg_win:,.2f}",
        f"Avg Loss:            ₹{report.core.avg_loss:,.2f}",
        f"Largest Win:         ₹{report.core.largest_win:,.2f}",
        f"Largest Loss:        ₹{report.core.largest_loss:,.2f}",
        "",
        f"Max Drawdown:        ₹{report.core.max_drawdown:,.2f} ({report.core.max_drawdown_pct}%)",
        f"Consecutive Losses:  {report.core.consecutive_losses}",
        "",
    ]

    # Strategy breakdown
    if report.strategies:
        lines.extend([
            "━" * 80,
            "STRATEGY ANALYSIS",
            "━" * 80,
            ""
        ])

        for strat in report.strategies:
            lines.append(f"{strat.strategy}:")
            lines.append(f"  Trades:         {strat.trades}")
            lines.append(f"  Win Rate:       {strat.win_rate}%")
            lines.append(f"  Net PnL:        ₹{strat.net_pnl:,.2f}")
            lines.append(f"  Expectancy:     ₹{strat.expectancy:.2f}")
            lines.append(f"  Profit Factor:  {strat.profit_factor}")
            lines.append(f"  Max DD:         ₹{strat.max_drawdown:,.2f}")
            lines.append("")

        best = report.strategies[0]
        worst = report.strategies[-1]
        lines.append(f"Best Strategy:  {best.strategy} (₹{best.net_pnl:,.2f})")
        lines.append(f"Worst Strategy: {worst.strategy} (₹{worst.net_pnl:,.2f})")
        lines.append("")

    # Time analysis
    lines.extend([
        "━" * 80,
        "TIME ANALYSIS",
        "━" * 80,
        "",
        f"First Hour (9:15-10:15):  ₹{report.time.first_hour_pnl:,.2f}",
        f"Rest of Day:              ₹{report.time.rest_of_day_pnl:,.2f}",
        "",
        f"Best Hour:                {report.time.best_hour[0]:02d}:00 (₹{report.time.best_hour[1]:,.2f})",
        f"Worst Hour:               {report.time.worst_hour[0]:02d}:00 (₹{report.time.worst_hour[1]:,.2f})",
        "",
        "PnL by Hour:"
    ])

    for hour in sorted(report.time.pnl_by_hour.keys()):
        pnl = report.time.pnl_by_hour[hour]
        count = report.time.trades_by_hour.get(hour, 0)
        lines.append(f"  {hour:02d}:00  ₹{pnl:>8,.2f}  ({count} trades)")

    lines.append("")
    lines.append("PnL by Weekday:")
    for day, pnl in report.time.pnl_by_weekday.items():
        lines.append(f"  {day:<10}  ₹{pnl:,.2f}")

    lines.append("")

    # Regime analysis
    if report.regimes:
        lines.extend([
            "━" * 80,
            "MARKET REGIME ANALYSIS",
            "━" * 80,
            ""
        ])

        for regime in report.regimes:
            lines.append(f"{regime.regime}:")
            lines.append(f"  Trades:      {regime.trades}")
            lines.append(f"  Win Rate:    {regime.win_rate}%")
            lines.append(f"  Net PnL:     ₹{regime.net_pnl:,.2f}")
            lines.append(f"  Expectancy:  ₹{regime.expectancy:.2f}")
            lines.append("")

    # Reality check
    if report.reality:
        lines.extend([
            "━" * 80,
            "REALITY CHECK (Execution Quality)",
            "━" * 80,
            "",
            f"Theoretical Edge (Gross):  ₹{report.reality.theoretical_edge:,.2f}",
            f"Actual Edge (Net):         ₹{report.reality.actual_edge:,.2f}",
            f"Slippage Cost:             ₹{report.reality.slippage_cost:,.2f}",
            f"Slippage Impact:           {report.reality.slippage_impact_pct}% of gross",
            f"Avg Slippage:              {report.reality.avg_slippage_pts} pts",
            f"Execution Quality Score:   {report.reality.execution_quality_score}/100",
            ""
        ])

    # Drawdown details
    lines.extend([
        "━" * 80,
        "DRAWDOWN ANALYSIS",
        "━" * 80,
        "",
        f"Max Drawdown:           ₹{report.drawdown.max_drawdown:,.2f}",
        f"Max Drawdown %:         {report.drawdown.max_drawdown_pct}%",
        f"Drawdown Period:        {report.drawdown.max_drawdown_start.strftime('%Y-%m-%d')} to {report.drawdown.max_drawdown_end.strftime('%Y-%m-%d')}",
        f"Longest Losing Streak:  {report.drawdown.consecutive_losses} trades",
        ""
    ])

    if report.drawdown.longest_losing_streak:
        lines.append("Worst Losing Streak:")
        for i, trade in enumerate(report.drawdown.longest_losing_streak[:5], 1):
            lines.append(f"  {i}. {trade['symbol']}: ₹{trade['net_pnl']:.2f} at {trade['time'].strftime('%H:%M')}")
        lines.append("")

    # Warnings
    if report.warnings:
        lines.extend([
            "━" * 80,
            "⚠  WARNINGS",
            "━" * 80,
            ""
        ])
        for warning in report.warnings:
            lines.append(f"⚠  {warning}")
        lines.append("")

    # Recommendations
    if report.recommendations:
        lines.extend([
            "━" * 80,
            "→  RECOMMENDATIONS",
            "━" * 80,
            ""
        ])
        for rec in report.recommendations:
            lines.append(f"→  {rec}")
        lines.append("")

    # Final verdict
    lines.extend([
        "━" * 80,
        "FINAL VERDICT",
        "━" * 80,
        ""
    ])

    if report.verdict == "PROFITABLE & STABLE":
        lines.append("✓ SYSTEM STATUS: PROFITABLE & STABLE")
        lines.append("")
        lines.append("  System makes money consistently with acceptable risk.")
        lines.append("  Continue trading with current parameters.")
    elif report.verdict == "PROFITABLE BUT UNSTABLE":
        lines.append("⚠ SYSTEM STATUS: PROFITABLE BUT UNSTABLE")
        lines.append("")
        lines.append("  System is profitable but exhibits high volatility.")
        lines.append("  Reduce position size or tighten risk controls.")
    elif report.verdict == "BREAK-EVEN":
        lines.append("⚠ SYSTEM STATUS: BREAK-EVEN")
        lines.append("")
        lines.append("  System neither makes nor loses money significantly.")
        lines.append("  Re-evaluate strategy parameters.")
    elif report.verdict == "LOSING SYSTEM":
        lines.append("✗ SYSTEM STATUS: LOSING SYSTEM")
        lines.append("")
        lines.append("  System loses money. DO NOT TRADE.")
        lines.append("  Fundamental strategy flaw detected.")
    else:
        lines.append(f"? SYSTEM STATUS: {report.verdict}")

    lines.append("")
    lines.append("=" * 80)

    return "\n".join(lines)


def save_csv_report(report: PerformanceReport, filename: str):
    """Save report as CSV."""
    import csv

    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)

        # Core metrics
        writer.writerow(["Core Metrics"])
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Total Trades", report.core.total_trades])
        writer.writerow(["Win Rate %", report.core.win_rate])
        writer.writerow(["Net PnL", report.core.net_pnl])
        writer.writerow(["Gross PnL", report.core.gross_pnl])
        writer.writerow(["Profit Factor", report.core.profit_factor])
        writer.writerow(["Expectancy", report.core.expectancy])
        writer.writerow(["Max Drawdown", report.core.max_drawdown])
        writer.writerow(["Max Drawdown %", report.core.max_drawdown_pct])
        writer.writerow([])

        # Strategy breakdown
        writer.writerow(["Strategy Analysis"])
        writer.writerow(["Strategy", "Trades", "Win Rate %", "Net PnL", "Expectancy", "Profit Factor"])
        for strat in report.strategies:
            writer.writerow([
                strat.strategy, strat.trades, strat.win_rate,
                strat.net_pnl, strat.expectancy, strat.profit_factor
            ])
        writer.writerow([])

        # Time analysis
        writer.writerow(["Time Analysis - By Hour"])
        writer.writerow(["Hour", "PnL", "Trades"])
        for hour in sorted(report.time.pnl_by_hour.keys()):
            writer.writerow([
                f"{hour:02d}:00",
                report.time.pnl_by_hour[hour],
                report.time.trades_by_hour.get(hour, 0)
            ])
        writer.writerow([])

        # Verdict
        writer.writerow(["Verdict"])
        writer.writerow([report.verdict])


def save_json_report(report: PerformanceReport, filename: str):
    """Save report as JSON."""

    # Convert dataclasses to dict
    from dataclasses import asdict

    data = asdict(report)

    # Convert datetime objects
    def convert_datetime(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, tuple) and len(obj) == 2:
            return [convert_datetime(obj[0]), convert_datetime(obj[1])]
        elif isinstance(obj, list):
            return [convert_datetime(item) for item in obj]
        elif isinstance(obj, dict):
            return {k: convert_datetime(v) for k, v in obj.items()}
        return obj

    data = convert_datetime(data)

    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Generate automated trading performance report"
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Analyze last N days (default: all data)"
    )
    parser.add_argument(
        "--output",
        choices=["console", "csv", "json", "all"],
        default="console",
        help="Output format (default: console)"
    )
    parser.add_argument(
        "--file",
        help="Output filename (auto-generated if not specified)"
    )

    args = parser.parse_args()

    # Calculate date range
    end_date = datetime.now()
    start_date = None

    if args.days:
        start_date = end_date - timedelta(days=args.days)

    # Generate report
    print("Analyzing performance from database...")
    print("")

    report = analyze_performance(start_date, end_date, include_reality_check=True)

    # Output
    if args.output in ["console", "all"]:
        print(format_console_report(report))

    # Generate filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = args.file or f"performance_report_{timestamp}"

    # Ensure data/reports directory exists
    os.makedirs("data/reports", exist_ok=True)

    if args.output in ["csv", "all"]:
        csv_file = f"data/reports/{base_filename}.csv"
        save_csv_report(report, csv_file)
        print(f"\nCSV report saved: {csv_file}")

    if args.output in ["json", "all"]:
        json_file = f"data/reports/{base_filename}.json"
        save_json_report(report, json_file)
        print(f"JSON report saved: {json_file}")

    # Exit with status code based on verdict
    if report.verdict == "LOSING SYSTEM":
        sys.exit(1)
    elif report.verdict in ["BREAK-EVEN", "PROFITABLE BUT UNSTABLE"]:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
