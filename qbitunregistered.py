#!/usr/bin/python3
import config
import argparse
from qbittorrentapi import Client
import logging
from scripts.orphaned import check_files_on_disk
from scripts.unregistered_checks import unregistered_checks

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--host', type=str, help='The host and port where qBittorrent is running.')
parser.add_argument('--username', type=str, help='The username for logging into qBittorrent Web UI.')
parser.add_argument('--password', type=str, help='The password for logging into qBittorrent Web UI.')
parser.add_argument('--dry-run', action='store_true', help='If set, the script will only print actions without executing them.')
parser.add_argument('--other-issues-tag', type=str, help='The tag to be used for torrents that have issues other than being unregistered.')
parser.add_argument('--use-delete-tags', type=bool, help='Flag for using delete_tags in the script.')
parser.add_argument('--use-delete-files', type=bool, help='Flag for using delete_files in the script.')
parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk.')
parser.add_argument('--unregistered', action='store_true', help='If set, checks for unregistered torrents.')

# Parse command-line arguments
args = parser.parse_args()

# Override configuration with command-line arguments if provided
host = args.host if args.host else config.host
username = args.username if args.username else config.username
password = args.password if args.password else config.password
dry_run = args.dry_run if args.dry_run else config.dry_run
other_issues_tag = args.other_issues_tag if args.other_issues_tag else config.other_issues_tag
unregistered = config.unregistered
use_delete_tags = args.use_delete_tags if args.use_delete_tags is not None else config.use_delete_tags
use_delete_files = args.use_delete_files if args.use_delete_files is not None else config.use_delete_files

# Connect to qBittorrent client
client = Client(host=host, username=username, password=password)

# Log script start
logging.info("Starting qbitunregistered script...")

# Get all torrents from qBittorrent
logging.info("Fetching torrent information from qBittorrent...")
torrents = client.torrents.info()
logging.info("Total torrents found: %d", len(torrents))

# Call the unregistered_checks function if --unregistered argument is passed
if args.unregistered:
    torrent_file_paths, unregistered_counts_per_path = unregistered_checks(client, unregistered, config, dry_run, use_delete_tags, use_delete_files)

# Call the check_files_on_disk function if --orphaned argument is passed
if args.orphaned:
    check_files_on_disk(client)

# Log script end
logging.info("qbitunregistered script completed.")
