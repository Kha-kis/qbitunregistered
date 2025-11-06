import logging
import datetime
from typing import Sequence, Dict, Any, DefaultDict
from collections import defaultdict
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.types import TorrentInfo, QBittorrentClient


def tag_by_age(
    client: QBittorrentClient, torrents: Sequence[TorrentInfo], _config: Dict[str, Any], dry_run: bool = False
) -> None:
    """
    Tag torrents based on their age in months using batched API calls.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to tag
        _config: Configuration dictionary (unused currently, reserved for future age bucket config)
        dry_run: If True, only log actions without making changes

    Performance: Uses batched API calls - 1 call per age bucket instead of 1 per torrent.
    """
    try:
        current_time = datetime.datetime.now()

        # Group torrents by age tag for batch processing
        tag_groups = defaultdict(list)
        tag_counts: DefaultDict[str, int] = defaultdict(int)

        logging.info(f"Calculating age tags for {len(torrents)} torrents...")

        for torrent in torrents:
            try:
                # Get added_on timestamp (when torrent was added to qBittorrent)
                added_on = getattr(torrent, "added_on", None)
                if not added_on:
                    logging.debug(f"Skipping torrent '{torrent.name}': missing added_on timestamp")
                    continue

                # Convert timestamp to datetime
                created_at = datetime.datetime.fromtimestamp(added_on)

                # Calculate the age of the torrent in months (accounting for day-of-month)
                months_diff = (current_time.year - created_at.year) * 12 + (current_time.month - created_at.month)

                # Adjust if the day hasn't passed yet in the current month
                # (e.g., created Jan 31, checked Feb 1 should be 0 months, not 1)
                if current_time.day < created_at.day:
                    months_diff -= 1

                torrent_age_months = months_diff

                # Determine the appropriate tag based on age buckets in months
                # Handle future creation dates (system clock issues or data errors)
                if torrent_age_months < 0:
                    logging.warning(
                        f"Skipping torrent '{torrent.name}': creation date is in the future "
                        f"({created_at} > {current_time}). "
                        f"Check system clock or torrent metadata."
                    )
                    continue

                # Skip torrents less than 1 month old
                if torrent_age_months < 1:
                    logging.debug(f"Skipping torrent '{torrent.name}': under one month old")
                    continue
                if torrent_age_months >= 6:
                    tag = "6_months_plus"
                elif torrent_age_months >= 5:
                    tag = ">5_months"
                elif torrent_age_months >= 4:
                    tag = ">4_months"
                elif torrent_age_months >= 3:
                    tag = ">3_months"
                elif torrent_age_months >= 2:
                    tag = ">2_months"
                else:
                    tag = ">1_month"

                # Group torrents by tag for batch processing
                tag_groups[tag].append(torrent.hash)
                tag_counts[tag] += 1

                logging.debug(f"Torrent '{torrent.name}' ({torrent_age_months} months old) -> tag '{tag}'")

            except AttributeError as e:
                logging.warning(
                    f"Skipping torrent '{getattr(torrent, 'name', 'unknown')}': missing added_on attribute: {e}"
                )
            except Exception:
                logging.exception(f"Error processing torrent '{getattr(torrent, 'name', 'unknown')}'")

        # Apply tags in batches (one API call per tag instead of one per torrent)
        total_tagged = 0
        for tag, torrent_hashes in tag_groups.items():
            try:
                if dry_run:
                    logging.info(f"[Dry Run] Would add tag '{tag}' to {len(torrent_hashes)} torrents")
                else:
                    client.torrents_add_tags(torrent_hashes=torrent_hashes, tags=tag)
                    logging.info(f"Added tag '{tag}' to {len(torrent_hashes)} torrents")
                total_tagged += len(torrent_hashes)
            except Exception:
                logging.exception(f"Failed to add tag '{tag}' to batch of {len(torrent_hashes)} torrents")

        # Summary with age-based sort order (oldest to youngest)
        bucket_order = ["6_months_plus", ">5_months", ">4_months", ">3_months", ">2_months", ">1_month"]

        logging.info(f"Tagging by age completed: {total_tagged}/{len(torrents)} torrents tagged")
        for tag in bucket_order:
            if tag in tag_counts:
                count = tag_counts[tag]
                logging.info(f"  - {tag}: {count} torrents")

    except Exception:
        logging.exception("Error in tag_by_age")
        raise
