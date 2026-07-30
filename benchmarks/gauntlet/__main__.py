"""Command-line entry point for the repository-local gauntlet."""

from __future__ import annotations

import argparse
import tempfile
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


def _resolve_output(path: Path) -> tuple[Path, Path]:
    try:
        expanded_path = path.expanduser()
        publication_path = expanded_path.parent.resolve() / expanded_path.name
        return publication_path, publication_path.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("gauntlet output path could not be resolved") from error


def _output_is_outside_repositories(
    publication_path: Path,
    target_path: Path,
    repository_roots: Sequence[Path],
) -> bool:
    return all(
        not output_path.is_relative_to(repository_root)
        for output_path in (publication_path, target_path)
        for repository_root in repository_roots
    )


def _revalidate_output(
    path: Path,
    expected_publication_path: Path,
    expected_target_path: Path,
    repository_roots: Sequence[Path],
) -> Path:
    publication_path, target_path = _resolve_output(path)
    if (
        publication_path != expected_publication_path
        or target_path != expected_target_path
        or not _output_is_outside_repositories(
            publication_path,
            target_path,
            repository_roots,
        )
    ):
        raise SystemExit("gauntlet output destination changed or became unsafe during execution")
    return publication_path


def _resolve_default_output_directory() -> Path:
    try:
        return Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("gauntlet default output directory could not be resolved") from error


def _revalidate_default_output_directory(
    expected_directory: Path,
    repository_roots: Sequence[Path],
) -> None:
    output_directory = _resolve_default_output_directory()
    if output_directory != expected_directory or not _output_is_outside_repositories(
        output_directory,
        output_directory,
        repository_roots,
    ):
        raise SystemExit("gauntlet output destination changed or became unsafe during execution")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one evaluator profile, optionally compare it, and write JSON."""
    repository_root = Path(__file__).resolve().parents[2]
    identity_before_imports = capture_repository_identity(repository_root)
    arguments = build_parser().parse_args(argv)
    resolved_output: Path | None = None
    resolved_output_target: Path | None = None
    default_output_directory: Path | None = None
    if arguments.output is not None:
        resolved_output, resolved_output_target = _resolve_output(arguments.output)
        if not _output_is_outside_repositories(
            resolved_output,
            resolved_output_target,
            (repository_root,),
        ):
            raise SystemExit("gauntlet output must be outside the repository")
    else:
        default_output_directory = _resolve_default_output_directory()
        if not _output_is_outside_repositories(
            default_output_directory,
            default_output_directory,
            (repository_root,),
        ):
            raise SystemExit("gauntlet output must be outside the repository")
    paired_control: Path | None = arguments.paired_control
    paired_candidate: Path | None = arguments.paired_candidate
    paired_output = resolved_output
    paired_output_target = resolved_output_target
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
        if paired_output is not None:
            assert paired_output_target is not None
            if not _output_is_outside_repositories(
                paired_output,
                paired_output_target,
                (paired_control, paired_candidate),
            ):
                raise SystemExit("paired gauntlet output must be outside both evaluated repositories")
        else:
            assert default_output_directory is not None
            if not _output_is_outside_repositories(
                default_output_directory,
                default_output_directory,
                (paired_control, paired_candidate),
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
        if paired_output is not None:
            assert arguments.output is not None
            assert paired_output_target is not None
            paired_output = _revalidate_output(
                arguments.output,
                paired_output,
                paired_output_target,
                (repository_root, paired_control, paired_candidate),
            )
        else:
            assert default_output_directory is not None
            _revalidate_default_output_directory(
                default_output_directory,
                (repository_root, paired_control, paired_candidate),
            )
        if paired_output is None:
            assert default_output_directory is not None
            output_path = write_serialized_result(
                serialized_result,
                default_directory=default_output_directory,
            )
        else:
            output_path = write_serialized_result(serialized_result, paired_output)
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
    if resolved_output is not None:
        assert arguments.output is not None
        assert resolved_output_target is not None
        resolved_output = _revalidate_output(
            arguments.output,
            resolved_output,
            resolved_output_target,
            (repository_root,),
        )
    else:
        assert default_output_directory is not None
        _revalidate_default_output_directory(
            default_output_directory,
            (repository_root,),
        )
    if resolved_output is None:
        assert default_output_directory is not None
        output_path = write_serialized_result(
            serialized_result,
            default_directory=default_output_directory,
        )
    else:
        output_path = write_serialized_result(serialized_result, resolved_output)
    print(output_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
