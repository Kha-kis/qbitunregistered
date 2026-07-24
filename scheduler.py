"""Deprecated 2.x source-checkout wrapper; use the installed scheduler."""

from pathlib import Path

from qbitunregistered.scheduler import main

if __name__ == "__main__":
    wrapper_path = Path(__file__).absolute()
    raise SystemExit(
        main(
            default_config_path=wrapper_path.with_name("config.json"),
            execution_cwd=wrapper_path.parent,
        )
    )
