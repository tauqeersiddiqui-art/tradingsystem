@echo off
REM ────────────────────────────────────────────────────────
REM  start_trading_afternoon.bat  —  Afternoon session restart
REM
REM  Called by Windows Task Scheduler at 1:30 PM IST daily.
REM  Restarts the engine after lunch break with position resume.
REM
REM  What it does:
REM    1. Changes to the project directory
REM    2. Kills the morning engine
REM    3. Sets ALLOW_BROKER_POSITION_ON_START=1 for position continuity
REM    4. Starts supervisor.py (which starts master_runner.py)
REM ────────────────────────────────────────────────────────

cd /d "d:\All Bots\Trading_system"

REM Log the start
echo [%date% %time%] Afternoon session starting >> logs\autostart.log

REM Kill the morning engine (clean restart for afternoon)
if exist data\.master_runner.pid (
    set /p OLD_PID=<data\.master_runner.pid
    echo [%date% %time%] Killing morning engine PID %OLD_PID% >> logs\autostart.log
    taskkill /PID %OLD_PID% /F >nul 2>&1
    del data\.master_runner.pid >nul 2>&1
)

REM Set env var for afternoon session (resume open positions from morning)
set ALLOW_BROKER_POSITION_ON_START=1

REM Start supervisor in background (it will start master_runner.py)
echo [%date% %time%] Starting supervisor.py (afternoon) >> logs\autostart.log
start "TradingSupervisor_Afternoon" /MIN python -u scripts\supervisor.py >> logs\supervisor_console.log 2>&1

echo [%date% %time%] Afternoon supervisor launched >> logs\autostart.log
