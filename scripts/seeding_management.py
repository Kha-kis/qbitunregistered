from qbittorrentapi import Client
import logging

def apply_seed_time(client, config):
    torrents = client.torrents.info()

    for torrent in torrents:
        for tracker in torrent.trackers:
            tracker_tag_config = config.get('tracker_tags', {}).get(tracker.host)

            if tracker_tag_config is not None:
                seed_time_limit = tracker_tag_config.get('seed_time_limit')

                if seed_time_limit is not None:
                    client.torrents_edit(torrent.hash, seeding_time_limit=seed_time_limit)
                    logging.info(f"Updated seeding time limit for torrent with name '{torrent.name}' to {seed_time_limit} minutes.")

def apply_seed_ratio(client, config):
    torrents = client.torrents.info()

    for torrent in torrents:
        for tracker in torrent.trackers:
            tracker_tag_config = config.get('tracker_tags', {}).get(tracker.host)

            if tracker_tag_config is not None:
                seed_ratio_limit = tracker_tag_config.get('seed_ratio_limit')

                if seed_ratio_limit is not None:
                    client.torrents_edit(torrent.hash, ratio_limit=seed_ratio_limit)
                    logging.info(f"Updated seed ratio limit for torrent with name '{torrent.name}' to {seed_ratio_limit}.")