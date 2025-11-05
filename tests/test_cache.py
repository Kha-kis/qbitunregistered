"""Tests for caching functionality."""
import pytest
import time
from utils.cache import SimpleCache, cached, get_cache, clear_cache


class TestSimpleCache:
    """Test SimpleCache functionality."""

    def test_cache_set_and_get(self):
        """Test basic cache set and get operations."""
        cache = SimpleCache(default_ttl=60)
        cache.set("test_key", "test_value")

        result = cache.get("test_key")
        assert result == "test_value"

    def test_cache_miss(self):
        """Test cache miss returns None."""
        cache = SimpleCache()
        result = cache.get("nonexistent_key")
        assert result is None

    def test_cache_expiry(self):
        """Test that cached values expire after TTL."""
        cache = SimpleCache(default_ttl=1)  # 1 second TTL
        cache.set("expiring_key", "expiring_value", ttl=1)

        # Should exist immediately
        assert cache.get("expiring_key") == "expiring_value"

        # Wait for expiration
        time.sleep(1.1)

        # Should be None after expiry
        assert cache.get("expiring_key") is None

    def test_cache_custom_ttl(self):
        """Test custom TTL per cache entry."""
        cache = SimpleCache(default_ttl=60)
        cache.set("short_ttl", "value1", ttl=1)
        cache.set("long_ttl", "value2", ttl=60)

        time.sleep(1.1)

        # Short TTL should be expired
        assert cache.get("short_ttl") is None

        # Long TTL should still exist
        assert cache.get("long_ttl") == "value2"

    def test_cache_invalidate(self):
        """Test manual cache invalidation."""
        cache = SimpleCache()
        cache.set("key_to_invalidate", "value")

        # Should exist
        assert cache.get("key_to_invalidate") == "value"

        # Invalidate
        cache.invalidate("key_to_invalidate")

        # Should be None after invalidation
        assert cache.get("key_to_invalidate") is None

    def test_cache_clear(self):
        """Test clearing all cache entries."""
        cache = SimpleCache()
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Clear all
        cache.clear()

        # All should be None
        assert cache.get("key1") is None
        assert cache.get("key2") is None
        assert cache.get("key3") is None

    def test_cache_stats(self):
        """Test cache statistics tracking."""
        cache = SimpleCache()
        cache.set("key1", "value1")

        # Generate some hits and misses
        cache.get("key1")  # Hit
        cache.get("key1")  # Hit
        cache.get("nonexistent")  # Miss
        cache.get("nonexistent2")  # Miss

        stats = cache.stats()

        assert stats['hits'] == 2
        assert stats['misses'] == 2
        assert stats['size'] == 1
        assert stats['hit_rate'] == 50.0

    def test_cache_stats_no_requests(self):
        """Test cache stats with no requests."""
        cache = SimpleCache()
        stats = cache.stats()

        assert stats['hits'] == 0
        assert stats['misses'] == 0
        assert stats['size'] == 0
        assert stats['hit_rate'] == 0


class TestCachedDecorator:
    """Test the @cached decorator."""

    def test_cached_decorator_basic(self):
        """Test basic @cached decorator functionality."""
        call_count = 0

        @cached(ttl=60, key_prefix="test")
        def expensive_function(client, param):
            nonlocal call_count
            call_count += 1
            return f"result_{param}"

        # First call should execute function
        result1 = expensive_function("client", "value1")
        assert result1 == "result_value1"
        assert call_count == 1

        # Second call with same params should use cache
        result2 = expensive_function("client", "value1")
        assert result2 == "result_value1"
        assert call_count == 1  # Should not increment

        # Call with different params should execute function
        result3 = expensive_function("client", "value2")
        assert result3 == "result_value2"
        assert call_count == 2

    def test_cached_decorator_expiry(self):
        """Test @cached decorator respects TTL."""
        call_count = 0

        @cached(ttl=1, key_prefix="test")
        def expiring_function(client, param):
            nonlocal call_count
            call_count += 1
            return f"result_{param}"

        # First call
        result1 = expiring_function("client", "value")
        assert call_count == 1

        # Immediate second call should use cache
        result2 = expiring_function("client", "value")
        assert call_count == 1

        # Wait for expiry
        time.sleep(1.1)

        # Should execute function again after expiry
        result3 = expiring_function("client", "value")
        assert call_count == 2

    def test_global_cache_operations(self):
        """Test global cache get and clear functions."""
        global_cache = get_cache()

        # Set a value
        global_cache.set("global_key", "global_value")
        assert global_cache.get("global_key") == "global_value"

        # Clear cache
        clear_cache()
        assert global_cache.get("global_key") is None


class TestCacheTypes:
    """Test caching of different data types."""

    def test_cache_strings(self):
        """Test caching string values."""
        cache = SimpleCache()
        cache.set("str_key", "string value")
        assert cache.get("str_key") == "string value"

    def test_cache_integers(self):
        """Test caching integer values."""
        cache = SimpleCache()
        cache.set("int_key", 12345)
        assert cache.get("int_key") == 12345

    def test_cache_lists(self):
        """Test caching list values."""
        cache = SimpleCache()
        test_list = [1, 2, 3, 4, 5]
        cache.set("list_key", test_list)
        assert cache.get("list_key") == test_list

    def test_cache_dicts(self):
        """Test caching dictionary values."""
        cache = SimpleCache()
        test_dict = {"key1": "value1", "key2": "value2"}
        cache.set("dict_key", test_dict)
        assert cache.get("dict_key") == test_dict

    def test_cache_none_value(self):
        """Test that None values are cached properly."""
        cache = SimpleCache()
        cache.set("none_key", None)

        # None should be cached and returned (not treated as miss)
        result = cache.get("none_key")
        assert result is None

        # Check stats to verify it was a hit, not a miss
        stats = cache.stats()
        assert stats['hits'] == 1
        assert stats['misses'] == 0
