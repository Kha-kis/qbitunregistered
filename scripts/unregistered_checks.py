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
    tag_counts = {}

    for torrent in client.torrents.info():
        update_torrent_file_paths(torrent_file_paths, torrent)

        unregistered_count = process_torrent(torrent, unregistered)

        unregistered_counts_per_path[torrent.save_path] = unregistered_counts_per_path.get(torrent.save_path, 0) + unregistered_count

        # Add tags based on unregistered_count
        if unregistered_count > 0:
            # Check if all torrents in the same save path are unregistered
            if unregistered_counts_per_path[torrent.save_path] == len(torrent_file_paths[torrent.save_path]):
                tags_to_add = ["unregistered"]
            else:
                tags_to_add = ["unregistered:crossseeding"]
            if not dry_run:
                # Add tags to the torrent
                client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=tags_to_add)
                logging.info("Adding tags %s to torrent with name '%s'", tags_to_add, torrent.name)
                for tag in tags_to_add:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            else:
                # Dry run, only print what would be done
                logging.info("[Dry Run] Would add tags %s to torrent with name '%s'", tags_to_add, torrent.name)
                for tag in tags_to_add:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    delete_torrents_and_files(client, config, dry_run)

    for tag, count in tag_counts.items():
        logging.info("Tag: %s, Count: %d", tag, count)

    return torrent_file_paths, unregistered_counts_per_path