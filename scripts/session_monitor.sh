#!/bin/bash
# Session monitor - checks every 15 min until 3:30 PM IST
LOG="logs/master_runner.log"
while true; do
    HOUR=$(TZ="Asia/Kolkata" date +%H)
    MIN=$(TZ="Asia/Kolkata" date +%M)
    # Stop after 15:30
    if [ "$HOUR" -gt 15 ] || ([ "$HOUR" -eq 15 ] && [ "$MIN" -ge 30 ]); then
        echo "$(TZ='Asia/Kolkata' date '+%H:%M:%S') [MONITOR] Market closed. Stopping master_runner..."
        PID=$(cat data/.master_runner.pid 2>/dev/null)
        if [ -n "$PID" ]; then
            taskkill //F //PID $PID 2>/dev/null
            echo "$(TZ='Asia/Kolkata' date '+%H:%M:%S') [MONITOR] Killed PID $PID"
        fi
        echo "=== EOD SESSION SUMMARY ==="
        echo "Time: $(TZ='Asia/Kolkata' date '+%Y-%m-%d %H:%M:%S')"
        tail -5 "$LOG"
        break
    fi
    echo "$(TZ='Asia/Kolkata' date '+%H:%M:%S') [MONITOR] === CHECK ==="
    # Check if process is alive
    PID=$(cat data/.master_runner.pid 2>/dev/null)
    if [ -n "$PID" ] && tasklist 2>/dev/null | grep -q "$PID"; then
        echo "  Process: ALIVE (PID $PID)"
    else
        echo "  Process: DEAD! Restarting..."
        rm -f data/.master_runner.pid
        python master_runner.py &
        disown
        sleep 3
        echo "  Restarted. New PID: $(cat data/.master_runner.pid 2>/dev/null)"
    fi
    # Show today's trades
    echo "  === Today's trades ==="
    grep -E "(SCALP ENTRY|SCALP EXIT|EXPANSION.*ENTRY)" "$LOG" | grep "2026-08-20\|$(TZ='Asia/Kolkata' date '+%H:')" | tail -10
    # Show current PnL
    grep -E "pnl=|day=" "$LOG" | tail -5
    echo ""
    sleep 900  # 15 minutes
done
