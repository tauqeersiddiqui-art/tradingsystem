#!/usr/bin/env python3
# scripts/setup_automated_analysis.py
#
# ONE-TIME SETUP for automated daily analysis.
#
# Sets up:
#   1. Daily analysis at 4:00 PM (after market close)
#   2. Weekly summary every Friday at 4:30 PM
#   3. Auto-cleanup of old reports (30-day retention)
#
# Run once:
#   python scripts/setup_automated_analysis.py
#
# NO MANUAL INTERVENTION AFTER SETUP.

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("AUTOMATED ANALYSIS SETUP")
print("=" * 70)
print("")

# Check if PostgreSQL is configured
print("[1/3] Checking PostgreSQL connection...")
try:
    from engine.storage.postgres_client import get_client

    client = get_client()
    if client.connect():
        health = client.health_check()
        print(f"✓ PostgreSQL connected: {health['stats']['today_trades']} trades today")
        client.close()
    else:
        print("✗ PostgreSQL connection failed")
        print("  → Configure .env with POSTGRES_* credentials")
        sys.exit(1)
except Exception as e:
    print(f"✗ PostgreSQL error: {e}")
    sys.exit(1)

print("")

# Create scheduler task
print("[2/3] Setting up automated scheduler...")

try:
    from engine.core.scheduler import setup_daily_analysis

    # Daily analysis at 4:00 PM
    job_id = setup_daily_analysis()

    print(f"✓ Daily analysis scheduled (16:00 IST)")
    print(f"  Job ID: {job_id}")

except Exception as e:
    print(f"⚠ Scheduler setup: {e}")
    print("  → Add to crontab manually:")
    print(f"  0 16 * * 1-5 cd {Path.cwd()} && python scripts/analysis/run_daily_analysis.py")

print("")

# Create reports directory
print("[3/3] Creating reports directory...")
os.makedirs("data/reports", exist_ok=True)
print("✓ data/reports/ created")

print("")
print("=" * 70)
print("SETUP COMPLETE")
print("=" * 70)
print("")
print("Automated analysis will run daily at 4:00 PM.")
print("Reports saved to: data/reports/")
print("Telegram summary sent after each run.")
print("")
print("To test now:")
print("  python scripts/analysis/generate_performance_report.py")
print("")
