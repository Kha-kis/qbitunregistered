#!/usr/bin/python3
"""
Scheduler for qbitunregistered script.

This module schedules qbitunregistered to run at specified times
each day based on the scheduled_times configuration in config.json.

Requirements:
    - The selected config must define scheduled_times
    - Scheduled times must be in 24-hour format (HH:MM or HH:MM:SS)

Usage:
    qbitunregistered-scheduler --config /path/to/config.json

The scheduler runs continuously and executes qbitunregistered with the selected
configured times. Press Ctrl+C to stop the scheduler.
"""

import argparse
import schedule
import time
import subprocess
import json
import sys
from pathlib import Path


def run_script(config_path: str | Path) -> None:
    """Execute qbitunregistered with the scheduler's selected configuration."""
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting scheduled run of qbitunregistered")
        result = subprocess.run(
            [sys.executable, "-m", "qbitunregistered", "--config", str(config_path), "--yes"],
            timeout=3600,
            check=True,
            capture_output=True,
            text=True,
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


def main(
    argv: list[str] | None = None,
    *,
    default_config_path: str | Path | None = None,
) -> int:
    """Run the scheduler with an optional caller-specific default config path."""
    if default_config_path is None:
        default_config_path = Path.cwd() / "config.json"

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Run qbitunregistered at the times defined in its configuration.")
    parser.add_argument(
        "--config",
        type=str,
        default=str(default_config_path),
        help=f"Path to the config.json file (default: {default_config_path})",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()

    # Load configuration from config.json
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        print(f"Error: The configuration file {config_path} was not found.")
        return 1
    except json.JSONDecodeError:
        print(f"Error: The configuration file {config_path} contains invalid JSON.")
        return 1

    # Schedule the script to run at the specified times
    scheduled_times = config.get("scheduled_times", [])
    if not scheduled_times:
        print("Warning: No scheduled_times found in config.json. Scheduler will not run any tasks.")
        return 0

    for scheduled_time in scheduled_times:
        try:
            schedule.every().day.at(scheduled_time).do(run_script, config_path)
        except schedule.ScheduleValueError as e:
            print(f"Error: Invalid time format '{scheduled_time}' in scheduled_times. {e}")
            return 1

    # Run the scheduler loop
    print(f"Scheduler started. Next runs scheduled at: {', '.join(scheduled_times)}")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nScheduler stopped by user")
        return 0
    except Exception as e:
        print(f"Scheduler crashed with unexpected error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
