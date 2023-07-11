# File: scripts/unregistered_checks.py
import logging
from urllib.parse import urlsplit

def check_unregistered_message(tracker, unregistered):
    #Check if tracker message matches any pattern in the unregistered list.
    for pattern in unregistered:
        lower_pattern = pattern.lower()
        lower_msg = tracker.msg.lower()

        if lower_pattern.startswith("starts_with:") and lower_msg.startswith(lower_pattern.split("starts_with:")[1]):
            return True
        elif lower_msg == lower_pattern:
            return True
    return False

def process_torrent(torrent, unregistered):
    #Process a torrent to check for unregistered messages and return count.
    unregistered_count = 0

    for tracker in torrent.trackers:
        if check_unregistered_message(tracker, unregistered) and tracker.status == 4:
            unregistered_count += 1
            tracker_short = urlsplit(tracker.url)
            logging.info("%s %s %s", torrent.name, tracker.msg, tracker_short.netloc)

    return unregistered_count

def update_torrent_file_paths(torrent_file_paths, torrent):
    #Update the file paths of torrents.
    if torrent.save_path not in torrent_file_paths:
        torrent_file_paths[torrent.save_path] = [torrent.hash]
    else:
        torrent_file_paths[torrent.save_path].append(torrent.hash)

def delete_torrents_and_files(client, config, dry_run):
    #Delete torrents and their files based on configuration and tags.
    if config.use_delete_tags:
        for torrent in client.torrents.info():
            for tag in config.delete_tags:
                if tag in torrent.tags:
                    if config.use_delete_files and config.delete_files.get(tag, False):
                        if not dry_run:
                            # Delete files
                            client.torrents.delete(torrent.hash, delete_files=True)
                            logging.info("Deleted torrent '%s' with hash %s and its files.", torrent.name, torrent.hash)
                        else:
                            # Dry run, only print what would be done
                            logging.info("[Dry Run] Would delete torrent '%s' with hash %s and its files.", torrent.name, torrent.hash)
                    else:
                        if not dry_run:
                            # Delete torrent without files
                            client.torrents.delete(torrent.hash, delete_files=False)
                            logging.info("Deleted torrent '%s' with hash %s.", torrent.name, torrent.hash)
                        else:
                            # Dry run, only print what would be done
                            logging.info("[Dry Run] Would delete torrent '%s' with hash %s.", torrent.name, torrent.hash)

def unregistered_checks(client, unregistered, config, dry_run):
    #Check for unregistered torrents and process them.
    torrent_file_paths = {}
    unregistered_counts_per_path = {}

    for torrent in client.torrents.info():
        update_torrent_file_paths(torrent_file_paths, torrent)

        unregistered_count = process_torrent(torrent, unregistered)

        if torrent.save_path not in unregistered_counts_per_path:
            unregistered_counts_per_path[torrent.save_path] = unregistered_count
        else:
            unregistered_counts_per_path[torrent.save_path] += unregistered_count

    delete_torrents_and_files(client, config, dry_run)

    return torrent_file_paths, unregistered_counts_per_path
