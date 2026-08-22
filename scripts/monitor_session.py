# monitor_session.py
# Lightweight session monitor for master_runner.py.
#
# Tails logs/master_runner.log, extracts notable events (trades, exits,
# block reasons, errors, watchdog activity) and appends compact alerts to
# logs/monitor_session.log with wall-clock timestamps.
#
# Usage:
#   python scripts/monitor_session.py            # default: poll every 5 s
#   python scripts/monitor_session.py 10         # poll every 10 s
#
# Run alongside the engine (nohup ... &) and tail logs/monitor_session.log.

import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_LOG = os.path.join(BASE, "logs", "master_runner.log")
MON_LOG = os.path.join(BASE, "logs", "monitor_session.log")

POLL_S = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

# Lines that are interesting for session monitoring.
INTEREST = (
    "ENTRY", "EXIT", "STOP HIT", "TARGET", "LOCK", "BLOCK", "REJECT",
    "ERROR", "CRITICAL", "WARNING", "WATCHDOG", "PAUSED", "RECOVERY",
    "DRIFT", "resume", "halted", "BROKER SL", "SCALP ENTRY", "SCALP EXIT",
    "ATM", "FEED HEALTH", "alive", "Startup", "STARTED",
)

# Noisy lines to always skip (predictor spam etc.)
SKIP = (
    "low-conviction",
    "prob=",
)


def tail_events():
    pos = os.path.getsize(ENGINE_LOG) if os.path.exists(ENGINE_LOG) else 0
    while True:
        time.sleep(POLL_S)
        try:
            size = os.path.getsize(ENGINE_LOG)
        except OSError:
            continue
        if size < pos:
            pos = 0  # log rotated/truncated
        if size == pos:
            continue
        with open(ENGINE_LOG, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            lines = f.read().splitlines()
            pos = f.tell()

        for line in lines:
            if any(s in line for s in SKIP):
                continue
            if any(k in line for k in INTEREST):
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(MON_LOG, "a", encoding="utf-8") as m:
                    m.write(f"{now} | {line}\n")


if __name__ == "__main__":
    print(f"[MONITOR] watching {ENGINE_LOG} -> {MON_LOG} (poll {POLL_S}s)")
    tail_events()
