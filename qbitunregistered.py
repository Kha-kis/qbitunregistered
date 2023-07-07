#!/usr/bin/python3
import config
import argparse
from urllib.parse import urlsplit
from qbittorrentapi import Client
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the desired logging level (e.g., INFO, DEBUG, WARNING)
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--host', type=str, help='The host and port where qBittorrent is running.')
parser.add_argument('--username', type=str, help='The username for logging into qBittorrent Web UI.')
parser.add_argument('--password', type=str, help='The password for logging into qBittorrent Web UI.')
parser.add_argument('--dry-run', action='store_true', help='If set, the script will only print actions without executing them.')
parser.add_argument('--other-issues-tag', type=str, help='The tag to be used for torrents that have issues other than being unregistered.')

# Parse command-line arguments
args = parser.parse_args()

# Override configuration with command-line arguments if provided
host = args.host if args.host else config.host
username = args.username if args.username else config.username
password = args.password if args.password else config.password
dry_run = args.dry_run if args.dry_run else config.dry_run
other_issues_tag = args.other_issues_tag if args.other_issues_tag else config.other_issues_tag
unregistered = config.unregistered

# Connect to qBittorrent client
client = Client(host=config.host, username=config.username, password=config.password)

# Log script start
logging.info("Starting qbitunregistered script...")

# Log dry run mode
if dry_run:
    logging.info("Running in dry run mode. No actions will be executed.")
else:
    logging.info("Running in normal mode. Actions will be executed.")

# List of unregistered tracker messages
unregistered = config.unregistered

# Dictionary to store file paths and their associated hashes
torrent_file_paths = {}

# Get all torrents from qBittorrent
logging.info("Fetching torrent information from qBittorrent...")
torrents = client.torrents.info()
logging.info("Total torrents found: %d", len(torrents))

# Initialize tag counts
tag_counts = {
    "unregistered": 0,
    "unregistered:crossseeding": 0,
    config.other_issues_tag: 0
}

# Iterate through all the torrents
for torrent in client.torrents.info():

    # Store the hashes in the torrent_file_paths dictionary
    if torrent.save_path not in torrent_file_paths:
        torrent_file_paths[torrent.save_path] = [torrent.hash]
    else:
        torrent_file_paths[torrent.save_path].append(torrent.hash)

    # Initialize unregistered_count to 0
    unregistered_count = 0

    # Check trackers for unregistered messages
    for tracker in torrent.trackers:
        is_unregistered = False
        # Check if the message matches any pattern in the unregistered list
        for pattern in unregistered:
            lower_pattern = pattern.lower()
            lower_msg = tracker.msg.lower()
            if lower_pattern.startswith("starts_with:") and lower_msg.startswith(lower_pattern.split("starts_with:")[1]):
                is_unregistered = True
                break
            elif lower_msg == lower_pattern:
                is_unregistered = True
                break
        # If the message is unregistered, increment the counter and print info
        if is_unregistered and tracker.status == 4:
            unregistered_count += 1
            tracker_short = urlsplit(tracker.url)
            logging.info("%s %s %s", torrent.name, tracker.msg, tracker_short.netloc)

    # Add tags based on unregistered_count
    if unregistered_count > 0:
        tags_to_add = ["unregistered:crossseeding"] if len(torrent_file_paths[torrent.save_path]) > 1 else ["unregistered"]
        if config.dry_run:
            # Dry run, only print what would be done
            print(f"[Dry Run] Would add tags {tags_to_add} to torrent with hash {torrent.hash}")
        else:
            # Not a dry run, execute the action
            client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])

        # Update the tag counts
        for tag in tags_to_add:
            if tag in tag_counts:
                tag_counts[tag] += 1

        continue

    # Check trackers for other issues
    for tracker in torrent.trackers:
        if tracker.msg != 'This torrent is private' and tracker.status == 4 and tracker.msg.lower() not in [p.lower() for p in unregistered]:
            tracker_short = urlsplit(tracker.url)
            logging.info("%s %s %s", torrent.name, tracker.msg, tracker_short.netloc)

            # Add a tag to the torrent
            tags_to_add = [config.other_issues_tag]
            if config.dry_run:
                # Dry run, only print what would be done
                logging.info("[Dry Run] Would add tags %s to torrent with hash %s", tags_to_add, torrent.name)
            else:
                # Not a dry run, execute the action
                client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])

            # Update the tag counts
            for tag in tags_to_add:
                if tag in tag_counts:
                    tag_counts[tag] += 1

# Log the total number of torrents with each added tag
logging.info("Tag statistics:")

for tag, count in tag_counts.items():
    logging.info("Total torrents with tag '%s': %d", tag, count)

# Log script end
logging.info("qbitunregistered script completed.")

