import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from qbitunregistered.cache import get_cache
from qbitunregistered.tracker_matcher import match_tracker_url
from qbitunregistered.types import QBittorrentClient

_TRACKER_CACHE_MISS = object()
_MALFORMED_TRACKER_METADATA = object()
_PSEUDO_TRACKER_URLS = ("** [DHT] **", "** [PeX] **", "** [LSD] **")


class MalformedEmbeddedTrackerMetadataError(RuntimeError):
    """Raised when a successful bulk response contains malformed trackers."""


def _tracker_cache_key(torrent_hash: str, cache_scope: int) -> str:
    return f"torrent_trackers:{cache_scope}:{torrent_hash}"


def _bulk_tracker_cache_key(cache_scope: int) -> str:
    return f"torrent_trackers:{cache_scope}:bulk"


def _store_tracker_metadata(
    torrent_hash: str,
    cache_scope: int,
    trackers: list[Any] | object,
) -> None:
    get_cache().set_for_execution(_tracker_cache_key(torrent_hash, cache_scope), trackers)


def prime_torrent_trackers(client: QBittorrentClient, torrents: Sequence[Any]) -> None:
    """Preload embedded tracker metadata for one authoritative snapshot."""
    torrent_hashes: set[str] = set()
    for torrent in torrents:
        torrent_hash = torrent.get("hash") if isinstance(torrent, Mapping) else getattr(torrent, "hash", None)
        if not isinstance(torrent_hash, str) or not torrent_hash or torrent_hash in torrent_hashes:
            raise RuntimeError("qBittorrent returned a missing or duplicate torrent hash while preloading tracker metadata")
        torrent_hashes.add(torrent_hash)

    tracker_metadata_by_hash: dict[str, list[Any] | object] = {}
    for torrent in torrents:
        if not isinstance(torrent, Mapping) or "trackers" not in torrent:
            continue
        torrent_hash = cast(str, torrent.get("hash"))
        trackers = torrent["trackers"]
        if isinstance(trackers, list):
            tracker_metadata_by_hash[torrent_hash] = trackers
        elif isinstance(trackers, Sequence) and not isinstance(trackers, (str, bytes, bytearray)):
            tracker_metadata_by_hash[torrent_hash] = list(trackers)
        else:
            tracker_metadata_by_hash[torrent_hash] = _MALFORMED_TRACKER_METADATA

    cache_scope = id(client)
    get_cache().set_for_execution(_bulk_tracker_cache_key(cache_scope), tracker_metadata_by_hash)


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

    cache = get_cache()
    bulk_cache_key = _bulk_tracker_cache_key(cache_scope)
    bulk_trackers = cache.get(bulk_cache_key, _TRACKER_CACHE_MISS)
    if bulk_trackers is not _TRACKER_CACHE_MISS and torrent_hash in cast(dict[str, object], bulk_trackers):
        # Record a bulk hit only after resolving it, so an exact fallback keeps
        # one cache miss per API fetch in the operator-facing statistics.
        cache.get(bulk_cache_key, namespace="torrent_trackers")
        cached_trackers = cast(dict[str, object], bulk_trackers)[torrent_hash]
    else:
        cached_trackers = cache.get(
            _tracker_cache_key(torrent_hash, cache_scope),
            _TRACKER_CACHE_MISS,
            namespace="torrent_trackers",
        )
    if cached_trackers is _MALFORMED_TRACKER_METADATA:
        raise MalformedEmbeddedTrackerMetadataError(
            f"qBittorrent returned malformed tracker metadata for torrent {torrent_hash}"
        )
    if cached_trackers is not _TRACKER_CACHE_MISS:
        return cast(list[Any], cached_trackers)

    trackers = cast(object, client.torrents_trackers(torrent_hash=torrent_hash))
    if trackers is None or not isinstance(trackers, Sequence) or isinstance(trackers, (str, bytes, bytearray)):
        raise RuntimeError(f"qBittorrent returned malformed tracker metadata for torrent {torrent_hash}")
    cached_trackers = list(cast(Sequence[Any], trackers))
    _store_tracker_metadata(torrent_hash, cache_scope, cached_trackers)
    return cached_trackers


# Kept as a private alias for callers that imported the previous helper.
_fetch_trackers = fetch_torrent_trackers


def _find_matching_tracker_config(trackers: Sequence[Any], tracker_tags_config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first configured pseudo or supplied tracker match."""
    for tracker_url in _PSEUDO_TRACKER_URLS:
        matched_config = match_tracker_url(tracker_url, tracker_tags_config)
        if matched_config is not None:
            return matched_config

    for tracker in trackers:
        if not isinstance(tracker, dict):
            continue

        tracker_url = tracker.get("url", "")
        if not tracker_url:
            continue

        matched_config = match_tracker_url(tracker_url, tracker_tags_config)
        if matched_config is not None:
            return matched_config
    return None


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
    except MalformedEmbeddedTrackerMetadataError:
        raise
    except Exception:
        if raise_on_error:
            raise
        logging.exception(f"Failed to fetch trackers for torrent {torrent.hash}")
        return None

    tracker_tags_config = config.get("tracker_tags", {})
    return _find_matching_tracker_config(trackers, tracker_tags_config)


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
