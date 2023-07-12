#!/usr/bin/python3
import logging
from urllib.parse import urlsplit

def check_unregistered_message(tracker, unregistered):
    lower_unregistered = {pattern.lower() for pattern in unregistered}
    lower_msg = tracker.msg.lower()

    for pattern in lower_unregistered:
        if pattern.startswith("starts_with:") and lower_msg.startswith(pattern.split("starts_with:")[1]):
            return True
        elif lower_msg == pattern:
            return True

    return False

def process_torrent(torrent, unregistered):
    unregistered_count = 0
    lower_unregistered = {pattern.lower() for pattern in unregistered}

    for tracker in torrent.trackers:
        if check_unregistered_message(tracker, lower_unregistered) and tracker.status == 4:
            unregistered_count += 1
            tracker_short = urlsplit(tracker.url)
            logging.info("%s %s %s", torrent.name, tracker.msg, tracker_short.netloc)

    return unregistered_count

def update_torrent_file_paths(torrent_file_paths, torrent):
    torrent_file_paths.setdefault(torrent.save_path, []).append(torrent.hash)

def delete_torrents_and_files(client, config, dry_run):
    if config['use_delete_tags']:
        for torrent in client.torrents.info():
            for tag in config['delete_tags']:
                if tag in torrent.tags:
                    if config['use_delete_files'] and config['delete_files'].get(tag, False):
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
    torrent_file_paths = {}
    unregistered_counts_per_path = {}

    for torrent in client.torrents.info():
        update_torrent_file_paths(torrent_file_paths, torrent)

        unregistered_count = process_torrent(torrent, unregistered)

        unregistered_counts_per_path[torrent.save_path] = unregistered_counts_per_path.get(torrent.save_path, 0) + unregistered_count

    delete_torrents_and_files(client, config, dry_run)

    total_unregistered_count = sum(unregistered_counts_per_path.values())
    logging.info("Total unregistered torrents: %d", total_unregistered_count)

    return torrent_file_paths, unregistered_counts_per_path
