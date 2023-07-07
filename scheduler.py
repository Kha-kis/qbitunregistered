#!/usr/bin/python3
import schedule
import time
import subprocess

def run_script():
    subprocess.run(['python3', 'qbitunregistered.py'])

# Schedule the script to run at the specified times
for scheduled_time in config.scheduled_times:
    schedule.every().day.at(scheduled_time).do(run_script)

# Run the scheduler loop
while True:
    schedule.run_pending()
    time.sleep(1)
