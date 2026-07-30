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
    parser.add_argument(
        "--paired-control",
        type=Path,
        help="Clean control worktree for a contemporaneous ABBA+BAAB crossover.",
    )
    parser.add_argument(
        "--paired-candidate",
        type=Path,
        help="Clean candidate worktree for a contemporaneous ABBA+BAAB crossover.",
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
    paired_control: Path | None = arguments.paired_control
    paired_candidate: Path | None = arguments.paired_candidate
    paired_requested = paired_control is not None or paired_candidate is not None
    if paired_requested and (paired_control is None or paired_candidate is None):
        raise SystemExit("--paired-control and --paired-candidate must be supplied together")
    if paired_requested:
        assert paired_control is not None
        assert paired_candidate is not None
        try:
            paired_control = paired_control.expanduser().resolve()
            paired_candidate = paired_candidate.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit("paired gauntlet worktree paths could not be resolved") from error
        if arguments.compare is not None and arguments.compare.expanduser().resolve() != DEFAULT_QUALITY_BAR.resolve():
            raise SystemExit("paired gauntlet requires the invoking checkout's canonical quality bar")
        if arguments.output is not None and (
            not _outside_repository(arguments.output, paired_control)
            or not _outside_repository(arguments.output, paired_candidate)
        ):
            raise SystemExit("paired gauntlet output must be outside both evaluated repositories")

    from benchmarks.gauntlet.baseline import compare_result, load_quality_bar
    from benchmarks.gauntlet.runner import (
        run_gauntlet,
        serialize_result,
        write_serialized_result,
    )

    if paired_requested:
        from benchmarks.gauntlet.paired import run_paired_gauntlet

        assert paired_control is not None
        assert paired_candidate is not None
        paired_result = run_paired_gauntlet(
            paired_control,
            paired_candidate,
            orchestrator_root=repository_root,
            profile=arguments.profile,
            seed=arguments.seed,
            samples=arguments.samples,
        )
        serialized_result = serialize_result(paired_result)
        require_same_identity(
            identity_before_imports,
            capture_repository_identity(repository_root),
        )
        output_path = write_serialized_result(serialized_result, arguments.output)
        print(output_path)
        return 0 if paired_result["comparison"]["overall"] == "pass" else COMPARISON_FAILED_EXIT

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
