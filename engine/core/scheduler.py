# engine/core/scheduler.py
#
# Automated task scheduler for daily analysis and monitoring.
#
# Runs:
#   - Daily performance analysis at 4:00 PM (after market close)
#   - Weekly summary on Fridays
#   - Report cleanup (30-day retention)

import logging
from datetime import datetime

logger = logging.getLogger("scheduler")


def setup_daily_analysis() -> str:
    """
    Setup daily automated analysis at 4:00 PM.

    Returns:
        Job ID for the scheduled task
    """
    try:
        from engine.core import cron_manager

        # Daily at 4:00 PM IST (weekdays only)
        job_id = cron_manager.schedule_task(
            name="daily_performance_analysis",
            cron="0 16 * * 1-5",  # 4:00 PM Monday-Friday
            command="python scripts/analysis/run_daily_analysis.py",
            description="Daily performance report generation"
        )

        logger.info(f"[SCHEDULER] Daily analysis scheduled: {job_id}")
        return job_id

    except Exception as e:
        logger.error(f"[SCHEDULER] Failed to schedule daily analysis: {e}")
        raise


def setup_weekly_summary() -> str:
    """
    Setup weekly summary report on Fridays at 4:30 PM.

    Returns:
        Job ID for the scheduled task
    """
    try:
        from engine.core import cron_manager

        # Fridays at 4:30 PM IST
        job_id = cron_manager.schedule_task(
            name="weekly_performance_summary",
            cron="30 16 * * 5",  # 4:30 PM Friday
            command="python scripts/analysis/generate_performance_report.py --days 7 --output all",
            description="Weekly performance summary"
        )

        logger.info(f"[SCHEDULER] Weekly summary scheduled: {job_id}")
        return job_id

    except Exception as e:
        logger.error(f"[SCHEDULER] Failed to schedule weekly summary: {e}")
        raise


def setup_report_cleanup() -> str:
    """
    Setup automated cleanup of old reports (30-day retention).

    Returns:
        Job ID for the scheduled task
    """
    try:
        from engine.core import cron_manager

        # Daily at 11:00 PM
        job_id = cron_manager.schedule_task(
            name="report_cleanup",
            cron="0 23 * * *",  # 11:00 PM daily
            command="find data/reports -name '*.txt' -mtime +30 -delete",
            description="Cleanup reports older than 30 days"
        )

        logger.info(f"[SCHEDULER] Report cleanup scheduled: {job_id}")
        return job_id

    except Exception as e:
        logger.error(f"[SCHEDULER] Failed to schedule cleanup: {e}")
        raise
