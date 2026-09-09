import os
import subprocess
import time

# Kill existing master runner process
print("Checking for existing master runner process...")
result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe'], capture_output=True, text=True, shell=True)
if 'master_runner' in result.stdout:
    lines = result.stdout.strip().split('\n')
    for line in lines:
        if 'master_runner' in line and 'python.exe' in line:
            parts = line.split()
            # Find the PID (usually the second column)
            for i, part in enumerate(parts):
                if part.isdigit() and i > 0:
                    pid = part
                    print(f"Found master runner process with PID: {pid}")
                    subprocess.run(['taskkill', '/PID', pid, '/F'], shell=True)
                    print(f"Killed process {pid}")
                    break

# Delete PID file if exists
pid_file = r'D:\All Bots\trading_system\.master_runner.pid'
if os.path.exists(pid_file):
    os.remove(pid_file)
    print("Deleted PID file")

# Start master runner
print("Starting master runner...")
os.chdir(r'D:\All Bots\trading_system')
subprocess.Popen(['python', 'master_runner.py'],
                 stdout=open(r'logs\master_runner.log', 'a'),
                 stderr=subprocess.STDOUT)

print("Master runner started successfully.")
print("Check logs/master_runner.log for output.")