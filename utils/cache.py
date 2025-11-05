"""
Caching utilities for qBittorrent API responses.

Provides in-memory caching with TTL (time-to-live) to reduce redundant API calls
within a single script execution.
"""

import time
import logging
import json
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

    def __init__(self, default_ttl: int = 300):
        """
        Initialize the cache.

        Args:
            default_ttl: Default time-to-live in seconds (default: 300 = 5 minutes)
        """
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a value from the cache.

        Args:
            key: Cache key
            default: Value to return if key not found or expired (default: None)

        Returns:
            Cached value if found and not expired, default otherwise
        """
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
            ttl: Time-to-live in seconds (uses default if not specified)
        """
        if ttl is None:
            ttl = self._default_ttl

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

        return {
            'hits': self._hits,
            'misses': self._misses,
            'size': len(self._cache),
            'hit_rate': round(hit_rate, 2)
        }

    def log_stats(self) -> None:
        """Log cache statistics."""
        stats = self.stats()
        logging.info(f"Cache stats - Hits: {stats['hits']}, Misses: {stats['misses']}, "
                     f"Size: {stats['size']}, Hit rate: {stats['hit_rate']}%")


# Global cache instance
_global_cache = SimpleCache()


def get_cache() -> SimpleCache:
    """
    Get the global cache instance.

    Returns:
        Global SimpleCache instance
    """
    return _global_cache


def cached(ttl: int = 300, key_prefix: str = ""):
    """
    Decorator to cache function results.

    Args:
        ttl: Time-to-live in seconds
        key_prefix: Prefix for cache keys

    Example:
        @cached(ttl=60, key_prefix="tracker_config")
        def get_tracker_config(client, torrent_hash):
            return client.torrents_trackers(torrent_hash)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key from function name and arguments
            # Build tuple of key components for unambiguous serialization
            key_components = (
                key_prefix,
                func.__qualname__,  # Use __qualname__ for better identification
                args[1:],  # Skip 'self' or 'client'
                tuple(sorted(kwargs.items()))
            )

            # Serialize with json.dumps for stability, fallback to repr for non-JSON types
            try:
                cache_key = json.dumps(key_components, sort_keys=True)
            except (TypeError, ValueError):
                # Fallback to repr for non-JSON-serializable objects
                cache_key = repr(key_components)

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
