@echo off
REM ────────────────────────────────────────────────────────
REM  start_trading.bat  —  Start trading session
REM
REM  Called by Windows Task Scheduler at 9:10 AM IST daily.
REM  Can also be double-clicked to start manually.
REM
REM  What it does:
REM    1. Changes to the project directory
REM    2. Kills any stale engine from yesterday
REM    3. Starts supervisor.py (which starts master_runner.py)
REM ────────────────────────────────────────────────────────

cd /d "d:\All Bots\Trading_system"

REM Log the start
echo [%date% %time%] Trading session starting >> logs\autostart.log

REM Kill any stale engine from yesterday (safe — new day, new session)
if exist data\.master_runner.pid (
    set /p OLD_PID=<data\.master_runner.pid
    echo [%date% %time%] Killing stale engine PID %OLD_PID% >> logs\autostart.log
    taskkill /PID %OLD_PID% /F >nul 2>&1
    del data\.master_runner.pid >nul 2>&1
)

REM Start supervisor in background (it will start master_runner.py)
echo [%date% %time%] Starting supervisor.py >> logs\autostart.log
start "TradingSupervisor" /MIN python -u scripts\supervisor.py >> logs\supervisor_console.log 2>&1

echo [%date% %time%] Supervisor launched >> logs\autostart.log
