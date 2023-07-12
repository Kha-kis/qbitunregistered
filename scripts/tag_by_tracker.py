import logging
from urllib.parse import urlsplit

def tag_by_tracker(client, config):
    torrents = client.torrents.info()
    
    for torrent in torrents:
        for tracker in torrent.trackers:
            # Extract the tracker hostname
            tracker_host = urlsplit(tracker.url).hostname

            # Check if the tracker host contains the specified term
            for term, tag in config.get('tracker_tags', {}).items():
                if term.lower() in tracker_host.lower():
                    # Add the tag to the torrent
                    client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
                    logging.info(f"Added tag '{tag}' to torrent with name '{torrent.name}'")
                    break
                elif term.lower() in tracker_host.lower().split("."):
                    # Add the tag to the torrent
                    client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
                    logging.info(f"Added tag '{tag}' to torrent with name '{torrent.name}'")
                    break

    logging.info("Tagging by tracker completed.")