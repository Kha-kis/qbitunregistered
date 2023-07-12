#!/usr/bin/python3
import json
import argparse
import os
import logging
from qbittorrentapi import Client
from scripts.orphaned import check_files_on_disk
from scripts.unregistered_checks import unregistered_checks, delete_torrents_and_files

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--config', type=str, default='config.json', help='Path to the config.json file.')
parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk.')
parser.add_argument('--unregistered', action='store_true', help='If set, perform unregistered checks.')
parser.add_argument('--dry-run', action='store_true', help='If set, the script will only print actions without executing them.')

# Parse command-line arguments
args = parser.parse_args()

# Resolve the absolute path to the config.json file
config_file_path = os.path.abspath(args.config)

# Load configuration from config.json
with open(config_file_path, 'r') as config_file:
    config = json.load(config_file)

# Connect to qBittorrent client
client = Client(host=config['host'], username=config['username'], password=config['password'])

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
    # Call the check_files_on_disk function
    orphaned_files = check_files_on_disk(client)

    # Log the total number of orphaned files
    logging.info("Total orphaned files: %d", len(orphaned_files))

# Run unregistered checks if --unregistered argument is passed
if args.unregistered:
    # Call the unregistered_checks function
    file_paths, unregistered_counts = unregistered_checks(client, config.get('unregistered'), config, use_delete_tags=config.get('use_delete_tags', False), dry_run=args.dry_run)

    # Log the total counts
    total_unregistered_count = sum(unregistered_counts.values())
    logging.info("Total unregistered count: %d", total_unregistered_count)

# Log script end
logging.info("qbitunregistered script completed.")
