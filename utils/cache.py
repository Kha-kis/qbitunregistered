"""
Caching utilities for qBittorrent API responses.

Provides in-memory caching with TTL (time-to-live) to reduce redundant API calls
within a single script execution.

Cache Design Notes:
- In-memory only: Cache is cleared between script runs
- Default TTL: 300 seconds (5 minutes) is sufficient for single-execution scripts
- Configuration: TTL is currently hardcoded by design. The script typically runs once
  and exits, so configurable TTLs add complexity without clear benefit. If you need
  different TTLs for different environments (e.g., long-running daemon mode), the
  decorator's ttl parameter can be modified or made configurable via config.json.
"""

import time
import logging
import json
import pickle
import hashlib
from typing import Any, Optional, Callable, Dict, Tuple, Union
from functools import wraps


# Sentinel object to distinguish cache misses from cached None values
_CACHE_MISS = object()


class SimpleCache:
    """
    Simple in-memory cache with TTL (time-to-live) support.

    This cache is designed for single-script execution caching to avoid
    repeated API calls for the same data within one run.
    """

    # Cleanup thresholds (configurable class constants)
    CLEANUP_ACCESS_THRESHOLD = 100  # Trigger cleanup after this many accesses
    CLEANUP_TIME_THRESHOLD = 300  # Trigger cleanup after this many seconds (5 minutes)

    def __init__(self, default_ttl: int = 300):
        """
        Initialize the cache.

        Args:
            default_ttl: Default time-to-live in seconds (default: 300 = 5 minutes, must be > 0)

        Raises:
            ValueError: If default_ttl is not a positive number
        """
        if not isinstance(default_ttl, (int, float)) or default_ttl <= 0:
            raise ValueError(f"default_ttl must be a positive number, got: {default_ttl}")

        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0
        self._access_count = 0  # Track accesses for periodic cleanup
        self._last_cleanup = time.time()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a value from the cache.

        Args:
            key: Cache key
            default: Value to return if key not found or expired (default: None)

        Returns:
            Cached value if found and not expired, default otherwise
        """
        # Trigger periodic cleanup to prevent memory leaks
        self._maybe_cleanup()

        if key not in self._cache:
            self._misses += 1
            logging.debug(f"Cache miss: {key}")
            return default

        value, expiry = self._cache[key]

        # Check if expired
        if time.time() > expiry:
            del self._cache[key]
            self._misses += 1
            logging.debug(f"Cache expired: {key}")
            return default

        self._hits += 1
        logging.debug(f"Cache hit: {key}")
        return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        Set a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if not specified, must be > 0)

        Raises:
            ValueError: If ttl is not a positive integer
        """
        if ttl is None:
            ttl = self._default_ttl

        # Validate TTL to prevent immediately expired entries
        if not isinstance(ttl, (int, float)) or ttl <= 0:
            raise ValueError(f"ttl must be a positive number, got: {ttl}")

        expiry = time.time() + ttl
        self._cache[key] = (value, expiry)
        logging.debug(f"Cache set: {key} (TTL: {ttl}s)")

    def invalidate(self, key: str) -> None:
        """
        Invalidate a cache entry.

        Args:
            key: Cache key to invalidate
        """
        if key in self._cache:
            del self._cache[key]
            logging.debug(f"Cache invalidated: {key}")

    def clear(self) -> None:
        """Clear all cache entries."""
        count = len(self._cache)
        self._cache.clear()
        logging.debug(f"Cache cleared: {count} entries removed")

    def stats(self) -> Dict[str, Union[int, float]]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats (hits, misses, size, hit_rate)
            - hits: int
            - misses: int
            - size: int
            - hit_rate: float (percentage)
        """
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0

        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache), "hit_rate": round(hit_rate, 2)}

    def log_stats(self) -> None:
        """Log cache statistics."""
        stats = self.stats()
        logging.info(
            f"Cache stats - Hits: {stats['hits']}, Misses: {stats['misses']}, "
            f"Size: {stats['size']}, Hit rate: {stats['hit_rate']}%"
        )

    def cleanup_expired(self) -> int:
        """
        Remove all expired entries from the cache.

        This is automatically called periodically based on CLEANUP_ACCESS_THRESHOLD
        and CLEANUP_TIME_THRESHOLD to prevent memory leaks in long-running processes.

        Returns:
            Number of expired entries removed
        """
        now = time.time()
        expired_keys = [key for key, (_, expiry) in self._cache.items() if now > expiry]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logging.debug(f"Cache cleanup: removed {len(expired_keys)} expired entries")

        self._last_cleanup = now
        return len(expired_keys)

    def _maybe_cleanup(self) -> None:
        """
        Conditionally trigger cache cleanup based on access count or time.

        Cleanup is triggered when:
        - CLEANUP_ACCESS_THRESHOLD cache accesses have occurred since last cleanup, OR
        - CLEANUP_TIME_THRESHOLD seconds have elapsed since last cleanup
        """
        self._access_count += 1

        # Trigger cleanup based on configurable thresholds
        if (
            self._access_count >= self.CLEANUP_ACCESS_THRESHOLD
            or (time.time() - self._last_cleanup) >= self.CLEANUP_TIME_THRESHOLD
        ):
            self.cleanup_expired()
            self._access_count = 0


# Global cache instance
_global_cache = SimpleCache()


def get_cache() -> SimpleCache:
    """
    Get the global cache instance.

    Returns:
        Global SimpleCache instance
    """
    return _global_cache


def cached(ttl: int = 300, key_prefix: str = "", skip_first_arg: bool = True):
    """
    Decorator to cache function results.

    Args:
        ttl: Time-to-live in seconds
        key_prefix: Prefix for cache keys
        skip_first_arg: If True, skip the first positional argument (e.g., 'self' or 'client')
                        when generating cache keys. Set to False for standalone functions
                        where all arguments should be included in the cache key.

    Example:
        @cached(ttl=60, key_prefix="tracker_config")
        def get_tracker_config(client, torrent_hash):
            return client.torrents_trackers(torrent_hash)

        @cached(ttl=60, key_prefix="config", skip_first_arg=False)
        def get_system_config(config_name):
            return load_config(config_name)
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key from function name and arguments
            # Build tuple of key components for unambiguous serialization
            cache_args = args[1:] if skip_first_arg else args
            key_components = (
                key_prefix,
                func.__qualname__,  # Use __qualname__ for better identification
                cache_args,
                tuple(sorted(kwargs.items())),
            )

            # Serialize with json.dumps for stability, fallback to pickle+hash for non-JSON types
            try:
                cache_key = json.dumps(key_components, sort_keys=True)
            except (TypeError, ValueError):
                # Fallback to pickle with deterministic hash for non-JSON-serializable objects
                # This provides consistent cache keys across runs for complex objects
                try:
                    pickled = pickle.dumps(key_components, protocol=pickle.HIGHEST_PROTOCOL)
                    cache_key = f"pickled:{hashlib.sha256(pickled).hexdigest()}"
                except (TypeError, pickle.PicklingError) as e:
                    # If even pickle fails, log error and skip caching for this call
                    logging.warning(f"Cannot generate cache key for {func.__qualname__}: {e}. Skipping cache.")
                    return func(*args, **kwargs)

            # Try to get from cache using sentinel to distinguish misses from None values
            cache = get_cache()
            cached_value = cache.get(cache_key, default=_CACHE_MISS)

            if cached_value is not _CACHE_MISS:
                return cached_value

            # Call the actual function
            result = func(*args, **kwargs)

            # Cache the result
            cache.set(cache_key, result, ttl)

            return result

        return wrapper

    return decorator


def clear_cache() -> None:
    """Clear the global cache."""
    get_cache().clear()


def log_cache_stats() -> None:
    """Log global cache statistics."""
    get_cache().log_stats()
