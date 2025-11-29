import logging
from collections import defaultdict
from typing import Sequence
from tqdm import tqdm
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.file_operations import fetch_torrent_files  # noqa: E402
from utils.types import TorrentInfo, QBittorrentClient  # noqa: E402


def tag_cross_seeds(client: QBittorrentClient, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
    """
    Tags torrents as 'cross-seed' if multiple torrents share the same file structure,
    otherwise tags them as 'not-cross-seeding'.

    Uses batched API calls for tagging (1 call per tag type instead of 1 per torrent).

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to analyze
        dry_run: If True, only log actions without making changes

    Performance: File structure detection requires individual API calls (unavoidable),
                 but tagging is batched for efficiency.
    """
    try:
        file_structure_map = defaultdict(list)
        tag_counts = {"cross-seed": 0, "not-cross-seeding": 0}
        errors = 0

        logging.info(f"Analyzing file structures for {len(torrents)} torrents...")

        # Collect torrents by their file structure
        for torrent in tqdm(torrents, desc="Analyzing file structures", unit="torrent", disable=dry_run):
            try:
                torrent_path = getattr(torrent, "save_path", None)
                if not torrent_path:
                    logging.warning(f"Skipping torrent '{torrent.name}': missing save_path")
                    errors += 1
                    continue

                # Fetch file list for this torrent (cached to reduce API calls)
                try:
                    torrent_files = fetch_torrent_files(client, torrent.hash, cache_scope=id(client))
                except Exception as e:
                    logging.error(f"Failed to fetch files for torrent '{torrent.name}': {e}")
                    errors += 1
                    continue

                if not torrent_files:
                    logging.warning(f"Skipping torrent '{torrent.name}': no files found")
                    errors += 1
                    continue

                # Create a frozen set of file names for comparison
                file_structure = frozenset(f["name"] for f in torrent_files if isinstance(f, dict) and "name" in f)

                if not file_structure:
                    logging.warning(f"Skipping torrent '{torrent.name}': could not determine file structure")
                    errors += 1
                    continue

                file_structure_map[file_structure].append(torrent)

            except Exception as e:
                logging.error(f"Error processing torrent '{getattr(torrent, 'name', 'unknown')}': {e}")
                errors += 1

        logging.info(f"Collected {len(file_structure_map)} unique file structures from {len(torrents) - errors} torrents")
        if errors > 0:
            logging.warning(f"Skipped {errors} torrents due to errors")

        # Group torrents by tag for batch processing
        cross_seed_hashes = []
        not_cross_seeding_hashes = []

        for file_structure, torrent_list in file_structure_map.items():
            logging.debug(
                f"File structure with {len(list(file_structure)[:3])}... files found in {len(torrent_list)} torrents"
            )

            if len(torrent_list) > 1:
                # Cross-seeding detected
                tag_counts["cross-seed"] += len(torrent_list)
                cross_seed_hashes.extend([t.hash for t in torrent_list])
                for torrent in torrent_list:
                    logging.debug(f"Cross-seed detected: '{torrent.name}'")
            else:
                # Not cross-seeding
                tag_counts["not-cross-seeding"] += len(torrent_list)
                not_cross_seeding_hashes.extend([t.hash for t in torrent_list])

        # Apply tags in batches with proper cleanup of contradictory tags
        # Remove inverse tags first, then add the correct tag
        total_tagged = 0
        total_removed = 0

        if cross_seed_hashes:
            try:
                # Remove contradictory tag first
                if dry_run:
                    logging.info(f"[Dry Run] Would remove tag 'not-cross-seeding' from {len(cross_seed_hashes)} torrents")
                    logging.info(f"[Dry Run] Would add tag 'cross-seed' to {len(cross_seed_hashes)} torrents")
                else:
                    # Remove opposite tag to prevent contradictions
                    client.torrents_remove_tags(torrent_hashes=cross_seed_hashes, tags="not-cross-seeding")
                    total_removed += len(cross_seed_hashes)
                    logging.debug(f"Removed tag 'not-cross-seeding' from {len(cross_seed_hashes)} torrents (cleanup)")

                    # Add the correct tag
                    client.torrents_add_tags(torrent_hashes=cross_seed_hashes, tags="cross-seed")
                    logging.info(f"Added tag 'cross-seed' to {len(cross_seed_hashes)} torrents")

                total_tagged += len(cross_seed_hashes)
            except Exception as e:
                logging.error(f"Failed to update 'cross-seed' tags for batch: {e}")

        if not_cross_seeding_hashes:
            try:
                # Remove contradictory tag first
                if dry_run:
                    logging.info(f"[Dry Run] Would remove tag 'cross-seed' from {len(not_cross_seeding_hashes)} torrents")
                    logging.info(f"[Dry Run] Would add tag 'not-cross-seeding' to {len(not_cross_seeding_hashes)} torrents")
                else:
                    # Remove opposite tag to prevent contradictions
                    client.torrents_remove_tags(torrent_hashes=not_cross_seeding_hashes, tags="cross-seed")
                    total_removed += len(not_cross_seeding_hashes)
                    logging.debug(f"Removed tag 'cross-seed' from {len(not_cross_seeding_hashes)} torrents (cleanup)")

                    # Add the correct tag
                    client.torrents_add_tags(torrent_hashes=not_cross_seeding_hashes, tags="not-cross-seeding")
                    logging.info(f"Added tag 'not-cross-seeding' to {len(not_cross_seeding_hashes)} torrents")

                total_tagged += len(not_cross_seeding_hashes)
            except Exception as e:
                logging.error(f"Failed to update 'not-cross-seeding' tags for batch: {e}")

        # Summary
        logging.info(
            f"Cross-seed tagging completed: {total_tagged} torrents tagged, {total_removed} contradictory tags removed"
        )
        logging.info(f"  - cross-seed: {tag_counts['cross-seed']} torrents")
        logging.info(f"  - not-cross-seeding: {tag_counts['not-cross-seeding']} torrents")

    except Exception as e:
        logging.error(f"Error in tag_cross_seeds: {e}")
        raise
