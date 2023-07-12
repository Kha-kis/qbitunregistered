#!/usr/bin/python3
import json
import argparse
import os
import logging
from qbittorrentapi import Client
from scripts.orphaned import check_files_on_disk
from scripts.unregistered_checks import (
    check_unregistered_message,
    process_torrent,
    update_torrent_file_paths,
    delete_torrents_and_files,
    unregistered_checks
)
# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--config', type=str, default='config.json', help='Path to the config.json file.')
parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk.')
parser.add_argument('--unregistered', action='store_true', help='If set, perform unregistered checks on torrents.')


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

# Get all torrents from qBittorrent
logging.info("Fetching torrent information from qBittorrent...")
torrents = client.torrents.info()
logging.info("Total torrents found: %d", len(torrents))

# Call the find_orphaned_files function if --orphaned argument is passed
if args.orphaned:
    orphaned_files = check_files_on_disk(client, torrents)
    # Log the total number of orphaned files
    logging.info("Total orphaned files: %d", len(orphaned_files))

# Call the unregistered_checks function if --unregistered argument is passed
if args.unregistered:
    # Call the unregistered_checks function
    file_paths, unregistered_counts = unregistered_checks(client, config['unregistered'], config, dry_run=config['dry_run'])
    # Log the file paths and unregistered counts
    logging.info("Torrent file paths:")
    for path, torrents in file_paths.items():
        logging.info("Path: %s", path)
        for torrent_hash in torrents:
            logging.info("  Torrent Hash: %s", torrent_hash)
    
    logging.info("Unregistered counts per path:")
    for path, count in unregistered_counts.items():
        logging.info("Path: %s, Count: %d", path, count)


# Log script end
logging.info("qbitunregistered script completed.")
