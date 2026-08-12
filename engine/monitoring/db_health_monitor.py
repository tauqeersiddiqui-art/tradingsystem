# engine/monitoring/db_health_monitor.py
#
# Database write failure visibility and alerting.
#
# Tracks DB write failures and alerts when persistence layer degrades.
# Critical: System continues trading without DB, but this is NOT SAFE for
# production — positions can be orphaned on restart.
#
# Alert thresholds:
#   - 5 failures in 5 minutes → WARNING
#   - 10 failures in 5 minutes → CRITICAL (DB effectively offline)

import logging
import time
from datetime import datetime, timedelta
from collections import deque
from typing import Deque, Dict, Any

logger = logging.getLogger("db_health")

# Failure tracking (sliding window)
_FAILURE_WINDOW_SECONDS = 300  # 5 minutes
_FAILURE_WARN_THRESHOLD = 5
_FAILURE_CRITICAL_THRESHOLD = 10

# Singleton state
_failure_log: Deque[float] = deque(maxlen=100)  # Timestamps of failures
_last_alert_sent: Dict[str, float] = {}  # Alert type → last sent timestamp
_alert_cooldown = 300  # 5 minutes between identical alerts


class DBHealthStatus:
    """Database health status snapshot."""

    def __init__(self):
        self.timestamp = datetime.now()
        self.status = "HEALTHY"
        self.failure_count_5min = 0
        self.total_failures = 0
        self.last_failure_at: datetime | None = None
        self.alert_level = "OK"


def record_db_write_failure(operation: str):
    """
    Record a database write failure.

    Args:
        operation: Operation that failed (e.g., "insert_order", "update_position")

    Side effects:
        - Logs error
        - Updates failure counter
        - Sends alert if threshold exceeded
    """
    now = time.time()
    _failure_log.append(now)

    # Count recent failures (sliding 5-minute window)
    cutoff = now - _FAILURE_WINDOW_SECONDS
    recent_failures = [ts for ts in _failure_log if ts >= cutoff]
    recent_count = len(recent_failures)

    logger.error(
        f"[DB_HEALTH] Write failure: {operation} "
        f"({recent_count} failures in last 5 min)"
    )

    # Alert on threshold breach
    if recent_count >= _FAILURE_CRITICAL_THRESHOLD:
        _send_db_alert("CRITICAL", recent_count)
    elif recent_count >= _FAILURE_WARN_THRESHOLD:
        _send_db_alert("WARNING", recent_count)


def get_db_health_status() -> DBHealthStatus:
    """
    Return current database health status.

    Returns:
        DBHealthStatus snapshot
    """
    status = DBHealthStatus()

    now = time.time()
    cutoff = now - _FAILURE_WINDOW_SECONDS

    # Count recent failures
    recent_failures = [ts for ts in _failure_log if ts >= cutoff]
    status.failure_count_5min = len(recent_failures)
    status.total_failures = len(_failure_log)

    # Last failure timestamp
    if _failure_log:
        status.last_failure_at = datetime.fromtimestamp(_failure_log[-1])

    # Determine status
    if status.failure_count_5min >= _FAILURE_CRITICAL_THRESHOLD:
        status.status = "CRITICAL"
        status.alert_level = "CRITICAL"
    elif status.failure_count_5min >= _FAILURE_WARN_THRESHOLD:
        status.status = "DEGRADED"
        status.alert_level = "WARNING"
    else:
        status.status = "HEALTHY"
        status.alert_level = "OK"

    return status


def _send_db_alert(severity: str, failure_count: int):
    """
    Send Telegram alert for DB health degradation.

    Deduplicates: Only sends once per severity level per cooldown period.
    """
    try:
        # Check cooldown
        now = time.time()
        last_sent = _last_alert_sent.get(severity, 0)

        if now - last_sent < _alert_cooldown:
            logger.debug(f"[DB_HEALTH] Alert cooldown active for {severity}")
            return

        # Send alert
        from telegram.notifier import send_alert

        emoji = "⚠️" if severity == "WARNING" else "🚨"
        threshold = (
            _FAILURE_WARN_THRESHOLD if severity == "WARNING"
            else _FAILURE_CRITICAL_THRESHOLD
        )

        msg = (
            f"{emoji} {severity}: DATABASE WRITE FAILURES\n\n"
            f"Failure count: {failure_count} in last 5 minutes\n"
            f"Threshold: {threshold}\n\n"
        )

        if severity == "CRITICAL":
            msg += (
                f"⚠️ SYSTEM RUNNING WITHOUT PERSISTENCE\n"
                f"Positions may be LOST on restart\n\n"
                f"Actions:\n"
                f"1. Check PostgreSQL service status\n"
                f"2. Check network connectivity\n"
                f"3. Check disk space\n"
                f"4. Consider stopping trading until DB restored"
            )
        else:
            msg += (
                f"Database writes are failing intermittently.\n"
                f"System continues trading but persistence is degraded.\n\n"
                f"Monitor logs and investigate DB connection."
            )

        send_alert(msg)
        _last_alert_sent[severity] = now

        logger.warning(f"[DB_HEALTH] {severity} alert sent")

    except Exception as e:
        logger.error(f"[DB_HEALTH] Failed to send alert: {e}")


def check_and_alert_if_unhealthy():
    """
    Check DB health and send alert if degraded.

    Call this periodically (e.g., every minute) to monitor health.
    """
    status = get_db_health_status()

    if status.status == "CRITICAL":
        _send_db_alert("CRITICAL", status.failure_count_5min)
    elif status.status == "DEGRADED":
        _send_db_alert("WARNING", status.failure_count_5min)


def format_health_report() -> str:
    """
    Format human-readable health report.

    Returns:
        Multi-line health status string
    """
    status = get_db_health_status()

    lines = [
        "Database Health Report",
        "=" * 50,
        f"Status: {status.status}",
        f"Alert Level: {status.alert_level}",
        f"Failures (5 min): {status.failure_count_5min}",
        f"Total Failures: {status.total_failures}",
    ]

    if status.last_failure_at:
        lines.append(f"Last Failure: {status.last_failure_at.strftime('%H:%M:%S')}")
    else:
        lines.append("Last Failure: None")

    if status.status == "CRITICAL":
        lines.append("")
        lines.append("⚠️  CRITICAL: System running without persistence")
        lines.append("   Positions may be lost on restart")
    elif status.status == "DEGRADED":
        lines.append("")
        lines.append("⚠️  WARNING: Intermittent DB write failures")

    return "\n".join(lines)


# Integration with postgres_client
def patch_postgres_client():
    """
    Patch postgres_client to track failures automatically.

    Call this once at startup to instrument the client.
    """
    from engine.storage import postgres_client

    # Wrap insert_order
    original_insert_order = postgres_client.PostgresClient.insert_order

    def _tracked_insert_order(self, *args, **kwargs):
        success = original_insert_order(self, *args, **kwargs)
        if not success:
            record_db_write_failure("insert_order")
        return success

    postgres_client.PostgresClient.insert_order = _tracked_insert_order

    # Wrap update_order_status
    original_update_order = postgres_client.PostgresClient.update_order_status

    def _tracked_update_order(self, *args, **kwargs):
        success = original_update_order(self, *args, **kwargs)
        if not success:
            record_db_write_failure("update_order_status")
        return success

    postgres_client.PostgresClient.update_order_status = _tracked_update_order

    # Wrap insert_position
    original_insert_pos = postgres_client.PostgresClient.insert_position

    def _tracked_insert_pos(self, *args, **kwargs):
        result = original_insert_pos(self, *args, **kwargs)
        if result is None:
            record_db_write_failure("insert_position")
        return result

    postgres_client.PostgresClient.insert_position = _tracked_insert_pos

    # Wrap close_position
    original_close_pos = postgres_client.PostgresClient.close_position

    def _tracked_close_pos(self, *args, **kwargs):
        success = original_close_pos(self, *args, **kwargs)
        if not success:
            record_db_write_failure("close_position")
        return success

    postgres_client.PostgresClient.close_position = _tracked_close_pos

    logger.info("[DB_HEALTH] PostgreSQL client instrumented for failure tracking")


# Background health check thread
def start_health_monitor(interval_seconds: int = 60):
    """
    Start background DB health monitor (runs every minute).

    Args:
        interval_seconds: Check interval (default 60s)

    Returns:
        Thread handle
    """
    import threading

    def _monitor_loop():
        logger.info(f"[DB_HEALTH] Monitor started (interval={interval_seconds}s)")
        while True:
            try:
                time.sleep(interval_seconds)
                check_and_alert_if_unhealthy()
            except Exception as e:
                logger.error(f"[DB_HEALTH] Monitor error: {e}")

    thread = threading.Thread(target=_monitor_loop, daemon=True, name="db_health")
    thread.start()
    return thread
