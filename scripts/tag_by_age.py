import logging
import datetime
from typing import List, Dict, Any
from collections import defaultdict


def tag_by_age(client, torrents: List[Any], config: Dict[str, Any], dry_run: bool = False) -> None:
    """
    Tag torrents based on their age in months using batched API calls.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to tag
        config: Configuration dictionary (unused currently, reserved for future age bucket config)
        dry_run: If True, only log actions without making changes

    Performance: Uses batched API calls - 1 call per age bucket instead of 1 per torrent.
    """
    try:
        current_time = datetime.datetime.now()

        # Group torrents by age tag for batch processing
        tag_groups = defaultdict(list)
        tag_counts = defaultdict(int)

        logging.info(f"Calculating age tags for {len(torrents)} torrents...")

        for torrent in torrents:
            try:
                # Calculate the age of the torrent in months
                torrent_age_months = (current_time.year - torrent.creation_date.year) * 12 + (
                    current_time.month - torrent.creation_date.month
                )

                # Determine the appropriate tag based on age buckets in months
                if torrent_age_months < 1:
                    logging.debug(f"Skipping torrent '{torrent.name}': under one month old")
                    continue
                if torrent_age_months >= 6:
                    tag = '6_months_plus'
                elif torrent_age_months >= 5:
                    tag = '>5_months'
                elif torrent_age_months >= 4:
                    tag = '>4_months'
                elif torrent_age_months >= 3:
                    tag = '>3_months'
                elif torrent_age_months >= 2:
                    tag = '>2_months'
                else:
                    tag = '>1_month'

                # Group torrents by tag for batch processing
                tag_groups[tag].append(torrent.hash)
                tag_counts[tag] += 1

                logging.debug(f"Torrent '{torrent.name}' ({torrent_age_months} months old) -> tag '{tag}'")

            except AttributeError as e:
                logging.warning(f"Skipping torrent '{getattr(torrent, 'name', 'unknown')}': missing creation_date attribute: {e}")
            except Exception as e:
                logging.error(f"Error processing torrent '{getattr(torrent, 'name', 'unknown')}': {e}")

        # Apply tags in batches (one API call per tag instead of one per torrent)
        total_tagged = 0
        for tag, torrent_hashes in tag_groups.items():
            try:
                if dry_run:
                    logging.info(f"[Dry Run] Would add tag '{tag}' to {len(torrent_hashes)} torrents")
                else:
                    client.torrents_add_tags(torrent_hashes=torrent_hashes, tags=[tag])
                    logging.info(f"Added tag '{tag}' to {len(torrent_hashes)} torrents")
                total_tagged += len(torrent_hashes)
            except Exception as e:
                logging.error(f"Failed to add tag '{tag}' to batch of {len(torrent_hashes)} torrents: {e}")

        # Summary
        logging.info(f"Tagging by age completed: {total_tagged}/{len(torrents)} torrents tagged")
        for tag, count in sorted(tag_counts.items()):
            logging.info(f"  - {tag}: {count} torrents")

    except Exception as e:
        logging.error(f"Error in tag_by_age: {e}")
        raise
