#!/usr/bin/python3
import argparse
import logging
from urllib.parse import urlsplit
from qbittorrentapi import Client
import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the desired logging level (e.g., INFO, DEBUG, WARNING)
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Set up command-line argument parsing
parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
parser.add_argument('--host', type=str, default=config.host, help='The host and port where qBittorrent is running.')
parser.add_argument('--username', type=str, default=config.username, help='The username for logging into qBittorrent Web UI.')
parser.add_argument('--password', type=str, default=config.password, help='The password for logging into qBittorrent Web UI.')
parser.add_argument('--dry-run', action='store_true', default=config.dry_run, help='If set, the script will only log actions without executing them.')
parser.add_argument('--enable-scheduler', action='store_true', default=config.enable_scheduler, help='If set, the script will run as per the scheduled times.')

# Parse command-line arguments
args = parser.parse_args()

# Connect to qBittorrent client
client = Client(host=args.host, username=args.username, password=args.password)

# Log script start
logging.info("Starting qbitunregistered script...")

# List of unregistered tracker messages
unregistered = config.unregistered

# Dictionary to store file paths and their associated hashes
torrent_file_paths = {}

# Get all torrents from qBittorrent
logging.info("Fetching torrent information from qBittorrent...")
torrents = client.torrents.info()
logging.info("Total torrents found: %d", len(torrents))

# Variables to store statistics
total_deleted_count = 0
total_deleted_from_disk_count = 0

# Iterate through all the torrents
for torrent in torrents:
    # Initialize tag_counts dictionary
    tag_counts = {"unregistered": 0, "unregistered:crossseeding": 0, config.other_issues_tag: 0}
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
        # If the message is unregistered, increment the counter and log info
        if is_unregistered and tracker.status == 4:
            unregistered_count += 1
            tracker_short = urlsplit(tracker.url)
            logging.info("%s %s %s", torrent.name, tracker.msg, tracker_short.netloc)

    # Add tags based on unregistered_count
    if unregistered_count > 0:
        tags_to_add = ["unregistered:crossseeding"] if len(torrent_file_paths[torrent.save_path]) > 1 else ["unregistered"]
        if args.dry_run:
            # Dry run, only log what would be done
            logging.info("[Dry Run] Would add tags %s to torrent with name %s", tags_to_add, torrent.name)
        else:
            # Not a dry run, execute the action
            client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])
            logging.info("Added tags %s to torrent with name %s", tags_to_add, torrent.name)

        # Update tag_counts after adding the tags
        tags = torrent.tags
        for tag in tags:
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
            if args.dry_run:
                # Dry run, only log what would be done
                logging.info("[Dry Run] Would add tags %s to torrent with name %s", tags_to_add, torrent.name)
            else:
                # Not a dry run, execute the action
                client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])
                logging.info("Added tags %s to torrent with name %s", tags_to_add, torrent.name)

# Reinitialize tag_counts dictionary
tag_counts = {"unregistered": 0, "unregistered:crossseeding": 0, config.other_issues_tag: 0}

# Update tag_counts based on the updated torrents list
for torrent in torrents:
    tags = torrent.tags
    for tag in tags:
        if tag in tag_counts:
            tag_counts[tag] += 1

# Log tag statistics at the end
logging.info("Tag statistics:")
for torrent in torrents:
    tags = torrent.tags
    for tag in tags:
        if tag in tag_counts:
            tag_counts[tag] += 1

# Log additional statistics
logging.info("Total torrents removed from qBittorrent: %d", total_deleted_count)
logging.info("Total torrents deleted from disk: %d", total_deleted_from_disk_count)

# Log script end
logging.info("qbitunregistered script completed.")
logging.info("qbitunregistered script completed.")
