# How to Restart Master Runner

## Current Status
- Master runner process is running with PID 3564
- PID file: `data/.master_runner.pid` contains "3564"

## Manual Restart Steps

### Option 1: Using Command Prompt
1. Open Command Prompt as Administrator
2. Navigate to the trading system directory:
   ```
   cd /d "D:\All Bots\trading_system"
   ```
3. Kill the existing process:
   ```
   taskkill /PID 3564 /F
   ```
4. Delete the PID file (if it exists):
   ```
   del data\.master_runner.pid
   ```
5. Start the master runner:
   ```
   start "" python master_runner.py
   ```
6. Verify it's running:
   ```
   tasklist | findstr master_runner
   ```

### Option 2: Using the Provided Batch File
1. Run `restart_master_runner.bat` as Administrator
2. Check `logs/master_runner.log` for output

### Option 3: Using the Provided Python Script
1. Run `python restart_master.py` 
2. Check `logs/master_runner.log` for output

## Verification
After restarting, you should see:
- New PID in `data/.master_runner.pid`
- Activity in `logs/master_runner.log`
- No "already running" errors when checking

## Notes
- The master runner prevents multiple instances via PID file checking
- If the process dies but PID file remains, you'll get a stale PID error
- Always check the log file for startup errors