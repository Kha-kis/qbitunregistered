#!/usr/bin/python3
"""
Scheduler for qbitunregistered script.

This script schedules the qbitunregistered.py script to run at specified times
each day based on the scheduled_times configuration in config.json.

Requirements:
    - config.json must be in the same directory as this script
    - qbitunregistered.py must be in the same directory as this script
    - Scheduled times must be in 24-hour format (HH:MM or HH:MM:SS)

Usage:
    python3 scheduler.py

The script will run continuously and execute qbitunregistered.py at the
configured times. Press Ctrl+C to stop the scheduler.
"""
import schedule
import time
import subprocess
import json
import os
import sys

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Paths relative to script location
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
MAIN_SCRIPT_PATH = os.path.join(SCRIPT_DIR, 'qbitunregistered.py')

def run_script():
    """Execute the qbitunregistered.py script with error handling and timeout."""
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting scheduled run of qbitunregistered.py")
        result = subprocess.run(
            [sys.executable, MAIN_SCRIPT_PATH],
            timeout=3600,  # 1 hour timeout
            check=True,
            capture_output=True,
            text=True
        )
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Completed scheduled run successfully")

        # Print stdout if there was any output
        if result.stdout:
            print(f"Output:\n{result.stdout}")

    except subprocess.TimeoutExpired:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Script execution timed out after 1 hour")
    except subprocess.CalledProcessError as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Script execution failed with exit code {e.returncode}")
        if e.stderr:
            print(f"Error output:\n{e.stderr}")
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Unexpected error running script: {type(e).__name__}: {e}")

# Load configuration from config.json (relative to this script)
try:
    with open(CONFIG_PATH, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    print(f"Error: The configuration file {CONFIG_PATH} was not found.")
    sys.exit(1)
except json.JSONDecodeError:
    print(f"Error: The configuration file {CONFIG_PATH} contains invalid JSON.")
    sys.exit(1)

# Schedule the script to run at the specified times
scheduled_times = config.get('scheduled_times', [])
if not scheduled_times:
    print("Warning: No scheduled_times found in config.json. Scheduler will not run any tasks.")
    sys.exit(0)

for scheduled_time in scheduled_times:
    try:
        schedule.every().day.at(scheduled_time).do(run_script)
    except schedule.ScheduleValueError as e:
        print(f"Error: Invalid time format '{scheduled_time}' in scheduled_times. {e}")
        sys.exit(1)

# Run the scheduler loop
print(f"Scheduler started. Next runs scheduled at: {', '.join(scheduled_times)}")
try:
    while True:
        schedule.run_pending()
        time.sleep(1)
except KeyboardInterrupt:
    print("\nScheduler stopped by user")
    sys.exit(0)
except Exception as e:
    print(f"Scheduler crashed with unexpected error: {e}")
    sys.exit(1)
