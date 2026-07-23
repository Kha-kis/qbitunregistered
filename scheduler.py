"""Compatibility wrapper for running the scheduler from a source checkout."""

from pathlib import Path

from qbitunregistered.scheduler import main

if __name__ == "__main__":
    wrapper_config_path = Path(__file__).absolute().with_name("config.json")
    raise SystemExit(main(default_config_path=wrapper_config_path))
