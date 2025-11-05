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

    def test_invalid_tracker_tags(self):
        """Test that invalid tracker_tags structure raises error."""
        config = {
            'host': 'localhost:8080',
            'username': 'admin',
            'password': 'password',
            'tracker_tags': {
                'test_tracker': {
                    'tag': 'TEST',
                    'seed_time_limit': -1,  # Invalid negative value
                }
            }
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "must be a non-negative number" in str(exc_info.value)


class TestExcludePatternValidation:
    """Test exclude pattern validation."""

    def test_validate_dangerous_pattern(self, caplog):
        """Test that dangerous patterns generate warnings."""
        validate_exclude_patterns(['*'], [])
        assert "will match ALL files" in caplog.text

    def test_validate_relative_dir_path(self, caplog):
        """Test that relative paths generate warnings."""
        validate_exclude_patterns([], ['relative/path'])
        assert "should be absolute" in caplog.text
