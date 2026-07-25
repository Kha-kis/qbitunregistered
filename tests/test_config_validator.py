"""Tests for configuration validation."""

import json
from pathlib import Path
import pytest
from qbitunregistered.config import (
    validate_config,
    validate_exclude_patterns,
    resolve_dry_run,
    ConfigValidationError,
)


class TestConfigValidation:
    """Test configuration validation."""

    def test_valid_config(self):
        """Test that a valid configuration passes validation."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "dry_run": True,
            "default_unregistered_tag": "unregistered",
            "cross_seeding_tag": "unregistered:crossseeding",
        }
        # Should not raise any exception
        validate_config(config)

    def test_missing_host(self):
        """Test that missing host raises error."""
        config = {
            "username": "admin",
            "password": "password",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'host'" in str(exc_info.value)

    def test_missing_username(self):
        """Test that missing username raises error when no api_key is set."""
        config = {
            "host": "localhost:8080",
            "password": "password",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'username'" in str(exc_info.value)

    def test_missing_password(self):
        """Test that missing password raises error when no api_key is set."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'password'" in str(exc_info.value)

    def test_valid_config_with_api_key(self):
        """Test that api_key alone (no username/password) passes validation."""
        config = {
            "host": "localhost:8080",
            "api_key": "qbt_abc123",
        }
        validate_config(config)

    def test_api_key_does_not_require_username_password(self):
        """Test that api_key makes username and password optional."""
        config = {
            "host": "localhost:8080",
            "api_key": "qbt_abc123",
            "dry_run": True,
        }
        validate_config(config)

    def test_empty_api_key_uses_username_password(self):
        """Test that an empty api_key falls back to username/password."""
        config = {
            "host": "localhost:8080",
            "api_key": "   ",
            "username": "admin",
            "password": "password",
        }
        validate_config(config)

    def test_empty_api_key_without_credentials_raises_error(self):
        """Test that an empty api_key still requires username/password."""
        config = {
            "host": "localhost:8080",
            "api_key": "",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'username'" in str(exc_info.value)
        assert "Missing required field: 'password'" in str(exc_info.value)

    def test_invalid_api_key_type(self):
        """Test that a non-string api_key raises an error."""
        config = {
            "host": "localhost:8080",
            "api_key": 12345,
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Field 'api_key' must be a string" in str(exc_info.value)

    def test_example_config_is_valid(self):
        """Test that the committed example config passes validation."""
        config_path = Path(__file__).parents[1] / "config.json.example"
        with config_path.open(encoding="utf-8") as config_file:
            validate_config(json.load(config_file))

    def test_invalid_host_format(self):
        """Test that invalid host format raises error."""
        config = {
            "host": "localhost",  # Missing port
            "username": "admin",
            "password": "password",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Invalid host format" in str(exc_info.value)

    def test_empty_hostname(self):
        """Test that empty hostname raises error."""
        config = {
            "host": ":8080",  # Empty hostname
            "username": "admin",
            "password": "password",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Hostname cannot be empty" in str(exc_info.value)

    def test_whitespace_hostname(self):
        """Test that whitespace-only hostname raises error."""
        config = {
            "host": " :8080",  # Whitespace hostname
            "username": "admin",
            "password": "password",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Hostname cannot be empty" in str(exc_info.value)

    def test_invalid_dry_run_type(self):
        """Test that non-boolean dry_run raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "dry_run": "yes",  # Should be boolean
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "'dry_run' must be a boolean" in str(exc_info.value)

    def test_invalid_scheduled_time_format(self):
        """Test that invalid scheduled time format raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["25:00"],  # Invalid hour
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Invalid hour" in str(exc_info.value)

    def test_valid_scheduled_times(self):
        """Test that valid scheduled times pass validation."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["09:00", "15:30", "23:59:59"],
            "scheduled_operations": ["unregistered", "orphaned"],
        }
        # Should not raise any exception
        validate_config(config)

    @pytest.mark.parametrize("value", ["false", 1, None])
    def test_delete_files_values_must_be_boolean(self, value):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "delete_files": {"unregistered": value},
        }

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)

        assert "'delete_files[unregistered]' must be a boolean" in str(exc_info.value)

    def test_scheduled_times_require_operations(self):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["09:00"],
            "scheduled_operations": [],
        }

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)

        assert "at least one operation" in str(exc_info.value)

    def test_unknown_scheduled_operation_is_rejected(self):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["09:00"],
            "scheduled_operations": ["delete_everything"],
        }

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)

        assert "Unknown scheduled operation" in str(exc_info.value)

    @pytest.mark.parametrize("target_dir", [None, "", "relative/path"])
    def test_scheduled_hard_links_require_absolute_target_dir(self, target_dir):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["09:00"],
            "scheduled_operations": ["create_hard_links"],
        }
        if target_dir is not None:
            config["target_dir"] = target_dir

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)

        assert "'target_dir' must be" in str(exc_info.value)
        assert "'create_hard_links'" in str(exc_info.value)

    def test_scheduled_hard_links_accept_absolute_target_dir(self, tmp_path):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": ["09:00"],
            "scheduled_operations": ["create_hard_links"],
            "target_dir": str(tmp_path / "hard-links"),
        }

        validate_config(config)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("orphan_min_age_seconds", -1, "non-negative integer"),
            ("orphan_min_age_seconds", True, "non-negative integer"),
            ("orphan_min_age_seconds", 1.5, "non-negative integer"),
            ("orphan_max_candidates", 0, "null or a positive integer"),
            ("orphan_max_candidates", False, "null or a positive integer"),
            ("orphan_max_candidates", "10", "null or a positive integer"),
        ],
    )
    def test_invalid_orphan_safety_limits(self, field, value, message):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            field: value,
        }

        with pytest.raises(ConfigValidationError, match=message):
            validate_config(config)

    def test_valid_orphan_safety_limits(self):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "orphan_min_age_seconds": 0,
            "orphan_max_candidates": 10,
        }

        validate_config(config)

    def test_dormant_scheduled_hard_links_do_not_require_target_dir(self):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "scheduled_times": [],
            "scheduled_operations": ["create_hard_links"],
        }

        validate_config(config)

    def test_valid_tracker_tags_with_limits(self):
        """Test that valid tracker_tags with seed limits pass validation."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "tracker_tags": {
                "test_tracker": {
                    "tag": "TEST",
                    "seed_time_limit": -2,  # Valid: -2 = no limit
                    "seed_ratio_limit": -1,  # Valid: -1 = use global
                }
            },
        }
        # Should not raise any exception
        validate_config(config)

    def test_invalid_tracker_tags(self):
        """Test that invalid tracker_tags structure raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "tracker_tags": {
                "test_tracker": {
                    "tag": "TEST",
                    "seed_time_limit": -3,  # Invalid: must be >= -2 (API: -2=no limit, -1=global, 0+=specific)
                }
            },
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "must be an integer >= -2" in str(exc_info.value)

    def test_invalid_apprise_url(self):
        """Test that invalid apprise_url type raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "apprise_url": 123,  # Should be string
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "'apprise_url' must be a string" in str(exc_info.value)

    def test_invalid_notifiarr_key(self):
        """Test that invalid notifiarr_key type raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "notifiarr_key": 123,  # Should be string
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "'notifiarr_key' must be a string" in str(exc_info.value)

    def test_invalid_notifiarr_channel(self):
        """Test that invalid notifiarr_channel type raises error."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "notifiarr_channel": 123,  # Should be string
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "'notifiarr_channel' must be a string" in str(exc_info.value)

    def test_notifiarr_channel_must_be_numeric_and_valid_length(self):
        """Test that notifiarr_channel must be numeric and correct length."""
        # Non-numeric string
        config_non_numeric = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "notifiarr_channel": "abc123",
            "notifiarr_key": "dummy",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config_non_numeric)
        msg = str(exc_info.value)
        assert "'notifiarr_channel' must be a numeric Discord channel ID" in msg

        # Numeric but too short
        config_short = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "notifiarr_channel": "123456789012345",  # 15 digits
            "notifiarr_key": "dummy",
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config_short)
        msg = str(exc_info.value)
        assert "appears invalid (expected 17-20 digits" in msg

    def test_notifiarr_key_and_channel_must_be_set_together(self):
        """Test that Notifiarr key and channel are validated as a pair."""
        base = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
        }

        # Key without channel
        config_key_only = {**base, "notifiarr_key": "dummy"}
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config_key_only)
        msg = str(exc_info.value)
        assert "'notifiarr_channel' must be set when 'notifiarr_key' is provided" in msg

        # Channel without key
        config_channel_only = {**base, "notifiarr_channel": "12345678901234567"}
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config_channel_only)
        msg = str(exc_info.value)
        assert "'notifiarr_key' must be set when 'notifiarr_channel' is provided" in msg

    def test_recycle_bin_not_dir(self, tmp_path):
        """Test that recycle bin path pointing to a file raises error."""
        file_path = tmp_path / "file.txt"
        file_path.touch()

        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "recycle_bin": str(file_path),
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "is not a directory" in str(exc_info.value)

    def test_recycle_bin_does_not_exist_ok(self, tmp_path):
        """Test that non-existent recycle bin path is allowed (will be created later)."""
        non_existent_path = tmp_path / "does_not_exist"

        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "recycle_bin": str(non_existent_path),
        }
        # Should not raise exception
        validate_config(config)

    def test_recycle_bin_not_writable(self, tmp_path):
        """Test that non-writable recycle bin raises error."""
        from unittest.mock import patch

        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "recycle_bin": str(tmp_path),
        }

        with patch("os.access", return_value=False):
            with pytest.raises(ConfigValidationError) as exc_info:
                validate_config(config)
            assert "is not writable" in str(exc_info.value)

    def test_recycle_bin_must_be_absolute(self):
        """Test that recycle bin must be an absolute path (security requirement)."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "recycle_bin": "relative/path/to/recycle",
        }

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "must be an absolute path" in str(exc_info.value)
        assert "security requirement" in str(exc_info.value)

    def test_nonexistent_orphan_scan_root_is_valid(self, tmp_path):
        """Explicit scan roots are format-validated without requiring existence."""
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "orphan_scan_roots": [str(tmp_path / "not-created")],
        }

        validate_config(config)

    def test_orphan_scan_roots_must_be_a_list(self, tmp_path):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "orphan_scan_roots": str(tmp_path),
        }

        with pytest.raises(ConfigValidationError, match="'orphan_scan_roots' must be a list"):
            validate_config(config)

    @pytest.mark.parametrize(
        ("root", "expected_error"),
        [
            (123, "must be a string"),
            ("", "must be a nonblank absolute path"),
            ("   ", "must be a nonblank absolute path"),
            ("relative/path", "must be an absolute path"),
        ],
    )
    def test_orphan_scan_root_entries_fail_closed(self, root, expected_error):
        config = {
            "host": "localhost:8080",
            "username": "admin",
            "password": "password",
            "orphan_scan_roots": [root],
        }

        with pytest.raises(ConfigValidationError, match=expected_error):
            validate_config(config)


class TestDryRunResolution:
    """Test command-line and configuration dry-run precedence."""

    def test_uses_config_when_cli_is_unspecified(self):
        assert resolve_dry_run(None, {"dry_run": True}) is True
        assert resolve_dry_run(None, {"dry_run": False}) is False

    def test_cli_overrides_config(self):
        assert resolve_dry_run(True, {"dry_run": False}) is True
        assert resolve_dry_run(False, {"dry_run": True}) is False

    def test_defaults_to_false(self):
        assert resolve_dry_run(None, {}) is False

    def test_invalid_config_value_is_rejected(self):
        with pytest.raises(ConfigValidationError):
            resolve_dry_run(None, {"dry_run": "true"})


class TestExcludePatternValidation:

    def test_validate_dangerous_pattern(self, caplog):
        """Test that dangerous patterns generate warnings."""
        validate_exclude_patterns(["*"], [])
        assert "will match ALL files" in caplog.text

    def test_validate_relative_dir_path(self):
        """Test that relative paths raise errors (security requirement)."""
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_exclude_patterns([], ["relative/path"])
        assert "must be absolute" in str(exc_info.value)
        assert "security requirement" in str(exc_info.value)
