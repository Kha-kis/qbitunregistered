"""Deprecated 2.x source-checkout wrapper; use the installed scheduler."""

from pathlib import Path

from qbitunregistered.scheduler import main

if __name__ == "__main__":
    wrapper_config_path = Path(__file__).absolute().with_name("config.json")
    raise SystemExit(main(default_config_path=wrapper_config_path))
