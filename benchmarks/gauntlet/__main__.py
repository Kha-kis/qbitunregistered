"""Command-line entry point for the repository-local gauntlet."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from benchmarks.gauntlet.identity import (
    capture_repository_identity,
    require_same_identity,
)

PROFILE_NAMES = ("quick", "full")
DEFAULT_QUALITY_BAR = Path(__file__).with_name("quality-bar.toml")
COMPARISON_FAILED_EXIT = 2


def _positive_integer(value: str) -> int:
    parsed_value = int(value)
    if parsed_value < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed_value


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone evaluator argument parser without heavy imports."""
    parser = argparse.ArgumentParser(description="Run the deterministic qbitunregistered safety gauntlet.")
    parser.add_argument("--profile", choices=PROFILE_NAMES, default="quick")
    parser.add_argument("--seed", type=int, default=20_260_729)
    parser.add_argument(
        "--samples",
        type=_positive_integer,
        default=5,
        help="Timed samples; comparable runs require the locked value of five.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON output path outside the repository; defaults to a unique system temporary file.",
    )
    parser.add_argument(
        "--compare",
        nargs="?",
        const=DEFAULT_QUALITY_BAR,
        type=Path,
        help="Compare with a TOML quality bar; the repository quality bar is used when no path is supplied.",
    )
    return parser


def _outside_repository(path: Path, repository_root: Path) -> bool:
    try:
        return not path.expanduser().resolve().is_relative_to(repository_root)
    except (OSError, RuntimeError, ValueError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """Run one evaluator profile, optionally compare it, and write JSON."""
    repository_root = Path(__file__).resolve().parents[2]
    identity_before_imports = capture_repository_identity(repository_root)
    arguments = build_parser().parse_args(argv)
    if arguments.output is not None and not _outside_repository(
        arguments.output,
        repository_root,
    ):
        raise SystemExit("gauntlet output must be outside the repository")

    from benchmarks.gauntlet.baseline import compare_result, load_quality_bar
    from benchmarks.gauntlet.runner import (
        run_gauntlet,
        serialize_result,
        write_serialized_result,
    )

    result = run_gauntlet(
        arguments.profile,
        seed=arguments.seed,
        samples=arguments.samples,
        repository_root=repository_root,
        expected_identity=identity_before_imports,
    )
    exit_code = 0
    if arguments.compare is not None:
        comparison = compare_result(result, load_quality_bar(arguments.compare))
        result["comparison"] = comparison
        if comparison["overall"] != "pass":
            exit_code = COMPARISON_FAILED_EXIT

    serialized_result = serialize_result(result)
    require_same_identity(
        identity_before_imports,
        capture_repository_identity(repository_root),
    )
    output_path = write_serialized_result(serialized_result, arguments.output)
    print(output_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
