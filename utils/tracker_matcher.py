"""
Tracker URL matching utility for qBittorrent tracker configuration.

This module provides functionality to match tracker URLs against configured
tracker patterns and retrieve associated configuration.
"""

import logging
from typing import Dict, Any, Optional
from urllib.parse import urlparse


def match_tracker_url(tracker_url: str, tracker_tags_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Match a tracker URL against configured tracker patterns.

    Performs case-insensitive domain-based matching to find a tracker configuration
    that matches the given URL. Extracts the domain from the URL and matches against
    tracker keys to avoid false positives from generic substrings.

    Args:
        tracker_url (str): The tracker URL to match (e.g., "https://tracker.example.com/announce")
        tracker_tags_config (dict): Dictionary mapping tracker identifiers to their configurations.
                                    Keys are tracker identifiers (e.g., "aither", "blutopia"),
                                    values are configuration dictionaries.

    Returns:
        dict or None: The matching tracker configuration dictionary if found, None otherwise.
                     Configuration typically contains keys like 'tag', 'seed_time_limit', 'seed_ratio_limit'.

    Examples:
        >>> config = {
        ...     'aither': {'tag': 'AITHER', 'seed_time_limit': 10000},
        ...     'blutopia': {'tag': 'BLU', 'seed_ratio_limit': 1.5}
        ... }
        >>> match_tracker_url('https://aither.cc/announce/abc123', config)
        {'tag': 'AITHER', 'seed_time_limit': 10000}
        >>> print(match_tracker_url('https://unknown.com/announce', config))
        None
        >>> print(match_tracker_url('', config))
        None

    Notes:
        - Matching is case-insensitive (e.g., "AITHER" matches "aither.cc")
        - Uses domain-based matching (e.g., "aither" matches domain "aither.cc" but not path "/aither/...")
        - Returns the first match found (order depends on dict iteration)
        - Empty or None URLs will not match any pattern
        - Falls back to substring matching if URL parsing fails (e.g., for "** [DHT] **")
    """
    if not tracker_url:
        return None

    if not isinstance(tracker_tags_config, dict):
        return None

    # Extract domain from URL for more precise matching
    try:
        parsed = urlparse(tracker_url)
        domain = parsed.netloc.lower()

        # If we successfully extracted a domain, use domain-based matching
        if domain:
            for tracker_key, tracker_config in tracker_tags_config.items():
                if tracker_key.lower() in domain:
                    return tracker_config
            return None
    except Exception:
        # If URL parsing fails, fall through to substring matching
        logging.debug(f"URL parsing failed for tracker URL '{tracker_url}', falling back to substring matching")
        pass

    # Fallback to substring matching for non-standard tracker URLs
    # (e.g., "** [DHT] **", "** [PeX] **", "** [LSD] **")
    tracker_url_lower = tracker_url.lower()
    for tracker_key, tracker_config in tracker_tags_config.items():
        if tracker_key.lower() in tracker_url_lower:
            return tracker_config

    return None


# Unit tests (can be run with: python3 -m doctest utils/tracker_matcher.py -v)
if __name__ == "__main__":
    import doctest

    doctest.testmod()

    # Additional unit tests
    print("Running unit tests...")

    # Test case 1: Basic matching
    test_config = {
        "aither": {"tag": "AITHER", "seed_time_limit": 10000},
        "blutopia": {"tag": "BLU", "seed_ratio_limit": 1.5},
        "beyond-hd": {"tag": "BHD", "seed_time_limit": 200},
    }

    tests = [
        # (tracker_url, expected_tag_or_none)
        ("https://aither.cc/announce/abc123", "AITHER"),
        ("https://blutopia.xyz/announce/def456", "BLU"),
        ("https://beyond-hd.me/announce", "BHD"),
        ("https://unknown-tracker.com/announce", None),
        ("", None),
        ("** [DHT] **", None),
        ("** [PeX] **", None),
        ("HTTPS://AITHER.CC/ANNOUNCE", "AITHER"),  # Case insensitive
    ]

    all_passed = True
    for url, expected_tag in tests:
        result = match_tracker_url(url, test_config)
        actual_tag = result.get("tag") if result else None

        if actual_tag == expected_tag:
            print(f"  ✅ PASS: '{url[:50]}' -> {actual_tag}")
        else:
            print(f"  ❌ FAIL: '{url[:50]}' -> Expected {expected_tag}, got {actual_tag}")
            all_passed = False

    # Edge case tests
    print("\nEdge case tests:")

    # Test with None config
    result = match_tracker_url("https://aither.cc/announce", None)
    if result is None:
        print("  ✅ PASS: None config returns None")
    else:
        print(f"  ❌ FAIL: None config should return None, got {result}")
        all_passed = False

    # Test with empty config
    result = match_tracker_url("https://aither.cc/announce", {})
    if result is None:
        print("  ✅ PASS: Empty config returns None")
    else:
        print(f"  ❌ FAIL: Empty config should return None, got {result}")
        all_passed = False

    # Test with None URL
    result = match_tracker_url(None, test_config)
    if result is None:
        print("  ✅ PASS: None URL returns None")
    else:
        print(f"  ❌ FAIL: None URL should return None, got {result}")
        all_passed = False

    # Domain-based matching tests (false positive prevention)
    print("\nDomain-based matching tests:")

    # Test that generic key 'bt' in path doesn't match
    test_config_generic = {"bt": {"tag": "BT", "seed_time_limit": 100}}
    result = match_tracker_url("https://tracker.example.com/announce/bt/12345", test_config_generic)
    if result is None:
        print("  ✅ PASS: Generic 'bt' in path doesn't match (avoids false positive)")
    else:
        print(f"  ❌ FAIL: Generic 'bt' in path should not match, got {result}")
        all_passed = False

    # Test that tracker key matches domain correctly
    result = match_tracker_url("https://bt-tracker.com/announce", test_config_generic)
    actual_tag = result.get("tag") if result else None
    if actual_tag == "BT":
        print("  ✅ PASS: 'bt' matches domain 'bt-tracker.com'")
    else:
        print(f"  ❌ FAIL: 'bt' should match domain 'bt-tracker.com', got {actual_tag}")
        all_passed = False

    # Test subdomain matching
    result = match_tracker_url("https://announce.blutopia.xyz/tracker", test_config)
    actual_tag = result.get("tag") if result else None
    if actual_tag == "BLU":
        print("  ✅ PASS: 'blutopia' matches subdomain 'announce.blutopia.xyz'")
    else:
        print(f"  ❌ FAIL: 'blutopia' should match subdomain, got {actual_tag}")
        all_passed = False

    if all_passed:
        print("\n✅ All unit tests passed!")
    else:
        print("\n❌ Some tests failed!")
        exit(1)
