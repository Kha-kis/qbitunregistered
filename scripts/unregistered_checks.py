import logging
from urllib.parse import urlsplit
from typing import List, Set, Tuple
from tqdm import tqdm


def compile_patterns(unregistered: List[str]) -> Tuple[Set[str], Set[str]]:
    """
    Pre-compile patterns into two sets for efficient matching.

    Args:
        unregistered: List of unregistered patterns from config

    Returns:
        Tuple of (exact_match_set, starts_with_set)

    Security:
        Validates that starts_with patterns have non-empty prefixes to prevent
        universal matching (empty prefix would match everything).
    """
    exact_matches = set()
    starts_with_patterns = set()

    for pattern in unregistered:
        lower_pattern = pattern.lower()
        if lower_pattern.startswith("starts_with:"):
            # Extract the prefix after "starts_with:"
            prefix = lower_pattern.split("starts_with:", 1)[1]

            # Security: Normalize and validate prefix to prevent empty matches
            prefix = prefix.strip()

            if not prefix:
                # Empty prefix would match everything - log warning and skip
                logging.warning(
                    f"Skipping malformed pattern '{pattern}': starts_with prefix is empty. "
                    "Empty prefixes would match all messages."
                )
                continue

            starts_with_patterns.add(prefix)
        else:
            exact_matches.add(lower_pattern)

    return exact_matches, starts_with_patterns


def check_unregistered_message(tracker, exact_matches: Set[str], starts_with_patterns: Set[str]) -> bool:
    """
    Check if tracker message matches any unregistered pattern.

    Args:
        tracker: Tracker object with msg attribute
        exact_matches: Set of exact match patterns (pre-compiled, lowercase)
        starts_with_patterns: Set of starts_with patterns (pre-compiled, lowercase)

    Returns:
        True if message matches any pattern
    """
    lower_msg = tracker.msg.lower()

    # Check exact matches first (O(1) lookup)
    if lower_msg in exact_matches:
        return True

    # Check starts_with patterns (O(n) where n is number of starts_with patterns)
    for prefix in starts_with_patterns:
        if lower_msg.startswith(prefix):
            return True

    return False


def process_torrent(torrent, exact_matches: Set[str], starts_with_patterns: Set[str]) -> int:
    """
    Count unregistered trackers for a torrent.

    Args:
        torrent: Torrent object
        exact_matches: Pre-compiled exact match patterns
        starts_with_patterns: Pre-compiled starts_with patterns

    Returns:
        Count of unregistered trackers
    """
    unregistered_count = sum(
        1
        for tracker in torrent.trackers
        if check_unregistered_message(tracker, exact_matches, starts_with_patterns) and tracker.status == 4
    )
    return unregistered_count


def update_torrent_file_paths(torrent_file_paths, torrent):
    torrent_file_paths.setdefault(torrent.save_path, []).append(torrent.hash)


def delete_torrents_and_files(client, config, use_delete_tags, delete_tags, delete_files, dry_run, torrents=None):
    """Delete torrents with specific tags. Pass torrents to avoid redundant API call."""
    if use_delete_tags:
        # Use provided torrents list to avoid redundant API call
        if torrents is None:
            torrents = client.torrents.info()

        for torrent in torrents:
            torrent_deleted = False
            for tag in delete_tags:
                if tag in torrent.tags:
                    if delete_files.get(tag, False):
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=True)
                            logging.info(f"Deleted torrent '{torrent.name}' with hash {torrent.hash} and its files.")
                        else:
                            logging.info(
                                f"[Dry Run] Would delete torrent '{torrent.name}' with hash {torrent.hash} and its files."
                            )
                    else:
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=False)
                            logging.info(f"Deleted torrent '{torrent.name}' with hash {torrent.hash}.")
                        else:
                            logging.info(f"[Dry Run] Would delete torrent '{torrent.name}' with hash {torrent.hash}.")
                    torrent_deleted = True
                    break  # Exit the inner loop after deleting the torrent

            # Skip to next torrent if this one was deleted (object is now stale)
            if torrent_deleted:
                continue


def unregistered_checks(client, torrents, config, use_delete_tags, delete_tags, delete_files, dry_run):
    """
    Check torrents for unregistered status and apply appropriate tags.

    Uses batched API calls for maximum performance.

    Args:
        client: qBittorrent client
        torrents: List of torrents to check
        config: Configuration dictionary
        use_delete_tags: Whether to use delete tags
        delete_tags: List of tags that trigger deletion
        delete_files: Dictionary mapping tags to delete_files boolean
        dry_run: If True, don't make actual changes

    Returns:
        Tuple of (torrent_file_paths, unregistered_counts_per_path)
    """
    torrent_file_paths = {}
    unregistered_counts_per_path = {}
    unregistered_torrents_per_path = {}  # Track number of torrents (not tracker hits) with unregistered trackers
    tag_counts = {}
    default_tag = config["default_unregistered_tag"]
    cross_seeding_tag = config["cross_seeding_tag"]

    # Pre-compile patterns for efficient matching
    unregistered_patterns = config.get("unregistered", [])
    exact_matches, starts_with_patterns = compile_patterns(unregistered_patterns)

    # First pass: Collect all torrent data and unregistered status
    # Store per-path lists of unregistered torrent hashes for second pass
    unregistered_hashes_per_path = {}

    for torrent in tqdm(torrents, desc="Checking for unregistered torrents", unit="torrent"):
        update_torrent_file_paths(torrent_file_paths, torrent)

        # Use pre-compiled patterns for faster matching
        unregistered_count = process_torrent(torrent, exact_matches, starts_with_patterns)

        unregistered_counts_per_path[torrent.save_path] = (
            unregistered_counts_per_path.get(torrent.save_path, 0) + unregistered_count
        )

        # Track unregistered torrents per path (don't assign tags yet)
        if unregistered_count > 0:
            # Track number of torrents (not tracker hits) with any unregistered tracker
            unregistered_torrents_per_path[torrent.save_path] = unregistered_torrents_per_path.get(torrent.save_path, 0) + 1
            # Store this torrent hash for the path
            if torrent.save_path not in unregistered_hashes_per_path:
                unregistered_hashes_per_path[torrent.save_path] = []
            unregistered_hashes_per_path[torrent.save_path].append(torrent.hash)

    # Second pass: Now that we have complete per-path counts, assign tags correctly
    default_tag_hashes = []
    cross_seeding_tag_hashes = []

    for save_path, unregistered_hashes in unregistered_hashes_per_path.items():
        # Now we can accurately check if ALL torrents in this path have unregistered trackers
        total_torrents_in_path = len(torrent_file_paths[save_path])
        unregistered_torrents_in_path = unregistered_torrents_per_path[save_path]

        is_all_unregistered = unregistered_torrents_in_path == total_torrents_in_path

        if is_all_unregistered:
            # All torrents in this path have unregistered trackers
            default_tag_hashes.extend(unregistered_hashes)
            tag_counts[default_tag] = tag_counts.get(default_tag, 0) + len(unregistered_hashes)
        else:
            # Only some torrents have unregistered trackers (cross-seeding)
            cross_seeding_tag_hashes.extend(unregistered_hashes)
            tag_counts[cross_seeding_tag] = tag_counts.get(cross_seeding_tag, 0) + len(unregistered_hashes)

    # Apply tags in batches (2 API calls instead of N)
    if not dry_run:
        if default_tag_hashes:
            try:
                client.torrents_add_tags(torrent_hashes=default_tag_hashes, tags=[default_tag])
                logging.info(f"Added tag '{default_tag}' to {len(default_tag_hashes)} torrents")
            except Exception as e:
                logging.error(f"Failed to add tag '{default_tag}' in batch: {e}")

        if cross_seeding_tag_hashes:
            try:
                client.torrents_add_tags(torrent_hashes=cross_seeding_tag_hashes, tags=[cross_seeding_tag])
                logging.info(f"Added tag '{cross_seeding_tag}' to {len(cross_seeding_tag_hashes)} torrents")
            except Exception as e:
                logging.error(f"Failed to add tag '{cross_seeding_tag}' in batch: {e}")
    else:
        if default_tag_hashes:
            logging.info(f"[Dry Run] Would add tag '{default_tag}' to {len(default_tag_hashes)} torrents")
        if cross_seeding_tag_hashes:
            logging.info(f"[Dry Run] Would add tag '{cross_seeding_tag}' to {len(cross_seeding_tag_hashes)} torrents")

    delete_torrents_and_files(client, config, use_delete_tags, delete_tags, delete_files, dry_run, torrents)

    for tag, count in tag_counts.items():
        logging.info("Tag: %s, Count: %d", tag, count)

    return torrent_file_paths, unregistered_counts_per_path
