import logging

def tag_by_tracker(client, torrents, config):
    for torrent in torrents:
        for tracker in torrent.trackers:
            tracker_tag_config = config.get('tracker_tags', {}).get(tracker.host)

            if tracker_tag_config is not None:
                tag = tracker_tag_config.get('tag')
                seed_time_limit = tracker_tag_config.get('seed_time_limit')
                seed_ratio_limit = tracker_tag_config.get('seed_ratio_limit')

                # Add the tag to the torrent
                client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
                logging.info(f"Added tag '{tag}' to torrent with name '{torrent.name}'")

                # Apply seed time limit if provided
                if seed_time_limit is not None:
                    client.torrents_edit(torrent.hash, seeding_time_limit=seed_time_limit)
                    logging.info(f"Updated seeding time limit for torrent with name '{torrent.name}' to {seed_time_limit} minutes.")

                # Apply seed ratio limit if provided
                if seed_ratio_limit is not None:
                    client.torrents_edit(torrent.hash, ratio_limit=seed_ratio_limit)
                    logging.info(f"Updated seed ratio limit for torrent with name '{torrent.name}' to {seed_ratio_limit}.")

    logging.info("Tagging by tracker completed.")
