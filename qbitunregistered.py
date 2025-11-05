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
    logging.error(f"The configuration file {config_file_path} was not found.")
    sys.exit(1)
except json.JSONDecodeError:
    logging.error(f"The configuration file {config_file_path} contains invalid JSON.")
    sys.exit(1)

# Ensure target_dir is provided if required
if pre_args.create_hard_links and not pre_args.target_dir and not config.get('target_dir'):
    logging.error("Error: --target-dir is required when --create-hard-links is specified and not present in config.json.")
    sys.exit(1)

# Re-parse arguments now that configuration has been loaded
args = parser.parse_args()

# Override configuration with command-line arguments if provided
config['host'] = args.host or config.get('host')
config['username'] = args.username or config.get('username')
config['password'] = args.password or config.get('password')
target_dir = args.target_dir or config.get('target_dir', None)
dry_run = args.dry_run if args.dry_run is not None else config.get('dry_run', False)
exclude_files = args.exclude_files if args.exclude_files else config.get('exclude_files', [])
exclude_dirs = args.exclude_dirs if args.exclude_dirs else config.get('exclude_dirs', [])

# Connect to qBittorrent client
try:
    client = Client(host=config['host'], username=config['username'], password=config['password'])
except exceptions.APIConnectionError as e:
    logging.error(f"Failed to connect to qBittorrent: {e}")
    sys.exit(1)

# Define torrents
torrents = client.torrents.info()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Log script start
logging.info("Starting qbitunregistered script...")

# Run orphaned check if --orphaned argument is passed
if args.orphaned:
    orphaned_files = check_files_on_disk(client, torrents, exclude_file_patterns=exclude_files, exclude_dirs=exclude_dirs)

    logging.info("Total orphaned files: %d", len(orphaned_files))

    # Delete orphaned files unless dry-run is set
    delete_orphaned_files(orphaned_files, dry_run, client)

# Run unregistered checks if --unregistered argument is passed
if args.unregistered:
    file_paths, unregistered_counts = unregistered_checks(client, torrents, config, use_delete_tags=config.get('use_delete_tags', False), delete_tags=config.get('delete_tags', []), delete_files=config.get('delete_files', {}), dry_run=dry_run)
    total_unregistered_count = sum(unregistered_counts.values())
    logging.info("Total unregistered count: %d", total_unregistered_count)

# Run the tag_by_tracker function if desired
if args.tag_by_tracker:
    tag_by_tracker(client, torrents, config)

# Run the tag_by_cross_seed function if --tag-by-cross-seed argument is passed
if args.tag_by_cross_seed:
    tag_cross_seeds(client, torrents)

# Run the tag_by_age function if --tag-by-age argument is passed
if args.tag_by_age:
    tag_by_age(client, torrents, config)

# Apply seed time and seed ratio limits if --seeding-management argument is passed
if args.seeding_management:
    apply_seed_limits(client, config)

# Run the apply_auto_tmm_per_torrent function if --auto-tmm argument is passed
if args.auto_tmm:
    apply_auto_tmm_per_torrent(client, torrents)

# Pause all torrents if --pause-torrents argument is passed
if args.pause_torrents:
    pause_torrents(client, torrents)

# Resume all torrents if --resume-torrents argument is passed
if args.resume_torrents:
    resume_torrents(client, torrents)

# Check if --auto-remove argument is passed
if args.auto_remove:
    auto_remove(client, torrents, dry_run)

# Run the create_hard_links function if --create-hard-links argument is passed
if args.create_hard_links:
    create_hard_links(target_dir, torrents)

# Log script end
logging.info("qbitunregistered script completed.")