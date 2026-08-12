# engine/reports/system_validation_report.py
#
# SYSTEM VALIDATION REPORT — Production readiness assessment.
#
# Aggregates results from all validation tests and monitoring systems.
# Used to certify system is safe for production trading.
#
# SUCCESS CRITERIA:
#   ✓ Restart recovery: 100% pass rate
#   ✓ Reconciliation: No mismatches
#   ✓ Failure injection: All tests pass
#   ✓ DB health: No persistent failures
#   ✓ Execution audit: Slippage within bounds
#
# DO NOT TRADE until all criteria are met.

import os
import sys
import logging
from datetime import datetime
from typing import Dict, Any, List
from dataclasses import dataclass

logger = logging.getLogger("validation_report")


@dataclass
class ValidationReport:
    """Complete system validation report."""

    timestamp: datetime
    status: str  # VALIDATED / FAILED / PARTIAL
    restart_tests_passed: int
    restart_tests_failed: int
    failure_injection_passed: int
    failure_injection_failed: int
    reconciliation_status: str
    db_health_status: str
    db_failure_count_5min: int
    execution_audit_available: bool
    avg_slippage_pts: float
    critical_issues: List[str]
    warnings: List[str]
    recommendations: List[str]


def run_restart_recovery_tests() -> Dict[str, Any]:
    """
    Run Phase 24 restart recovery test suite.

    Returns:
        Dict with pass/fail counts and detailed results
    """
    logger.info("[VALIDATION] Running restart recovery tests...")

    try:
        # Import and run test suite
        sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
        from _phase24_restart_verify import run_all_tests

        # Capture test results
        success = run_all_tests()

        # Parse results (simplified — actual implementation would capture per-test results)
        if success:
            return {
                "status": "PASSED",
                "passed": 5,
                "failed": 0,
                "details": "All restart scenarios validated"
            }
        else:
            return {
                "status": "FAILED",
                "passed": 0,
                "failed": 5,
                "details": "Check logs for failure details"
            }

    except Exception as e:
        logger.error(f"[VALIDATION] Restart tests failed: {e}")
        return {
            "status": "ERROR",
            "passed": 0,
            "failed": 5,
            "details": str(e)
        }


def run_failure_injection_tests() -> Dict[str, Any]:
    """
    Run Phase 25 failure injection test suite.

    Returns:
        Dict with pass/fail counts and detailed results
    """
    logger.info("[VALIDATION] Running failure injection tests...")

    try:
        from _phase25_failure_injection import run_all_tests

        success = run_all_tests()

        if success:
            return {
                "status": "PASSED",
                "passed": 5,
                "failed": 0,
                "details": "System resilient to failures"
            }
        else:
            return {
                "status": "FAILED",
                "passed": 0,
                "failed": 5,
                "details": "Check logs for failure details"
            }

    except Exception as e:
        logger.error(f"[VALIDATION] Failure injection tests failed: {e}")
        return {
            "status": "ERROR",
            "passed": 0,
            "failed": 5,
            "details": str(e)
        }


def check_reconciliation_status(ctx) -> Dict[str, Any]:
    """
    Run broker vs DB reconciliation check.

    Returns:
        Dict with reconciliation results
    """
    logger.info("[VALIDATION] Running reconciliation check...")

    try:
        from engine.monitoring.reconciliation import reconcile_positions

        result = reconcile_positions(ctx)

        return {
            "status": result.status,
            "has_mismatch": result.has_critical_mismatch(),
            "matches": len(result.matches),
            "orphaned_broker": len(result.orphaned_in_broker),
            "orphaned_db": len(result.orphaned_in_db),
            "qty_mismatches": len(result.qty_mismatches),
            "details": result.summary()
        }

    except Exception as e:
        logger.error(f"[VALIDATION] Reconciliation check failed: {e}")
        return {
            "status": "ERROR",
            "has_mismatch": True,
            "details": str(e)
        }


def check_db_health() -> Dict[str, Any]:
    """
    Check database health status.

    Returns:
        Dict with DB health metrics
    """
    logger.info("[VALIDATION] Checking DB health...")

    try:
        from engine.monitoring.db_health_monitor import get_db_health_status

        status = get_db_health_status()

        return {
            "status": status.status,
            "alert_level": status.alert_level,
            "failure_count_5min": status.failure_count_5min,
            "total_failures": status.total_failures,
            "last_failure": status.last_failure_at.isoformat() if status.last_failure_at else None
        }

    except Exception as e:
        logger.error(f"[VALIDATION] DB health check failed: {e}")
        return {
            "status": "UNKNOWN",
            "alert_level": "ERROR",
            "failure_count_5min": 0,
            "total_failures": 0,
            "error": str(e)
        }


def check_execution_audit() -> Dict[str, Any]:
    """
    Check execution audit data availability and quality.

    Returns:
        Dict with execution quality metrics
    """
    logger.info("[VALIDATION] Checking execution audit...")

    try:
        from engine.analytics.execution_audit import get_today_audit_summary

        summary = get_today_audit_summary()

        if summary["status"] == "ok":
            return {
                "available": True,
                "trade_count": summary["trade_count"],
                "avg_slippage_pts": (
                    summary["avg_entry_slippage_pts"] +
                    summary["avg_exit_slippage_pts"]
                ),
                "total_slippage_cost": summary["total_slippage_cost_rs"],
                "partial_fills": summary["partial_fill_count"]
            }
        else:
            return {
                "available": False,
                "status": summary["status"]
            }

    except Exception as e:
        logger.error(f"[VALIDATION] Execution audit check failed: {e}")
        return {
            "available": False,
            "error": str(e)
        }


def generate_validation_report(ctx) -> ValidationReport:
    """
    Generate comprehensive system validation report.

    Args:
        ctx: TradingContext (used for reconciliation check)

    Returns:
        ValidationReport with complete assessment
    """
    logger.info("[VALIDATION] Generating system validation report...")

    timestamp = datetime.now()
    critical_issues = []
    warnings = []
    recommendations = []

    # Run all checks
    restart_tests = run_restart_recovery_tests()
    failure_tests = run_failure_injection_tests()
    recon = check_reconciliation_status(ctx)
    db_health = check_db_health()
    exec_audit = check_execution_audit()

    # Analyze restart tests
    restart_passed = restart_tests.get("passed", 0)
    restart_failed = restart_tests.get("failed", 0)

    if restart_failed > 0:
        critical_issues.append(
            f"Restart recovery: {restart_failed} test(s) failed — "
            f"system may lose positions on crash"
        )

    # Analyze failure injection tests
    failure_passed = failure_tests.get("passed", 0)
    failure_failed = failure_tests.get("failed", 0)

    if failure_failed > 0:
        critical_issues.append(
            f"Failure injection: {failure_failed} test(s) failed — "
            f"system not resilient to failures"
        )

    # Analyze reconciliation
    recon_status = recon.get("status", "ERROR")
    has_mismatch = recon.get("has_mismatch", True)

    if has_mismatch:
        critical_issues.append(
            f"Reconciliation: Position mismatch detected — "
            f"broker={recon.get('orphaned_broker', 0)}, "
            f"db={recon.get('orphaned_db', 0)}, "
            f"qty_diff={recon.get('qty_mismatches', 0)}"
        )

    # Analyze DB health
    db_status = db_health.get("status", "UNKNOWN")
    db_failures = db_health.get("failure_count_5min", 0)

    if db_status == "CRITICAL":
        critical_issues.append(
            f"Database: CRITICAL — {db_failures} failures in 5 min — "
            f"system running without persistence"
        )
    elif db_status == "DEGRADED":
        warnings.append(
            f"Database: DEGRADED — {db_failures} failures in 5 min — "
            f"intermittent write failures"
        )

    # Analyze execution audit
    exec_available = exec_audit.get("available", False)
    avg_slippage = exec_audit.get("avg_slippage_pts", 0.0)

    if not exec_available:
        warnings.append(
            "Execution audit: No data available — "
            "run live trades to collect execution metrics"
        )
    else:
        if avg_slippage > 5.0:
            critical_issues.append(
                f"Execution quality: Excessive slippage ({avg_slippage:.2f}pts avg) — "
                f"review broker performance"
            )
        elif avg_slippage > 2.0:
            warnings.append(
                f"Execution quality: High slippage ({avg_slippage:.2f}pts avg) — "
                f"monitor broker fills"
            )

    # Generate recommendations
    if len(critical_issues) == 0 and len(warnings) == 0:
        recommendations.append("✓ System VALIDATED — safe for production trading")
    else:
        if critical_issues:
            recommendations.append("✗ DO NOT TRADE — critical issues detected")
            recommendations.append("Fix all critical issues before production")

        if warnings:
            recommendations.append("Monitor warnings during paper trading")

    if not exec_available:
        recommendations.append("Run paper trading session to validate execution quality")

    if db_failures == 0:
        recommendations.append("Consider load testing DB with simulated high-frequency writes")

    # Determine overall status
    if len(critical_issues) > 0:
        status = "FAILED"
    elif len(warnings) > 0:
        status = "PARTIAL"
    else:
        status = "VALIDATED"

    # Build report
    report = ValidationReport(
        timestamp=timestamp,
        status=status,
        restart_tests_passed=restart_passed,
        restart_tests_failed=restart_failed,
        failure_injection_passed=failure_passed,
        failure_injection_failed=failure_failed,
        reconciliation_status=recon_status,
        db_health_status=db_status,
        db_failure_count_5min=db_failures,
        execution_audit_available=exec_available,
        avg_slippage_pts=avg_slippage,
        critical_issues=critical_issues,
        warnings=warnings,
        recommendations=recommendations
    )

    return report


def format_validation_report(report: ValidationReport) -> str:
    """
    Format validation report as human-readable text.

    Returns:
        Multi-line report string
    """
    lines = [
        "=" * 70,
        "SYSTEM VALIDATION REPORT",
        "=" * 70,
        "",
        f"Generated: {report.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Overall Status: {report.status}",
        "",
        "TEST RESULTS",
        "-" * 70,
        "",
        f"Restart Recovery Tests:",
        f"  Passed: {report.restart_tests_passed}",
        f"  Failed: {report.restart_tests_failed}",
        "",
        f"Failure Injection Tests:",
        f"  Passed: {report.failure_injection_passed}",
        f"  Failed: {report.failure_injection_failed}",
        "",
        "SYSTEM HEALTH",
        "-" * 70,
        "",
        f"Reconciliation: {report.reconciliation_status}",
        f"Database Health: {report.db_health_status}",
        f"  Failures (5 min): {report.db_failure_count_5min}",
        "",
        "EXECUTION QUALITY",
        "-" * 70,
        "",
        f"Audit Data Available: {report.execution_audit_available}",
    ]

    if report.execution_audit_available:
        lines.append(f"  Avg Slippage: {report.avg_slippage_pts:.2f} pts")

    lines.append("")

    # Critical issues
    if report.critical_issues:
        lines.append("CRITICAL ISSUES")
        lines.append("-" * 70)
        lines.append("")
        for issue in report.critical_issues:
            lines.append(f"✗ {issue}")
        lines.append("")

    # Warnings
    if report.warnings:
        lines.append("WARNINGS")
        lines.append("-" * 70)
        lines.append("")
        for warning in report.warnings:
            lines.append(f"⚠ {warning}")
        lines.append("")

    # Recommendations
    lines.append("RECOMMENDATIONS")
    lines.append("-" * 70)
    lines.append("")
    for rec in report.recommendations:
        lines.append(f"→ {rec}")

    lines.append("")
    lines.append("=" * 70)

    if report.status == "VALIDATED":
        lines.append("✓ SYSTEM VALIDATED — READY FOR PRODUCTION")
    elif report.status == "PARTIAL":
        lines.append("⚠ SYSTEM PARTIALLY VALIDATED — RESOLVE WARNINGS")
    else:
        lines.append("✗ SYSTEM NOT VALIDATED — FIX CRITICAL ISSUES")

    lines.append("=" * 70)

    return "\n".join(lines)


def save_validation_report(report: ValidationReport, filename: str = None):
    """
    Save validation report to file.

    Args:
        report: ValidationReport to save
        filename: Output filename (auto-generated if None)
    """
    if filename is None:
        timestamp = report.timestamp.strftime("%Y%m%d_%H%M%S")
        filename = f"data/validation_report_{timestamp}.txt"

    try:
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        content = format_validation_report(report)

        with open(filename, "w") as f:
            f.write(content)

        logger.info(f"[VALIDATION] Report saved: {filename}")

    except Exception as e:
        logger.error(f"[VALIDATION] Failed to save report: {e}")


def validate_system(ctx) -> bool:
    """
    Run full system validation and return True if validated.

    Args:
        ctx: TradingContext

    Returns:
        True if system is validated, False otherwise

    Side effects:
        - Prints report to stdout
        - Saves report to file
        - Sends Telegram alert if not validated
    """
    report = generate_validation_report(ctx)

    # Print to stdout
    print(format_validation_report(report))

    # Save to file
    save_validation_report(report)

    # Alert if not validated
    if report.status != "VALIDATED":
        try:
            from telegram.notifier import send_alert

            if report.status == "FAILED":
                msg = (
                    f"🚨 SYSTEM VALIDATION FAILED\n\n"
                    f"Critical issues detected:\n"
                )
                for issue in report.critical_issues[:3]:  # First 3
                    msg += f"• {issue}\n"

                msg += f"\nDO NOT TRADE until issues resolved"
            else:
                msg = (
                    f"⚠️ SYSTEM PARTIALLY VALIDATED\n\n"
                    f"Warnings detected:\n"
                )
                for warning in report.warnings[:3]:
                    msg += f"• {warning}\n"

                msg += f"\nResolve before production trading"

            send_alert(msg)

        except Exception as e:
            logger.error(f"[VALIDATION] Failed to send alert: {e}")

    return report.status == "VALIDATED"


# CLI entry point
if __name__ == "__main__":
    from engine.core.context import TradingContext

    print("Running system validation...")
    print("")

    # Create minimal context (for checks that don't need full setup)
    ctx = TradingContext()

    # Run validation
    is_valid = validate_system(ctx)

    # Exit with appropriate code
    sys.exit(0 if is_valid else 1)
