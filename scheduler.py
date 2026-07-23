"""Compatibility wrapper for running the scheduler from a source checkout."""

from qbitunregistered.scheduler import main

if __name__ == "__main__":
    raise SystemExit(main())
