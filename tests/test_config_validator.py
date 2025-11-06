"""Tests for configuration validation."""
import pytest
from utils.config_validator import validate_config, validate_exclude_patterns, ConfigValidationError


class TestConfigValidation:
    """Test configuration validation."""

    def test_valid_config(self):
        """Test that a valid configuration passes validation."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'dry_run': True,
            'default_unregistered_tag': 'unregistered',
            'cross_seeding_tag': 'unregistered:crossseeding',
        }
        # Should not raise any exception
        validate_config(config)

    def test_missing_host(self):
        """Test that missing host raises error."""
        config = {
            'username': 'admin',
            'password': 'password',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'host'" in str(exc_info.value)

    def test_missing_username(self):
        """Test that missing username raises error."""
        config = {
            'host': 'localhost:8080',
            'password': 'password',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'username'" in str(exc_info.value)

    def test_missing_password(self):
        """Test that missing password raises error."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Missing required field: 'password'" in str(exc_info.value)

    def test_invalid_host_format(self):
        """Test that invalid host format raises error."""
        config = {
            'host': 'localhost',  # Missing port
            'username': 'admin',
            'password': 'password',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Invalid host format" in str(exc_info.value)

    def test_empty_hostname(self):
        """Test that empty hostname raises error."""
        config = {
            'host': ':8080',  # Empty hostname
            'username': 'admin',
            'password': 'password',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Hostname cannot be empty" in str(exc_info.value)

    def test_whitespace_hostname(self):
        """Test that whitespace-only hostname raises error."""
        config = {
            'host': ' :8080',  # Whitespace hostname
            'username': 'admin',
            'password': 'password',
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Hostname cannot be empty" in str(exc_info.value)

    def test_invalid_dry_run_type(self):
        """Test that non-boolean dry_run raises error."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'dry_run': 'yes',  # Should be boolean
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "'dry_run' must be a boolean" in str(exc_info.value)

    def test_invalid_scheduled_time_format(self):
        """Test that invalid scheduled time format raises error."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'scheduled_times': ['25:00'],  # Invalid hour
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Invalid hour" in str(exc_info.value)

    def test_valid_scheduled_times(self):
        """Test that valid scheduled times pass validation."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'scheduled_times': ['09:00', '15:30', '23:59:59'],
        }
        # Should not raise any exception
        validate_config(config)

    def test_valid_tracker_tags_with_limits(self):
        """Test that valid tracker_tags with seed limits pass validation."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'tracker_tags': {
                'test_tracker': {
                    'tag': 'TEST',
                    'seed_time_limit': -2,  # Valid: -2 = no limit
                    'seed_ratio_limit': -1,  # Valid: -1 = use global
                }
            }
        }
        # Should not raise any exception
        validate_config(config)

    def test_invalid_tracker_tags(self):
        """Test that invalid tracker_tags structure raises error."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'tracker_tags': {
                'test_tracker': {
                    'tag': 'TEST',
                    'seed_time_limit': -3,  # Invalid: must be >= -2 (API: -2=no limit, -1=global, 0+=specific)
                }
            }
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "must be an integer >= -2" in str(exc_info.value)


class TestExcludePatternValidation:
    """Test exclude pattern validation."""

    def test_validate_dangerous_pattern(self, caplog):
        """Test that dangerous patterns generate warnings."""
        validate_exclude_patterns(['*'], [])
        assert "will match ALL files" in caplog.text

    def test_validate_relative_dir_path(self):
        """Test that relative paths raise errors (security requirement)."""
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_exclude_patterns([], ['relative/path'])
        assert "must be absolute" in str(exc_info.value)
        assert "security requirement" in str(exc_info.value)
