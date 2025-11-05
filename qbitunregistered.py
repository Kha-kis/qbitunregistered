import json
import argparse
import os
import sys
import logging
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

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--config', type=str, default='config.json', help='Path to the config.json file.')
parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk and delete them unless --dry-run is specified.')
parser.add_argument('--unregistered', action='store_true', help='If set, perform unregistered checks.')
parser.add_argument('--dry-run', action='store_true', help='If set, the script will only print actions without executing them.')
parser.add_argument('--host', type=str, help='The host and port where qBittorrent is running.')
parser.add_argument('--username', type=str, help='The username for logging into qBittorrent Web UI.')
parser.add_argument('--password', type=str, help='The password for logging into qBittorrent Web UI.')
parser.add_argument('--tag-by-tracker', action='store_true', help='If set, perform tagging based on the associated tracker.')
parser.add_argument('--seeding-management', action='store_true', help='If set, apply seed time and seed ratio limits based on tracker tags.')
parser.add_argument('--auto-tmm', action='store_true', help='If set, enable Automatic Torrent Management (auto TMM).')
parser.add_argument('--pause-torrents', action='store_true', help='If set, pause all torrents.')
parser.add_argument('--resume-torrents', action='store_true', help='If set, resume all torrents.')
parser.add_argument('--auto-remove', action='store_true', help='If set, automatically remove completed torrents.')
parser.add_argument('--create-hard-links', action='store_true', help='If set, create hard links for completed torrents in target directory.')
parser.add_argument('--target-dir', default=None, help='Specify the target directory for organizing completed torrents. This is required if --create-hard-links is used and not specified in the config.json file.')
parser.add_argument('--tag-by-age', action='store_true', help='If set, perform tagging based on torrent age in months.')
parser.add_argument('--tag-by-cross-seed', action='store_true', help='If set, tag torrents based on cross-seeding status.')
parser.add_argument("--exclude-files", nargs='+', default=[], help="List of file patterns to exclude.")
parser.add_argument("--exclude-dirs", nargs='+', default=[], help="List of directories to exclude.")

# Parse command-line arguments
pre_args, unknown = parser.parse_known_args()

# Load configuration from config.json
config_file_path = os.path.abspath(pre_args.config)
try:
    with open(config_file_path, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    print(f"ERROR: The configuration file {config_file_path} was not found.")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: The configuration file {config_file_path} contains invalid JSON: {e}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Failed to read configuration file: {e}")
    sys.exit(1)

# Validate configuration
try:
    validate_config(config)
except ConfigValidationError as e:
    print(f"ERROR: {e}")
    sys.exit(1)

# Ensure target_dir is provided if required
if pre_args.create_hard_links and not pre_args.target_dir and not config.get('target_dir'):
    logging.error("Error: --target-dir is required when --create-hard-links is specified and not present in config.json.")
    sys.exit(1)

# Re-parse arguments now that configuration has been loaded
args = parser.parse_args()

# Configure logging BEFORE any operations
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Override configuration with command-line arguments if provided
config['host'] = args.host or config.get('host')
config['username'] = args.username or config.get('username')
config['password'] = args.password or config.get('password')
target_dir = args.target_dir or config.get('target_dir', None)
dry_run = args.dry_run if args.dry_run is not None else config.get('dry_run', False)
exclude_files = args.exclude_files if args.exclude_files else config.get('exclude_files', [])
exclude_dirs = args.exclude_dirs if args.exclude_dirs else config.get('exclude_dirs', [])

# Validate exclude patterns
validate_exclude_patterns(exclude_files, exclude_dirs)

# Connect to qBittorrent client
try:
    client = Client(host=config['host'], username=config['username'], password=config['password'])
except exceptions.APIConnectionError as e:
    logging.error(f"Failed to connect to qBittorrent: {e}")
    sys.exit(1)

# Define torrents
try:
    torrents = client.torrents.info()
except Exception as e:
    logging.error(f"Failed to retrieve torrent list from qBittorrent: {e}")
    sys.exit(1)

# Log script start
logging.info("Starting qbitunregistered script...")

# Run orphaned check if --orphaned argument is passed
if args.orphaned:
    try:
        orphaned_files = check_files_on_disk(client, torrents, exclude_file_patterns=exclude_files, exclude_dirs=exclude_dirs)
        logging.info("Total orphaned files: %d", len(orphaned_files))

        # Delete orphaned files unless dry-run is set
        delete_orphaned_files(orphaned_files, dry_run, client)
    except Exception as e:
        logging.error(f"Error during orphaned file check: {e}")
        if not dry_run:
            logging.error("Orphaned file check failed, continuing with other operations...")

# Run unregistered checks if --unregistered argument is passed
if args.unregistered:
    try:
        file_paths, unregistered_counts = unregistered_checks(client, torrents, config, use_delete_tags=config.get('use_delete_tags', False), delete_tags=config.get('delete_tags', []), delete_files=config.get('delete_files', {}), dry_run=dry_run)
        total_unregistered_count = sum(unregistered_counts.values())
        logging.info("Total unregistered count: %d", total_unregistered_count)
    except Exception as e:
        logging.error(f"Error during unregistered checks: {e}")

# Run the tag_by_tracker function if desired
if args.tag_by_tracker:
    try:
        tag_by_tracker(client, torrents, config)
    except Exception as e:
        logging.error(f"Error during tag by tracker: {e}")

# Run the tag_by_cross_seed function if --tag-by-cross-seed argument is passed
if args.tag_by_cross_seed:
    try:
        tag_cross_seeds(client, torrents)
    except Exception as e:
        logging.error(f"Error during cross-seed tagging: {e}")

# Run the tag_by_age function if --tag-by-age argument is passed
if args.tag_by_age:
    try:
        tag_by_age(client, torrents, config)
    except Exception as e:
        logging.error(f"Error during tag by age: {e}")

# Apply seed time and seed ratio limits if --seeding-management argument is passed
if args.seeding_management:
    try:
        apply_seed_limits(client, config)
    except Exception as e:
        logging.error(f"Error during seeding management: {e}")

# Run the apply_auto_tmm_per_torrent function if --auto-tmm argument is passed
if args.auto_tmm:
    try:
        apply_auto_tmm_per_torrent(client, torrents)
    except Exception as e:
        logging.error(f"Error during auto TMM: {e}")

# Pause all torrents if --pause-torrents argument is passed
if args.pause_torrents:
    try:
        pause_torrents(client, torrents)
    except Exception as e:
        logging.error(f"Error pausing torrents: {e}")

# Resume all torrents if --resume-torrents argument is passed
if args.resume_torrents:
    try:
        resume_torrents(client, torrents)
    except Exception as e:
        logging.error(f"Error resuming torrents: {e}")

# Check if --auto-remove argument is passed
if args.auto_remove:
    try:
        auto_remove(client, torrents, dry_run)
    except Exception as e:
        logging.error(f"Error during auto remove: {e}")

# Run the create_hard_links function if --create-hard-links argument is passed
if args.create_hard_links:
    try:
        create_hard_links(target_dir, torrents)
    except Exception as e:
        logging.error(f"Error creating hard links: {e}")

# Log script end
logging.info("qbitunregistered script completed.")