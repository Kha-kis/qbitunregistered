import logging
from scripts.seeding_management import find_tracker_config

def tag_by_tracker(client, torrents, config):
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

            # Add the tag to the torrent with error handling
            try:
                client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
                logging.info(f"Added tag '{tag}' to torrent with name '{torrent.name}'")
            except Exception:
                logging.exception(f"Failed to add tag '{tag}' to torrent '{torrent.name}' (hash: {torrent.hash})")
                # Continue to try applying seed limits even if tagging fails

            # Apply seed limits if provided
            if seed_time_limit is not None or seed_ratio_limit is not None:
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
                            torrent_hashes=[torrent.hash],
                            ratio_limit=ratio_limit_float if ratio_limit_float is not None else -2.0,
                            seeding_time_limit=time_limit_int if time_limit_int is not None else -2,
                            inactive_seeding_time_limit=-2
                        )
                        if time_limit_int is not None:
                            logging.info(f"Updated seeding time limit for torrent with name '{torrent.name}' to {seed_time_limit} minutes.")
                        if ratio_limit_float is not None:
                            logging.info(f"Updated seed ratio limit for torrent with name '{torrent.name}' to {seed_ratio_limit}.")
                    except Exception:
                        logging.exception(f"Failed to set share limits for torrent '{torrent.name}' (hash: {torrent.hash})")

    logging.info("Tagging by tracker completed.")
