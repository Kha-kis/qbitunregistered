import logging
from typing import List, Dict, Any
from collections import defaultdict
from scripts.seeding_management import find_tracker_config


def tag_by_tracker(client, torrents: List[Any], config: Dict[str, Any]) -> None:
    """
    Tag torrents based on their tracker and optionally apply seed limits.

    Uses batched API calls - groups torrents by tag and share limit configuration.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to tag
        config: Configuration dictionary with tracker_tags
    """
    # Group torrents by tag for batching
    torrents_by_tag = defaultdict(list)
    # Group torrents by share limit configuration for batching
    torrents_by_limits = defaultdict(list)

    for torrent in torrents:
        tracker_tag_config = find_tracker_config(client, torrent, config)

        if tracker_tag_config is not None:
            tag = tracker_tag_config.get('tag')
            seed_time_limit = tracker_tag_config.get('seed_time_limit')
            seed_ratio_limit = tracker_tag_config.get('seed_ratio_limit')

            # Validate tag value before applying
            if tag is None:
                logging.warning(f"No tag defined for tracker config, skipping torrent '{torrent.name}' (hash: {torrent.hash})")
                continue

            # Group by tag for batch tagging
            torrents_by_tag[tag].append(torrent.hash)

            # Validate and group by share limits
            if seed_time_limit is not None or seed_ratio_limit is not None:
                time_limit_int = None
                ratio_limit_float = None

                if seed_time_limit is not None:
                    try:
                        time_limit_int = int(seed_time_limit)
                    except (ValueError, TypeError) as e:
                        logging.warning(f"Invalid seed_time_limit value '{seed_time_limit}' for torrent '{torrent.name}': {e}")

                if seed_ratio_limit is not None:
                    try:
                        ratio_limit_float = float(seed_ratio_limit)
                    except (ValueError, TypeError) as e:
                        logging.warning(f"Invalid seed_ratio_limit value '{seed_ratio_limit}' for torrent '{torrent.name}': {e}")

                # Group by limits configuration (use tuple as key)
                if time_limit_int is not None or ratio_limit_float is not None:
                    limits_key = (time_limit_int, ratio_limit_float)
                    torrents_by_limits[limits_key].append(torrent.hash)

    # Apply tags in batches (one API call per unique tag)
    # Note: If batch operation fails, all torrents in that batch are affected.
    # This is by design for performance - manual intervention may be needed for failures.
    for tag, torrent_hashes in torrents_by_tag.items():
        try:
            client.torrents_add_tags(torrent_hashes=torrent_hashes, tags=[tag])
            logging.info(f"Added tag '{tag}' to {len(torrent_hashes)} torrents")
        except Exception:
            logging.exception(f"Failed to add tag '{tag}' to batch of {len(torrent_hashes)} torrents. "
                             f"Check qBittorrent connectivity and permissions. "
                             f"Affected torrent hashes: {torrent_hashes[:3]}{'...' if len(torrent_hashes) > 3 else ''}")

    # Apply share limits in batches (one API call per unique configuration)
    # Note: Batch operations are all-or-nothing for performance.
    for (time_limit, ratio_limit), torrent_hashes in torrents_by_limits.items():
        try:
            client.torrents_set_share_limits(
                torrent_hashes=torrent_hashes,
                ratio_limit=ratio_limit if ratio_limit is not None else -2.0,
                seeding_time_limit=time_limit if time_limit is not None else -2,
                inactive_seeding_time_limit=-2
            )
            logging.info(f"Updated share limits for {len(torrent_hashes)} torrents "
                         f"(time: {time_limit}, ratio: {ratio_limit})")
        except Exception:
            logging.exception(f"Failed to set share limits for batch of {len(torrent_hashes)} torrents "
                             f"(time: {time_limit}, ratio: {ratio_limit}). "
                             f"Check qBittorrent API compatibility and values. "
                             f"Affected torrent hashes: {torrent_hashes[:3]}{'...' if len(torrent_hashes) > 3 else ''}")

    logging.info("Tagging by tracker completed.")
