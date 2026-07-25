import json
import argparse
import os
import sys
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from qbittorrentapi import exceptions
from qbitunregistered.file_operations import FileIdentity
from qbitunregistered.operations.orphaned import (
    build_orphan_file_plan,
    check_files_on_disk,
    delete_orphaned_files,
)
from qbitunregistered.operations.unregistered_checks import (
    build_unregistered_deletion_plan,
    DeletionAction,
    UnregisteredDeletionPlan,
    unregistered_checks,
)
from qbitunregistered.operations.tag_by_tracker import tag_by_tracker
from qbitunregistered.operations.seeding_management import apply_seed_limits
from qbitunregistered.operations.torrent_management import pause_torrents, resume_torrents
from qbitunregistered.operations.auto_remove import auto_remove
from qbitunregistered.operations.auto_tmm import apply_auto_tmm_per_torrent
from qbitunregistered.operations.create_hardlinks import (
    PlannedHardLink,
    create_hard_links,
    plan_hard_links,
    verify_hard_link_preservation,
)
from qbitunregistered.operations.tag_cross_seeding import tag_cross_seeds
from qbitunregistered.operations.tag_by_age import tag_by_age
from qbitunregistered.config import (
    validate_config,
    validate_exclude_patterns,
    resolve_dry_run,
    ConfigValidationError,
)
from qbitunregistered.cache import clear_cache, log_cache_stats
from qbitunregistered.notifications import NotificationManager
from qbitunregistered.client import create_client
from qbitunregistered.types import QBittorrentClient, TorrentInfo
from qbitunregistered import __version__

# Exit codes for different failure types
EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_CONNECTION_ERROR = 3


def _nonblank_recycle_bin_path(value: str) -> str:
    """Return a nonblank recycle-bin CLI path."""
    if not value.strip():
        raise argparse.ArgumentTypeError("--recycle-bin requires a nonblank path")
    return value


# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
parser.add_argument("--config", type=str, default="config.json", help="Path to the config.json file.")
parser.add_argument(
    "--orphaned",
    action="store_true",
    help="If set, check for orphaned files on disk and delete them unless --dry-run is specified.",
)
parser.add_argument(
    "--recycle-bin",
    type=_nonblank_recycle_bin_path,
    default=None,
    help="Path to the recycle bin directory. If set, orphaned files will be moved here instead of being deleted.",
)
parser.add_argument("--apprise-url", type=str, help="Apprise URL for notifications.")
parser.add_argument("--notifiarr-key", type=str, help="Notifiarr API Key.")
parser.add_argument("--notifiarr-channel", type=str, help="Notifiarr Discord Channel ID.")
parser.add_argument("--unregistered", action="store_true", help="If set, perform unregistered checks.")
parser.add_argument(
    "--dry-run",
    action=argparse.BooleanOptionalAction,
    default=None,
    help="Enable or disable dry-run mode. Overrides the value from the configuration file.",
)
parser.add_argument("--host", type=str, help="The host and port where qBittorrent is running.")
parser.add_argument("--username", type=str, help="The username for logging into qBittorrent Web UI.")
parser.add_argument("--password", type=str, help="The password for logging into qBittorrent Web UI.")
parser.add_argument(
    "--api-key", type=str, help="API key for qBittorrent Web UI (qBittorrent v5.2.0+). Alternative to username/password."
)
parser.add_argument("--tag-by-tracker", action="store_true", help="If set, perform tagging based on the associated tracker.")
parser.add_argument(
    "--seeding-management", action="store_true", help="If set, apply seed time and seed ratio limits based on tracker tags."
)
parser.add_argument("--auto-tmm", action="store_true", help="If set, enable Automatic Torrent Management (auto TMM).")
parser.add_argument("--pause-torrents", action="store_true", help="If set, pause all torrents.")
parser.add_argument("--resume-torrents", action="store_true", help="If set, resume all torrents.")
parser.add_argument("--auto-remove", action="store_true", help="If set, automatically remove completed torrents.")
parser.add_argument(
    "--create-hard-links", action="store_true", help="If set, create hard links for completed torrents in target directory."
)
parser.add_argument(
    "--target-dir",
    default=None,
    help="Specify the target directory for organizing completed torrents. This is required if --create-hard-links is used and not specified in the config.json file.",
)
parser.add_argument("--tag-by-age", action="store_true", help="If set, perform tagging based on torrent age in months.")
parser.add_argument("--tag-by-cross-seed", action="store_true", help="If set, tag torrents based on cross-seeding status.")
parser.add_argument("--exclude-files", nargs="+", default=[], help="List of file patterns to exclude.")
parser.add_argument("--exclude-dirs", nargs="+", default=[], help="List of directories to exclude.")
parser.add_argument(
    "--orphan-scan-roots",
    nargs="+",
    default=None,
    help="Additional absolute directory paths to scan for orphaned files. Overrides configured explicit roots.",
)
parser.add_argument(
    "--log-level", type=str, choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Set logging level (default: INFO)"
)
parser.add_argument("--log-file", type=str, help="Write logs to specified file in addition to console")
parser.add_argument(
    "--yes", "-y", action="store_true", help="Skip confirmation prompt and proceed with operations (use with caution)"
)


def _selected_operations(args: argparse.Namespace) -> list[str]:
    """Return impact operation names for every selected mutating flag."""
    operation_flags = (
        ("orphaned", "orphaned"),
        ("unregistered", "unregistered"),
        ("tag_by_tracker", "tag_by_tracker"),
        ("seeding_management", "seeding_management"),
        ("auto_tmm", "auto_tmm"),
        ("pause_torrents", "pause"),
        ("resume_torrents", "resume"),
        ("auto_remove", "auto_remove"),
        ("create_hard_links", "create_hard_links"),
        ("tag_by_age", "tag_by_age"),
        ("tag_by_cross_seed", "tag_cross_seeding"),
    )
    return [operation for attribute, operation in operation_flags if getattr(args, attribute)]


def _format_orphaned_operation_result(file_count: int, dry_run: bool, recycle_bin: str | None) -> str:
    """Return a truthful operation and notification summary for orphan cleanup."""
    if file_count == 0:
        return "Orphaned files check: 0 files found"

    noun = "file" if file_count == 1 else "files"
    if recycle_bin:
        action = "would be moved to recycle bin" if dry_run else "moved to recycle bin"
    else:
        action = "would be permanently deleted" if dry_run else "permanently deleted"
    return f"Orphaned files check: {file_count} {noun} {action}"


def _hard_links_must_precede_unregistered(
    *,
    create_hard_links_selected: bool,
    unregistered_selected: bool,
    config: dict[str, object],
    deletion_plan: UnregisteredDeletionPlan | None,
) -> bool:
    """Return whether hard links protect a selected unregistered file cleanup."""
    if not create_hard_links_selected or not unregistered_selected:
        return False

    if deletion_plan is not None:
        file_actions = {
            DeletionAction.RECYCLE_FILES,
            DeletionAction.PERMANENT_DELETE,
        }
        return any(deletion.action in file_actions for deletion in deletion_plan.deletions)

    if config.get("use_delete_tags") is not True or config.get("use_delete_files") is not True:
        return False
    delete_tags = config.get("delete_tags", [])
    delete_files = config.get("delete_files", {})
    if not isinstance(delete_tags, list) or not isinstance(delete_files, dict):
        return False
    return any(isinstance(tag, str) and delete_files.get(tag) is True for tag in delete_tags)


def _run_hard_link_operation(
    target_dir: str,
    torrents: Sequence[TorrentInfo],
    dry_run: bool,
    planned_links: Sequence[PlannedHardLink] | None,
    operation_results: dict[str, list[str]],
    *,
    required_files: Sequence[FileIdentity] = (),
) -> bool:
    """Run hard-link creation and record its operation result."""
    try:
        resolved_links = planned_links
        if required_files and resolved_links is None:
            resolved_links = plan_hard_links(target_dir, torrents)
        create_hard_links(
            target_dir,
            torrents,
            dry_run=dry_run,
            planned_links=resolved_links,
        )
        if required_files:
            assert resolved_links is not None
            verify_hard_link_preservation(
                target_dir,
                torrents,
                required_files,
                dry_run=dry_run,
                planned_links=resolved_links,
            )
        operation_results["succeeded"].append("Create hard links")
        return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error creating hard links")
        operation_results["failed"].append("Create hard links")
        return False


def _resolve_combined_unregistered_plan(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, object],
    *,
    create_hard_links_selected: bool,
    unregistered_selected: bool,
    confirmed_plan: UnregisteredDeletionPlan | None,
    operation_results: dict[str, list[str]],
) -> tuple[UnregisteredDeletionPlan | None, bool]:
    """Build the exact scheduled deletion plan needed for operation ordering."""
    destructive_unregistered_configured = _hard_links_must_precede_unregistered(
        create_hard_links_selected=create_hard_links_selected,
        unregistered_selected=unregistered_selected,
        config=config,
        deletion_plan=confirmed_plan,
    )
    if confirmed_plan is not None or not destructive_unregistered_configured:
        return confirmed_plan, False

    try:
        return (
            build_unregistered_deletion_plan(
                client,
                torrents,
                config,
                use_delete_tags=cast(bool, config.get("use_delete_tags", False)),
                delete_tags=cast(list[str], config.get("delete_tags", [])),
                delete_files=cast(dict[str, bool], config.get("delete_files", {})),
                recycle_bin=cast(str | None, config.get("recycle_bin")),
            ),
            False,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error planning unregistered checks")
        operation_results["failed"].append("Unregistered checks")
        return None, True


def _destructive_file_identities(
    deletion_plan: UnregisteredDeletionPlan | None,
    torrents: Sequence[TorrentInfo],
) -> tuple[FileIdentity, ...]:
    """Return destructive files owned by at least one completed torrent."""
    if deletion_plan is None:
        return ()
    file_actions = {
        DeletionAction.RECYCLE_FILES,
        DeletionAction.PERMANENT_DELETE,
    }
    destructive_identities = {
        identity.path: identity
        for deletion in deletion_plan.deletions
        if deletion.action in file_actions
        for identity in deletion.files
    }
    if deletion_plan.ownership_snapshot is None:
        return tuple(destructive_identities.values())

    completed_hashes = {torrent.hash for torrent in torrents if torrent.state_enum.is_complete}
    completed_source_paths = {
        file_path
        for ownership in deletion_plan.ownership_snapshot.torrents
        if ownership.torrent_hash in completed_hashes
        for file_path in ownership.file_paths
    }
    return tuple(identity for path, identity in destructive_identities.items() if path in completed_source_paths)


def _run_unregistered_operation(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, object],
    dry_run: bool,
    deletion_plan: UnregisteredDeletionPlan | None,
    *,
    planning_failed: bool,
    hard_links_required: bool,
    hard_links_succeeded: bool,
    operation_results: dict[str, list[str]],
) -> None:
    """Run unregistered checks unless their safety prerequisites failed."""
    if planning_failed:
        return
    if hard_links_required and not hard_links_succeeded:
        logging.error("Unregistered checks blocked because required hard-link creation failed")
        operation_results["failed"].append("Unregistered checks (blocked: hard-link creation failed)")
        return

    try:
        _file_paths, unregistered_counts = unregistered_checks(
            client,
            torrents,
            config,
            use_delete_tags=cast(bool, config.get("use_delete_tags", False)),
            delete_tags=cast(list[str], config.get("delete_tags", [])),
            delete_files=cast(dict[str, bool], config.get("delete_files", {})),
            dry_run=dry_run,
            recycle_bin=cast(str | None, config.get("recycle_bin")),
            deletion_plan=deletion_plan,
        )
        total_unregistered_count = sum(unregistered_counts.values())
        logging.info("Total unregistered count: %d", total_unregistered_count)
        operation_results["succeeded"].append("Unregistered checks")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error during unregistered checks")
        operation_results["failed"].append("Unregistered checks")


def main(argv: list[str] | None = None) -> int:
    """Run qbitunregistered and return a process exit code."""
    total_started_at = time.monotonic()
    clear_cache()

    # Parse command-line arguments
    pre_args, _unknown = parser.parse_known_args(argv)

    # Load configuration from config.json
    config_file_path = os.path.abspath(pre_args.config)
    try:
        with open(config_file_path, "r") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        print(f"ERROR: The configuration file {config_file_path} was not found.")
        sys.exit(EXIT_CONFIG_ERROR)
    except json.JSONDecodeError as e:
        print(f"ERROR: The configuration file {config_file_path} contains invalid JSON: {e}")
        sys.exit(EXIT_CONFIG_ERROR)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        print(f"ERROR: Failed to read configuration file: {e}")
        sys.exit(EXIT_CONFIG_ERROR)

    # Re-parse arguments now that configuration has been loaded
    args = parser.parse_args(argv)

    # Override configuration with command-line arguments if provided
    for field in ("host", "api_key", "username", "password", "recycle_bin"):
        cli_value = getattr(args, field)
        if cli_value is not None:
            config[field] = cli_value
    target_dir = args.target_dir if args.target_dir is not None else config.get("target_dir")
    if target_dir is not None:
        config["target_dir"] = target_dir
    exclude_files = args.exclude_files if args.exclude_files else config.get("exclude_files", [])
    exclude_dirs = args.exclude_dirs if args.exclude_dirs else config.get("exclude_dirs", [])
    orphan_scan_roots = args.orphan_scan_roots if args.orphan_scan_roots is not None else config.get("orphan_scan_roots", [])
    config["exclude_files"] = exclude_files
    config["exclude_dirs"] = exclude_dirs
    config["orphan_scan_roots"] = orphan_scan_roots

    # Notification configuration
    for field in ("apprise_url", "notifiarr_key", "notifiarr_channel"):
        cli_value = getattr(args, field)
        if cli_value is not None:
            config[field] = cli_value

    try:
        dry_run = resolve_dry_run(args.dry_run, config)
        config["dry_run"] = dry_run
        validate_config(config)
        validate_exclude_patterns(exclude_files, exclude_dirs)
    except ConfigValidationError as error:
        print(f"ERROR: Configuration validation failed: {error}")
        raise SystemExit(EXIT_CONFIG_ERROR) from error

    if args.create_hard_links and not target_dir:
        print("ERROR: --target-dir is required when --create-hard-links is selected.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    # Determine log level (CLI arg > config.json > default INFO)
    log_level_str = args.log_level or config.get("log_level", "INFO")
    log_level = getattr(logging, log_level_str.upper())

    # Determine log file (CLI arg > config.json > None)
    log_file = args.log_file or config.get("log_file", None)

    # Configure logging BEFORE any operations
    log_handlers: list[logging.Handler] = []

    # Console handler (always present)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log_handlers.append(console_handler)

    # File handler (optional, for scheduled runs)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, mode="a")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            log_handlers.append(file_handler)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            print(f"WARNING: Could not create log file {log_file}: {e}")

    # Apply logging configuration
    logging.basicConfig(level=log_level, handlers=log_handlers, force=True)  # Override any existing config

    # Connect to qBittorrent client
    try:
        client = cast(QBittorrentClient, create_client(config))
    except exceptions.APIConnectionError as e:
        logging.error(f"Failed to connect to qBittorrent: {e}")
        sys.exit(EXIT_CONNECTION_ERROR)

    # Define torrents
    try:
        torrents = cast(list[TorrentInfo], list(client.torrents.info()))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Failed to retrieve torrent list from qBittorrent")
        sys.exit(EXIT_CONNECTION_ERROR)

    # Log script start
    logging.info("Starting qbitunregistered script...")

    # Cached API data is shared only by operations in this execution.

    # Track operation results for summary
    operation_results: dict[str, list[str]] = {"succeeded": [], "failed": []}

    # ============================================================
    # IMPACT PREVIEW (if not using --yes flag)
    # ============================================================
    operations_to_run = _selected_operations(args)
    impact_summary = None

    if not operations_to_run:
        logging.warning("No operations selected. Choose an operation flag such as --unregistered or --orphaned.")

    # Show impact preview if there are operations to run and not in --yes mode
    if operations_to_run and not args.yes:
        try:
            from qbitunregistered.impact import analyze_impact

            logging.info("Analyzing potential impact of operations...")
            impact_started_at = time.monotonic()
            impact_summary = analyze_impact(client, torrents, config, operations_to_run)
            logging.info("Impact analysis completed in %.2f seconds.", time.monotonic() - impact_started_at)

            # Show preview
            print(impact_summary.format_summary(show_details=True))

            # Every selected non-dry-run operation requires explicit confirmation,
            # including operations whose current target set is empty.
            if not dry_run:
                try:
                    response = input("\n🔍 Proceed with these changes? [y/N]: ").strip().lower()
                    if response not in ["y", "yes"]:
                        logging.info("Operation aborted by user")
                        client.auth_log_out()
                        return EXIT_SUCCESS
                    logging.info("User confirmed, proceeding with operations...")
                except EOFError:
                    logging.warning("Non-interactive environment detected. Use --yes flag to skip confirmation.")
                    logging.info("Operation aborted (no confirmation in non-interactive mode)")
                    client.auth_log_out()
                    return EXIT_SUCCESS
            elif dry_run:
                logging.info("Dry-run mode: no actual changes will be made")
        except (KeyboardInterrupt, SystemExit):
            try:
                client.auth_log_out()
            except Exception:
                pass
            raise
        except Exception:
            logging.exception("Impact analysis failed; aborting before any operation")
            try:
                client.auth_log_out()
            except Exception:
                pass
            return EXIT_GENERAL_ERROR

    # ============================================================
    # RUN OPERATIONS
    # ============================================================
    operations_started_at = time.monotonic()
    confirmed_unregistered_plan = impact_summary.unregistered_deletion_plan if impact_summary is not None else None
    confirmed_unregistered_plan, unregistered_planning_failed = _resolve_combined_unregistered_plan(
        client,
        torrents,
        config,
        create_hard_links_selected=args.create_hard_links,
        unregistered_selected=args.unregistered,
        confirmed_plan=confirmed_unregistered_plan,
        operation_results=operation_results,
    )

    hard_links_before_unregistered = not unregistered_planning_failed and _hard_links_must_precede_unregistered(
        create_hard_links_selected=args.create_hard_links,
        unregistered_selected=args.unregistered,
        config=config,
        deletion_plan=confirmed_unregistered_plan,
    )
    hard_links_attempted = False
    hard_links_succeeded = False
    destructive_file_identities = _destructive_file_identities(confirmed_unregistered_plan, torrents)

    # Run orphaned check if --orphaned argument is passed
    if args.orphaned:
        try:
            # Avoid treating recycle bin contents as orphaned on subsequent runs
            recycle_bin = config.get("recycle_bin")
            exclude_dirs_for_scan = list(exclude_dirs)
            if recycle_bin:
                # Convert to absolute path to ensure proper exclusion
                exclude_dirs_for_scan.append(str(Path(recycle_bin).resolve()))

            if impact_summary is not None and impact_summary.orphan_file_plan is not None:
                orphan_plan = impact_summary.orphan_file_plan
            else:
                orphaned_files = check_files_on_disk(
                    client,
                    torrents,
                    exclude_file_patterns=exclude_files,
                    exclude_dirs=exclude_dirs_for_scan,
                    orphan_scan_roots=orphan_scan_roots,
                )
                orphan_plan = build_orphan_file_plan(orphaned_files)
            orphaned_files = [str(path) for path in orphan_plan.paths]
            logging.info(f"Found {len(orphaned_files)} orphaned files")

            if orphaned_files:
                logging.info("Orphaned files:")
                for file in orphaned_files:
                    logging.info(f"  - {file}")
            else:
                logging.info("No orphaned files found")

            # Delete/move orphaned files unless dry-run is set (pass torrents to avoid redundant API call)
            delete_orphaned_files(
                orphaned_files,
                dry_run,
                client,
                torrents=torrents,
                recycle_bin=recycle_bin,
                plan=orphan_plan,
                orphan_scan_roots=orphan_scan_roots,
            )
            operation_results["succeeded"].append(_format_orphaned_operation_result(len(orphaned_files), dry_run, recycle_bin))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logging.exception("Error checking orphaned files")
            operation_results["failed"].append(f"Orphaned files check: {e}")

    # Preserve completed data before any selected unregistered file cleanup.
    # This intentionally follows orphan scanning so newly created links cannot
    # be classified as orphaned during the same execution.
    if hard_links_before_unregistered:
        hard_links_attempted = True
        hard_links_succeeded = _run_hard_link_operation(
            cast(str, target_dir),
            torrents,
            dry_run,
            impact_summary.hard_link_plan if impact_summary is not None else None,
            operation_results,
            required_files=destructive_file_identities,
        )

    # Run unregistered checks if --unregistered argument is passed
    if args.unregistered:
        _run_unregistered_operation(
            client,
            torrents,
            config,
            dry_run,
            confirmed_unregistered_plan,
            planning_failed=unregistered_planning_failed,
            hard_links_required=hard_links_before_unregistered,
            hard_links_succeeded=hard_links_succeeded,
            operation_results=operation_results,
        )

    # Run the tag_by_tracker function if desired
    if args.tag_by_tracker:
        try:
            tag_by_tracker(client, torrents, config, dry_run=dry_run)
            operation_results["succeeded"].append("Tag by tracker")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during tag by tracker")
            operation_results["failed"].append("Tag by tracker")

    # Run the tag_by_cross_seed function if --tag-by-cross-seed argument is passed
    if args.tag_by_cross_seed:
        try:
            tag_cross_seeds(client, torrents, dry_run=dry_run)
            operation_results["succeeded"].append("Tag cross-seeds")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during cross-seed tagging")
            operation_results["failed"].append("Tag cross-seeds")

    # Run the tag_by_age function if --tag-by-age argument is passed
    if args.tag_by_age:
        try:
            tag_by_age(client, torrents, config, dry_run=dry_run)
            operation_results["succeeded"].append("Tag by age")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during tag by age")
            operation_results["failed"].append("Tag by age")

    # Apply seed time and seed ratio limits if --seeding-management argument is passed
    if args.seeding_management:
        try:
            apply_seed_limits(client, config, torrents=torrents, dry_run=dry_run)
            operation_results["succeeded"].append("Seeding management")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during seeding management")
            operation_results["failed"].append("Seeding management")

    # Run the apply_auto_tmm_per_torrent function if --auto-tmm argument is passed
    if args.auto_tmm:
        try:
            apply_auto_tmm_per_torrent(client, torrents, dry_run=dry_run)
            operation_results["succeeded"].append("Auto TMM")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during auto TMM")
            operation_results["failed"].append("Auto TMM")

    # Pause all torrents if --pause-torrents argument is passed
    if args.pause_torrents:
        try:
            pause_torrents(client, torrents, dry_run=dry_run)
            operation_results["succeeded"].append("Pause torrents")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error pausing torrents")
            operation_results["failed"].append("Pause torrents")

    # Resume all torrents if --resume-torrents argument is passed
    if args.resume_torrents:
        try:
            resume_torrents(client, torrents, dry_run=dry_run)
            operation_results["succeeded"].append("Resume torrents")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error resuming torrents")
            operation_results["failed"].append("Resume torrents")

    # Check if --auto-remove argument is passed
    if args.auto_remove:
        try:
            auto_remove(client, torrents, dry_run)
            operation_results["succeeded"].append("Auto remove")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Error during auto remove")
            operation_results["failed"].append("Auto remove")

    # Run the create_hard_links function if --create-hard-links argument is passed
    if args.create_hard_links and not hard_links_attempted:
        _run_hard_link_operation(
            cast(str, target_dir),
            torrents,
            dry_run,
            impact_summary.hard_link_plan if impact_summary is not None else None,
            operation_results,
        )

    logging.info("Selected operation execution completed in %.2f seconds.", time.monotonic() - operations_started_at)

    # Log cache statistics
    log_cache_stats()

    # Print operation summary
    logging.info("=" * 60)
    logging.info("OPERATION SUMMARY")
    logging.info("=" * 60)

    if operation_results["succeeded"]:
        logging.info("✓ Succeeded (%d):", len(operation_results["succeeded"]))
        for op in operation_results["succeeded"]:
            logging.info("  - %s", op)
    else:
        logging.info("✓ Succeeded: None")

    if operation_results["failed"]:
        logging.warning("✗ Failed (%d):", len(operation_results["failed"]))
        for op in operation_results["failed"]:
            logging.warning("  - %s", op)
    else:
        logging.info("✗ Failed: None")

    logging.info("=" * 60)

    # Send notifications
    notification_manager = NotificationManager(config)
    notification_manager.send_summary(operation_results)

    # Clean up client connection
    try:
        client.auth_log_out()
        logging.debug("Logged out from qBittorrent")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.debug("Failed to logout from qBittorrent (non-critical)")

    # Log script end
    logging.info("qbitunregistered script completed in %.2f seconds total.", time.monotonic() - total_started_at)

    # Exit with non-zero code if any operations failed (for cron/CI detection)
    if operation_results["failed"]:
        logging.error(f"Script completed with {len(operation_results['failed'])} failed operation(s)")
        return EXIT_GENERAL_ERROR

    return EXIT_SUCCESS
