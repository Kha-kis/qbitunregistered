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
    lower_unregistered = {pattern.lower() for pattern in unregistered}
    unregistered_count = sum(
        1
        for tracker in torrent.trackers
        if check_unregistered_message(tracker, lower_unregistered) and tracker.status == 4
    )
    return unregistered_count

def update_torrent_file_paths(torrent_file_paths, torrent):
    torrent_file_paths.setdefault(torrent.save_path, []).append(torrent.hash)

def delete_torrents_and_files(client, config, use_delete_tags, delete_tags, delete_files, dry_run):
    if use_delete_tags:
        for torrent in client.torrents.info():
            for tag in delete_tags:
                if tag in torrent.tags:
                    if delete_files.get(tag, False):
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=True)
                            logging.info(f"Deleted torrent '{torrent.name}' with hash {torrent.hash} and its files.")
                        else:
                            logging.info(f"[Dry Run] Would delete torrent '{torrent.name}' with hash {torrent.hash} and its files.")
                    else:
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=False)
                            logging.info(f"Deleted torrent '{torrent.name}' with hash {torrent.hash}.")
                        else:
                            logging.info(f"[Dry Run] Would delete torrent '{torrent.name}' with hash {torrent.hash}.")
                    break  # Exit the inner loop after deleting the torrent

def unregistered_checks(client, torrents, config, use_delete_tags, delete_tags, delete_files, dry_run):
    torrent_file_paths = {}
    unregistered_counts_per_path = {}
    tag_counts = {}
    default_tag = config['default_unregistered_tag']
    cross_seeding_tag = config['cross_seeding_tag']
    
    for torrent in torrents:
        update_torrent_file_paths(torrent_file_paths, torrent)

        # Pass the 'unregistered' argument to the process_torrent function
        unregistered_count = process_torrent(torrent, config.get('unregistered'))

        unregistered_counts_per_path[torrent.save_path] = unregistered_counts_per_path.get(torrent.save_path, 0) + unregistered_count

        # Add tags based on unregistered_count
        if unregistered_count > 0:
            is_all_unregistered = unregistered_counts_per_path[torrent.save_path] == len(torrent_file_paths[torrent.save_path])
            tags_to_add = [default_tag] if is_all_unregistered else [cross_seeding_tag]
            if not dry_run:
                client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=tags_to_add)
                logging.info(f"Adding tags {tags_to_add} to torrent with name '{torrent.name}'")
                for tag in tags_to_add:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            else:
                logging.info(f"[Dry Run] Would add tags {tags_to_add} to torrent with name '{torrent.name}'")
                for tag in tags_to_add:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    delete_torrents_and_files(client, config, use_delete_tags, delete_tags, delete_files, dry_run)

    for tag, count in tag_counts.items():
        logging.info("Tag: %s, Count: %d", tag, count)

    return torrent_file_paths, unregistered_counts_per_path
