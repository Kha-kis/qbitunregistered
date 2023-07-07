#!/usr/bin/python3

import config
from urllib.parse import urlsplit
from qbittorrentapi import Client

# Connect to qbittorrent client
client = Client(host=config.host, username=config.username, password=config.password)

# List of unregistered tracker messages
unregistered = config.unregistered

# Dictionary to store file paths and their associated hashes
torrent_file_paths = {}

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
            print(torrent.name, ' ', tracker.msg, ' ', tracker_short.netloc)

    # Add tags based on unregistered_count
    if unregistered_count > 0:
        tags_to_add = ["unregistered:crossseeding"] if len(torrent_file_paths[torrent.save_path]) > 1 else ["unregistered"]
        if config.dry_run:
            # Dry run, only print what would be done
            print(f"[Dry Run] Would add tags {tags_to_add} to torrent with hash {torrent.hash}")
        else:
            # Not a dry run, execute the action
            client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])
        continue

    # Check trackers for other issues
    for tracker in torrent.trackers:
        if tracker.msg != 'This torrent is private' and tracker.status == 4 and tracker.msg.lower() not in [p.lower() for p in unregistered]:
            tracker_short = urlsplit(tracker.url)
            print(torrent.name, ' ', tracker.msg, ' ', tracker_short.netloc)
            
            # Add a tag to the torrent
            tags_to_add = [config.other_issues_tag]
            if config.dry_run:
                # Dry run, only print what would be done
                print(f"[Dry Run] Would add tags {tags_to_add} to torrent with hash {torrent.hash}")
            else:
                # Not a dry run, execute the action
                client.torrents_add_tags(tags=tags_to_add, torrent_hashes=[torrent.hash])
