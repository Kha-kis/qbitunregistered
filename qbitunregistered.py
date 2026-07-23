"""Deprecated 2.x source-checkout wrapper; use the installed CLI."""

from qbitunregistered.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
