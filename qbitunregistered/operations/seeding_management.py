import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

from qbitunregistered.tracker_matcher import match_tracker_url
from qbitunregistered.cache import cached
from qbitunregistered.types import QBittorrentClient


@cached(ttl=None, key_prefix="torrent_trackers")
def fetch_torrent_trackers(client: QBittorrentClient, torrent_hash: str, *, cache_scope: int | None) -> list[Any]:
    """
    Fetch tracker metadata once per client and torrent during one execution.

    Args:
        client: qBittorrent client instance
        torrent_hash: Torrent hash
        cache_scope: Client-specific cache identity; pass ``id(client)``.

    Returns:
        Tracker metadata returned by qBittorrent.
    """
    if cache_scope is None:
        raise ValueError("cache_scope must be provided (use id(client))")
    trackers = cast(object, client.torrents_trackers(torrent_hash=torrent_hash))
    if trackers is None or not isinstance(trackers, Sequence) or isinstance(trackers, (str, bytes)):
        raise RuntimeError(f"qBittorrent returned malformed tracker metadata for torrent {torrent_hash}")
    return list(cast(Sequence[Any], trackers))


# Kept as a private alias for callers that imported the previous helper.
_fetch_trackers = fetch_torrent_trackers


def find_tracker_config(
    client: QBittorrentClient,
    torrent: Any,
    config: dict[str, Any],
    *,
    raise_on_error: bool = False,
) -> dict[str, Any] | None:
    """
    Find matching tracker configuration for a torrent.

    Uses caching to avoid repeated API calls for the same torrent.

    Args:
        client: qBittorrent client instance
        torrent: Torrent object
        config: Configuration dictionary with tracker_tags
        raise_on_error: Propagate tracker API failures for fail-closed callers.

    Returns:
        Matching tracker configuration dict or None if no match found.
        Returns None on API errors unless ``raise_on_error`` is true.
    """
    try:
        trackers = fetch_torrent_trackers(client, torrent.hash, cache_scope=id(client))
    except Exception:
        if raise_on_error:
            raise
        logging.exception(f"Failed to fetch trackers for torrent {torrent.hash}")
        return None

    tracker_tags_config = config.get("tracker_tags", {})

    for tracker in trackers:
        # Skip non-dict trackers
        if not isinstance(tracker, dict):
            continue

        tracker_url = tracker.get("url", "")
        if not tracker_url:
            continue

        # Use utility function for matching
        matched_config = match_tracker_url(tracker_url, tracker_tags_config)
        if matched_config is not None:
            return matched_config

    return None


def apply_seed_limits(
    client: QBittorrentClient,
    config: dict[str, Any],
    torrents: list[Any] | None = None,
    dry_run: bool = False,
) -> None:
    """
    Apply both seeding time and ratio limits using batched API calls.

    This function consolidates apply_seed_time and apply_seed_ratio to reduce
    API calls. It groups torrents by their (time_limit, ratio_limit) configuration
    and makes one API call per unique configuration instead of per-torrent calls.

    Performance: For 1,000 torrents with 5 unique limit configurations, this makes
    5 API calls instead of 1,000 (200x reduction).

    Note: Tracker fetching uses per-torrent API calls with execution-scoped caching.
    qBittorrent API doesn't support batch tracker fetching, so we rely on
    the shared cached fetch path to minimize repeated calls within one run.

    Args:
        client: qBittorrent client instance
        config: Configuration dictionary with tracker_tags
        torrents: Optional list of torrents (avoids redundant API call if provided)
        dry_run: If True, only log actions without making changes
    """
    if torrents is None:
        try:
            fetched_torrents = client.torrents.info()
        except Exception:
            logging.exception("Failed to fetch torrent list")
            return
        if fetched_torrents is None:
            logging.error("qBittorrent returned no torrent list; seed limits were not changed")
            return
        resolved_torrents = fetched_torrents
    else:
        resolved_torrents = torrents

    logging.debug(f"Applying seed limits to {len(resolved_torrents)} torrents")

    # Group torrents by share limit configuration for batching
    # Key: (time_limit_int, ratio_limit_float)
    # Value: list of torrent hashes
    torrents_by_limits = defaultdict(list)

    # First pass: Collect and validate all torrents
    for torrent in resolved_torrents:
        tracker_tag_config = find_tracker_config(client, torrent, config)

        if tracker_tag_config is not None:
            seed_time_limit = tracker_tag_config.get("seed_time_limit")
            seed_ratio_limit = tracker_tag_config.get("seed_ratio_limit")

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
                    logging.warning(
                        f"Invalid seed_time_limit value '{seed_time_limit}' for torrent '{torrent.name}' (hash: {torrent.hash}): {type(e).__name__}: {e}"
                    )

            if seed_ratio_limit is not None:
                try:
                    ratio_limit_float = float(seed_ratio_limit)
                except (ValueError, TypeError) as e:
                    logging.warning(
                        f"Invalid seed_ratio_limit value '{seed_ratio_limit}' for torrent '{torrent.name}' (hash: {torrent.hash}): {type(e).__name__}: {e}"
                    )

            # Group by limits configuration (use tuple as key for batching)
            if time_limit_int is not None or ratio_limit_float is not None:
                limits_key = (time_limit_int, ratio_limit_float)
                torrents_by_limits[limits_key].append(torrent.hash)

    # Second pass: Apply share limits in batches (one API call per unique configuration)
    # Note: Batch operations are all-or-nothing for performance.
    for (time_limit, ratio_limit), torrent_hashes in torrents_by_limits.items():
        try:
            if dry_run:
                logging.info(
                    f"[Dry Run] Would update share limits for {len(torrent_hashes)} torrents "
                    f"(time: {time_limit} min, ratio: {ratio_limit})"
                )
            else:
                client.torrents_set_share_limits(
                    torrent_hashes=torrent_hashes,
                    ratio_limit=ratio_limit if ratio_limit is not None else -2.0,
                    seeding_time_limit=time_limit if time_limit is not None else -2,
                )
                logging.info(
                    f"Updated share limits for {len(torrent_hashes)} torrents "
                    f"(time: {time_limit} min, ratio: {ratio_limit})"
                )
        except Exception:
            logging.exception(
                f"Failed to set share limits for batch of {len(torrent_hashes)} torrents "
                f"(time: {time_limit}, ratio: {ratio_limit}). "
                f"Check qBittorrent API compatibility and values. "
                f"Affected torrent hashes: {torrent_hashes[:3]}{'...' if len(torrent_hashes) > 3 else ''}"
            )


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
