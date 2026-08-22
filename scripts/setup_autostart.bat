@echo off
REM ────────────────────────────────────────────────────────
REM  setup_autostart.bat  —  One-time setup for auto-start
REM
REM  Registers both morning (9:10 AM) and afternoon (1:30 PM)
REM  Windows Task Scheduler entries for Mon-Fri.
REM
REM  To remove later:
REM    schtasks /Delete /TN "TradingBot_Morning" /F
REM    schtasks /Delete /TN "TradingBot_Afternoon" /F
REM ────────────────────────────────────────────────────────

echo.
echo ============================================
echo   Trading Bot Auto-Start Setup
echo ============================================
echo.
echo  Morning session:   9:10 AM IST (Mon-Fri)
echo  Afternoon session: 1:30 PM IST (Mon-Fri)
echo.

REM ── Morning task ──
echo [1/2] Creating morning task...
schtasks /Delete /TN "TradingBot_Morning" /F >nul 2>&1
schtasks /Create ^
    /TN "TradingBot_Morning" ^
    /TR "d:\All Bots\Trading_system\scripts\start_trading.bat" ^
    /SC WEEKLY ^
    /ST 09:10 ^
    /D MON,TUE,WED,THU,FRI ^
    /F

if %ERRORLEVEL% equ 0 (
    echo       [OK] Morning task created
) else (
    echo       [FAIL] Morning task failed - run as Administrator
)

REM ── Afternoon task ──
echo [2/2] Creating afternoon task...
schtasks /Delete /TN "TradingBot_Afternoon" /F >nul 2>&1
schtasks /Create ^
    /TN "TradingBot_Afternoon" ^
    /TR "d:\All Bots\Trading_system\scripts\start_trading_afternoon.bat" ^
    /SC WEEKLY ^
    /ST 13:30 ^
    /D MON,TUE,WED,THU,FRI ^
    /F

if %ERRORLEVEL% equ 0 (
    echo       [OK] Afternoon task created
) else (
    echo       [FAIL] Afternoon task failed - run as Administrator
)

echo.
echo ============================================
echo   Summary
echo ============================================
echo.
schtasks /Query /TN "TradingBot_Morning" /FO TABLE /NH 2>nul
schtasks /Query /TN "TradingBot_Afternoon" /FO TABLE /NH 2>nul
echo.
echo To remove all:
echo   schtasks /Delete /TN "TradingBot_Morning" /F
echo   schtasks /Delete /TN "TradingBot_Afternoon" /F
echo.

pause
