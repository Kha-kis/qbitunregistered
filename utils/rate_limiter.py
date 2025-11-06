"""
Rate limiting utilities for API calls.

Provides simple rate limiting to avoid overwhelming the qBittorrent API.

CURRENT STATUS: Not currently used in the codebase.

RATIONALE: The codebase has been optimized to use batched API calls throughout
(e.g., tag_by_tracker, auto_tmm, unregistered_checks all batch operations).
Batching reduces API call volume from O(N) to O(1) or O(K) where K is number of
unique tags/configurations, eliminating the need for rate limiting.

FUTURE USE: This implementation is kept available for scenarios where:
- Individual per-torrent API calls become necessary
- Integration with rate-limited external services
- Running in environments with strict API quotas
- Long-running daemon mode with continuous API access

To use, apply the @rate_limited decorator to any function making API calls:
    @rate_limited(max_calls=10, time_window=60)
    def my_api_call(client, ...):
        ...
"""

import time
import logging
from typing import Optional
from functools import wraps


class RateLimiter:
    """
    Simple rate limiter using token bucket algorithm.

    Limits the number of operations per time window.
    """

    def __init__(self, max_calls: int = 100, time_window: float = 60.0):
        """
        Initialize the rate limiter.

        Args:
            max_calls: Maximum number of calls allowed in the time window
            time_window: Time window in seconds (default: 60 seconds)
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = []
        self._total_delayed = 0
        self._total_calls = 0

    def acquire(self) -> None:
        """
        Acquire permission to make an API call.

        Blocks if rate limit would be exceeded.
        """
        current_time = time.time()
        self._total_calls += 1

        # Remove calls outside the time window
        self.calls = [call_time for call_time in self.calls if current_time - call_time < self.time_window]

        # If at limit, wait until oldest call expires
        if len(self.calls) >= self.max_calls:
            oldest_call = self.calls[0]
            sleep_time = self.time_window - (current_time - oldest_call) + 0.01  # Small buffer

            if sleep_time > 0:
                logging.debug(f"Rate limit reached, sleeping for {sleep_time:.2f}s")
                self._total_delayed += 1
                time.sleep(sleep_time)
                current_time = time.time()

                # Clean up old calls after sleeping
                self.calls = [call_time for call_time in self.calls if current_time - call_time < self.time_window]

        # Record this call
        self.calls.append(current_time)

    def reset(self) -> None:
        """Reset the rate limiter state."""
        self.calls.clear()
        self._total_delayed = 0
        self._total_calls = 0

    def stats(self) -> dict:
        """
        Get rate limiter statistics.

        Returns:
            Dictionary with stats (total_calls, delayed_calls, current_rate)
        """
        current_time = time.time()
        recent_calls = [call_time for call_time in self.calls if current_time - call_time < self.time_window]

        return {
            'total_calls': self._total_calls,
            'delayed_calls': self._total_delayed,
            'current_rate': len(recent_calls),
            'max_rate': self.max_calls,
            'time_window': self.time_window
        }

    def log_stats(self) -> None:
        """Log rate limiter statistics."""
        stats = self.stats()
        if stats['total_calls'] > 0:
            delay_rate = (stats['delayed_calls'] / stats['total_calls'] * 100)
            logging.info(f"Rate limiter stats - Total calls: {stats['total_calls']}, "
                         f"Delayed: {stats['delayed_calls']} ({delay_rate:.1f}%), "
                         f"Current rate: {stats['current_rate']}/{stats['max_rate']} per {stats['time_window']}s")


# Global rate limiter instance
_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> Optional[RateLimiter]:
    """
    Get the global rate limiter instance.

    Returns:
        Global RateLimiter instance or None if not initialized
    """
    return _global_rate_limiter


def init_rate_limiter(max_calls: int = 100, time_window: float = 60.0) -> RateLimiter:
    """
    Initialize the global rate limiter.

    Args:
        max_calls: Maximum number of calls allowed in the time window
        time_window: Time window in seconds

    Returns:
        Global RateLimiter instance
    """
    global _global_rate_limiter
    _global_rate_limiter = RateLimiter(max_calls, time_window)
    logging.info(f"Rate limiter initialized: {max_calls} calls per {time_window}s")
    return _global_rate_limiter


def rate_limited(func):
    """
    Decorator to rate limit a function.

    Applies global rate limiter if initialized.

    Example:
        @rate_limited
        def api_call(client, data):
            return client.some_method(data)
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        limiter = get_rate_limiter()
        if limiter:
            limiter.acquire()
        return func(*args, **kwargs)

    return wrapper


def log_rate_limiter_stats() -> None:
    """Log global rate limiter statistics."""
    limiter = get_rate_limiter()
    if limiter:
        limiter.log_stats()
