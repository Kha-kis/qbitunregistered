import logging
from utils.tracker_matcher import match_tracker_url

def find_tracker_config(client, torrent, config):
    """
    Find matching tracker configuration for a torrent.

    Args:
        client: qBittorrent client instance
        torrent: Torrent object
        config: Configuration dictionary with tracker_tags

    Returns:
        Matching tracker configuration dict or None if no match found.
        Returns None on API errors (logged but not raised).
    """
    try:
        trackers = client.torrents_trackers(torrent_hash=torrent.hash)
    except Exception:
        logging.exception(f"Failed to fetch trackers for torrent {torrent.hash}")
        return None

    tracker_tags_config = config.get('tracker_tags', {})

    for tracker in trackers:
        # Skip non-dict trackers
        if not isinstance(tracker, dict):
            continue

        tracker_url = tracker.get('url', '')
        if not tracker_url:
            continue

        # Use utility function for matching
        matched_config = match_tracker_url(tracker_url, tracker_tags_config)
        if matched_config is not None:
            return matched_config

    return None

def apply_seed_limits(client, config):
    """
    Apply both seeding time and ratio limits in a single pass.

    This function consolidates apply_seed_time and apply_seed_ratio to reduce
    API calls and iterations. It applies both limits together when configured.

    Args:
        client: qBittorrent client instance
        config: Configuration dictionary with tracker_tags
    """
    try:
        torrents = client.torrents.info()
    except Exception:
        logging.exception(f"Failed to fetch torrent list")
        return

    for torrent in torrents:
        tracker_tag_config = find_tracker_config(client, torrent, config)

        if tracker_tag_config is not None:
            seed_time_limit = tracker_tag_config.get('seed_time_limit')
            seed_ratio_limit = tracker_tag_config.get('seed_ratio_limit')

            # Skip if neither limit is configured
            if seed_time_limit is None and seed_ratio_limit is None:
                continue

            # Validate numeric values
            time_limit_int = None
            ratio_limit_float = None

            if seed_time_limit is not None:
                try:
                    time_limit_int = int(seed_time_limit)
                except (ValueError, TypeError) as e:
                    logging.warning(f"Invalid seed_time_limit value '{seed_time_limit}' for torrent '{torrent.name}' (hash: {torrent.hash}): {type(e).__name__}: {e}")

            if seed_ratio_limit is not None:
                try:
                    ratio_limit_float = float(seed_ratio_limit)
                except (ValueError, TypeError) as e:
                    logging.warning(f"Invalid seed_ratio_limit value '{seed_ratio_limit}' for torrent '{torrent.name}' (hash: {torrent.hash}): {type(e).__name__}: {e}")

            # Only call API if at least one valid limit was provided
            if time_limit_int is not None or ratio_limit_float is not None:
                try:
                    client.torrents_set_share_limits(
                        torrent_hashes=torrent.hash,
                        ratio_limit=ratio_limit_float if ratio_limit_float is not None else -2.0,
                        seeding_time_limit=time_limit_int if time_limit_int is not None else -2,
                        inactive_seeding_time_limit=-2
                    )
                    # Log what was updated
                    if time_limit_int is not None:
                        logging.info(f"Updated seeding time limit for torrent '{torrent.name}' to {seed_time_limit} minutes.")
                    if ratio_limit_float is not None:
                        logging.info(f"Updated seed ratio limit for torrent '{torrent.name}' to {seed_ratio_limit}.")
                except Exception:
                    logging.exception(f"Failed to set share limits for torrent '{torrent.name}' (hash: {torrent.hash})")


def apply_seed_time(client, config):
    """
    Apply seeding time limits based on tracker configuration.

    DEPRECATED: Use apply_seed_limits() instead for better performance.
    This function is kept for backward compatibility.
    """
    logging.warning("apply_seed_time() is deprecated. Use apply_seed_limits() to apply both limits in one pass.")
    apply_seed_limits(client, config)


def apply_seed_ratio(client, config):
    """
    Apply seeding ratio limits based on tracker configuration.

    DEPRECATED: Use apply_seed_limits() instead for better performance.
    This function is kept for backward compatibility.
    """
    logging.warning("apply_seed_ratio() is deprecated. Use apply_seed_limits() to apply both limits in one pass.")
    apply_seed_limits(client, config)