#!/usr/bin/python3
import json
import argparse
import os
import logging
from qbittorrentapi import Client
from scripts.orphaned import check_files_on_disk

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--config', type=str, default='config.json', help='Path to the config.json file.')
parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk.')

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
    check_files_on_disk(client)
    logging.info("Total orphaned files: %d", len(orphaned_files))

# Log script end
logging.info("qbitunregistered script completed.")
