import json
import argparse
import os
import sys
import logging
from typing import Dict, List
from qbittorrentapi import Client, exceptions
from scripts.orphaned import check_files_on_disk, delete_orphaned_files
from scripts.unregistered_checks import unregistered_checks
from scripts.tag_by_tracker import tag_by_tracker
from scripts.seeding_management import apply_seed_limits
from scripts.torrent_management import pause_torrents, resume_torrents
from scripts.auto_remove import auto_remove
from scripts.auto_tmm import apply_auto_tmm_per_torrent
from scripts.create_hardlinks import create_hard_links
from scripts.tag_cross_seeding import tag_cross_seeds
from scripts.tag_by_age import tag_by_age
from utils.config_validator import validate_config, validate_exclude_patterns, ConfigValidationError
from utils.cache import log_cache_stats
from utils.notifications import NotificationManager

# Exit codes for different failure types
EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_CONNECTION_ERROR = 3

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument("--config", type=str, default="config.json", help="Path to the config.json file.")
parser.add_argument(
    "--orphaned",
    action="store_true",
    help="If set, check for orphaned files on disk and delete them unless --dry-run is specified.",
)
parser.add_argument(
    "--recycle-bin",
    type=str,
    default=None,
    help="Path to the recycle bin directory. If set, orphaned files will be moved here instead of being deleted.",
)
parser.add_argument("--apprise-url", type=str, help="Apprise URL for notifications.")
parser.add_argument("--notifiarr-key", type=str, help="Notifiarr API Key.")
parser.add_argument("--notifiarr-channel", type=str, help="Notifiarr Discord Channel ID.")
parser.add_argument("--unregistered", action="store_true", help="If set, perform unregistered checks.")
parser.add_argument(
    "--dry-run", action="store_true", help="If set, the script will only print actions without executing them."
)
parser.add_argument("--host", type=str, help="The host and port where qBittorrent is running.")
parser.add_argument("--username", type=str, help="The username for logging into qBittorrent Web UI.")
parser.add_argument("--password", type=str, help="The password for logging into qBittorrent Web UI.")
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
    "--log-level", type=str, choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Set logging level (default: INFO)"
)
parser.add_argument("--log-file", type=str, help="Write logs to specified file in addition to console")
parser.add_argument(
    "--yes", "-y", action="store_true", help="Skip confirmation prompt and proceed with operations (use with caution)"
)

# Parse command-line arguments
pre_args, unknown = parser.parse_known_args()

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

# Ensure target_dir is provided if required
if pre_args.create_hard_links and not pre_args.target_dir and not config.get("target_dir"):
    logging.error("Error: --target-dir is required when --create-hard-links is specified and not present in config.json.")
    sys.exit(EXIT_CONFIG_ERROR)

# Re-parse arguments now that configuration has been loaded
args = parser.parse_args()

# Override configuration with command-line arguments if provided
config["host"] = args.host or config.get("host")
config["username"] = args.username or config.get("username")
config["password"] = args.password or config.get("password")
target_dir = args.target_dir or config.get("target_dir", None)
dry_run = args.dry_run if args.dry_run is not None else config.get("dry_run", False)
exclude_files = args.exclude_files if args.exclude_files else config.get("exclude_files", [])
exclude_dirs = args.exclude_dirs if args.exclude_dirs else config.get("exclude_dirs", [])

# Notification configuration
config["apprise_url"] = args.apprise_url or config.get("apprise_url")
config["notifiarr_key"] = args.notifiarr_key or config.get("notifiarr_key")
config["notifiarr_channel"] = args.notifiarr_channel or config.get("notifiarr_channel")

# Determine log level (CLI arg > config.json > default INFO)
log_level_str = args.log_level or config.get("log_level", "INFO")
log_level = getattr(logging, log_level_str.upper())

# Determine log file (CLI arg > config.json > None)
log_file = args.log_file or config.get("log_file", None)

# Configure logging BEFORE any operations
log_handlers = []

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

# Validate configuration after CLI overrides are applied
try:
    validate_config(config)
except ConfigValidationError as e:
    logging.error(f"Configuration validation failed: {e}")
    sys.exit(EXIT_CONFIG_ERROR)

# Validate exclude patterns
validate_exclude_patterns(exclude_files, exclude_dirs)

# Connect to qBittorrent client
try:
    client = Client(host=config["host"], username=config["username"], password=config["password"])
except exceptions.APIConnectionError as e:
    logging.error(f"Failed to connect to qBittorrent: {e}")
    sys.exit(EXIT_CONNECTION_ERROR)

# Define torrents
try:
    torrents = client.torrents.info()
except (KeyboardInterrupt, SystemExit):
    raise
except Exception:
    logging.exception("Failed to retrieve torrent list from qBittorrent")
    sys.exit(EXIT_CONNECTION_ERROR)

# Log script start
logging.info("Starting qbitunregistered script...")

# Note: Cache is in-memory and automatically cleared between script runs.
# No manual clearing needed on startup.

# Track operation results for summary
operation_results: Dict[str, List[str]] = {"succeeded": [], "failed": []}

# ============================================================
# IMPACT PREVIEW (if not using --yes flag)
# ============================================================
# Collect operations to analyze
operations_to_run = []
if args.orphaned:
    operations_to_run.append("orphaned")
if args.unregistered:
    operations_to_run.append("unregistered")
if args.tag_by_tracker:
    operations_to_run.append("tag_by_tracker")
if args.tag_by_age:
    operations_to_run.append("tag_by_age")
if args.tag_by_cross_seed:
    operations_to_run.append("tag_cross_seeding")
if args.auto_remove:
    operations_to_run.append("auto_remove")
if args.pause_torrents:
    operations_to_run.append("pause")
if args.resume_torrents:
    operations_to_run.append("resume")

# Show impact preview if there are operations to run and not in --yes mode
if operations_to_run and not args.yes:
    try:
        from utils.impact_analyzer import analyze_impact

        logging.info("Analyzing potential impact of operations...")
        impact_summary = analyze_impact(client, torrents, config, operations_to_run)

        # Show preview
        print(impact_summary.format_summary(show_details=False))

        # If not in dry-run mode and there are actual changes, prompt for confirmation
        if not dry_run and not impact_summary.is_empty():
            try:
                response = input("\n🔍 Proceed with these changes? [y/N]: ").strip().lower()
                if response not in ["y", "yes"]:
                    logging.info("Operation aborted by user")
                    client.auth_log_out()
                    sys.exit(EXIT_SUCCESS)
                else:
                    logging.info("User confirmed, proceeding with operations...")
            except EOFError:
                # Handle non-interactive environments (like CI/CD without --yes flag)
                logging.warning("Non-interactive environment detected. Use --yes flag to skip confirmation.")
                logging.info("Operation aborted (no confirmation in non-interactive mode)")
                client.auth_log_out()
                sys.exit(EXIT_SUCCESS)
        elif dry_run:
            logging.info("Dry-run mode: no actual changes will be made")
        elif impact_summary.is_empty():
            logging.info("No operations to perform")
    except (KeyboardInterrupt, SystemExit):
        logging.info("Operation cancelled by user")
        try:
            client.auth_log_out()
        except Exception:
            pass
        sys.exit(EXIT_SUCCESS)
    except Exception as e:
        logging.warning(f"Could not generate impact preview: {e}")
        logging.warning("Continuing with operations...")

# ============================================================
# RUN OPERATIONS
# ============================================================

# Run orphaned check if --orphaned argument is passed
if args.orphaned:
    try:
        orphaned_files = check_files_on_disk(client, torrents, exclude_file_patterns=exclude_files, exclude_dirs=exclude_dirs)
        logging.info("Total orphaned files: %d", len(orphaned_files))

        # Delete orphaned files unless dry-run is set (pass torrents to avoid redundant API call)
        recycle_bin = args.recycle_bin or config.get("recycle_bin", None)
        delete_orphaned_files(orphaned_files, dry_run, client, torrents=torrents, recycle_bin=recycle_bin)
        operation_results["succeeded"].append("Orphaned file check")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error during orphaned file check")
        logging.error("Orphaned file check failed, continuing with other operations...")
        operation_results["failed"].append("Orphaned file check")

# Run unregistered checks if --unregistered argument is passed
if args.unregistered:
    try:
        file_paths, unregistered_counts = unregistered_checks(
            client,
            torrents,
            config,
            use_delete_tags=config.get("use_delete_tags", False),
            delete_tags=config.get("delete_tags", []),
            delete_files=config.get("delete_files", {}),
            dry_run=dry_run,
        )
        total_unregistered_count = sum(unregistered_counts.values())
        logging.info("Total unregistered count: %d", total_unregistered_count)
        operation_results["succeeded"].append("Unregistered checks")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error during unregistered checks")
        operation_results["failed"].append("Unregistered checks")

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
if args.create_hard_links:
    try:
        create_hard_links(target_dir, torrents, dry_run=dry_run)
        operation_results["succeeded"].append("Create hard links")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.exception("Error creating hard links")
        operation_results["failed"].append("Create hard links")

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
logging.info("qbitunregistered script completed.")

# Exit with non-zero code if any operations failed (for cron/CI detection)
if operation_results["failed"]:
    logging.error(f"Script completed with {len(operation_results['failed'])} failed operation(s)")
    sys.exit(EXIT_GENERAL_ERROR)
