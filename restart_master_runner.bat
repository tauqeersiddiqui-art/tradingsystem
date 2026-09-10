@echo off
echo Checking for existing master runner process...
FOR /F "tokens=2" %%A IN ('tasklist /FI "IMAGENAME eq python.exe" ^| findstr /I "master_runner"') DO (
    echo Found master runner process with PID: %%A
    taskkill /PID %%A /F
    echo Killed process %%A
)

echo Deleting PID file if exists...
if exist data\.master_runner.pid (
    del data\.master_runner.pid
    echo Deleted PID file
)

echo Starting master runner...
cd /d "D:\All Bots\trading_system"
start "" python master_runner.py

echo Master runner started successfully.
echo Check logs/master_runner.log for output.