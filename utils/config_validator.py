"""Configuration validation utilities for qbitunregistered."""
import logging
from typing import Dict, List, Any
from pathlib import Path
from urllib.parse import urlparse


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


def validate_config(config: Dict[str, Any]) -> None:
    """
    Validate the configuration dictionary.

    Args:
        config: Configuration dictionary loaded from JSON

    Raises:
        ConfigValidationError: If configuration is invalid
    """
    errors = []

    # Required fields
    required_fields = ['host', 'username', 'password']
    for field in required_fields:
        if field not in config or not config[field]:
            errors.append(f"Missing required field: '{field}'")

    # Validate host format
    if 'host' in config:
        host = config['host']
        if host:
            # Parse as URL to support both 'hostname:port' and full URLs like 'https://example.com:8080/qbittorrent'
            parsed = urlparse(host)

            # If scheme is present, it should be http or https
            if parsed.scheme and parsed.scheme not in ['http', 'https']:
                errors.append(f"Invalid host scheme: '{parsed.scheme}'. Use 'http' or 'https', or omit for 'hostname:port' format")

            # If no scheme, treat as 'hostname:port' format (e.g., 'localhost:8080')
            if not parsed.scheme:
                if ':' not in host:
                    errors.append(f"Invalid host format: '{host}'. Expected 'hostname:port' or full URL like 'http://hostname:port/path'")
                else:
                    # Validate simple 'hostname:port' format
                    parts = host.split(':', 1)
                    try:
                        port = int(parts[1])
                        if not (1 <= port <= 65535):
                            errors.append(f"Invalid port number: {port}. Must be between 1 and 65535")
                    except ValueError:
                        errors.append(f"Invalid port in host: '{parts[1]}'. Must be a number")

            # If scheme is present, netloc should be populated
            elif not parsed.netloc:
                errors.append(f"Invalid host URL: '{host}'. Missing hostname/netloc")

    # Validate dry_run is boolean
    if 'dry_run' in config and not isinstance(config['dry_run'], bool):
        errors.append(f"'dry_run' must be a boolean, got: {type(config['dry_run']).__name__}")

    # Validate log_level
    if 'log_level' in config:
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        log_level = config['log_level']
        if not isinstance(log_level, str):
            errors.append(f"'log_level' must be a string, got: {type(log_level).__name__}")
        elif log_level.upper() not in valid_levels:
            errors.append(f"'log_level' must be one of {valid_levels}, got: '{log_level}'")

    # Validate log_file is a string if provided
    if 'log_file' in config and config['log_file']:
        if not isinstance(config['log_file'], str):
            errors.append(f"'log_file' must be a string path, got: {type(config['log_file']).__name__}")

    # Validate tags are strings
    for tag_field in ['default_unregistered_tag', 'cross_seeding_tag', 'other_issues_tag']:
        if tag_field in config and not isinstance(config[tag_field], str):
            errors.append(f"'{tag_field}' must be a string, got: {type(config[tag_field]).__name__}")

    # Validate boolean flags
    for bool_field in ['use_delete_tags', 'use_delete_files', 'auto_tmm_enabled',
                       'torrent_changed_tmm_enabled', 'save_path_changed_tmm_enabled',
                       'category_changed_tmm_enabled']:
        if bool_field in config and not isinstance(config[bool_field], bool):
            errors.append(f"'{bool_field}' must be a boolean, got: {type(config[bool_field]).__name__}")

    # Validate lists
    for list_field in ['delete_tags', 'exclude_files', 'exclude_dirs', 'unregistered', 'scheduled_times']:
        if list_field in config:
            if not isinstance(config[list_field], list):
                errors.append(f"'{list_field}' must be a list, got: {type(config[list_field]).__name__}")

    # Validate delete_files is a dict
    if 'delete_files' in config:
        if not isinstance(config['delete_files'], dict):
            errors.append(f"'delete_files' must be a dictionary, got: {type(config['delete_files']).__name__}")

    # Validate tracker_tags structure
    if 'tracker_tags' in config:
        if not isinstance(config['tracker_tags'], dict):
            errors.append(f"'tracker_tags' must be a dictionary, got: {type(config['tracker_tags']).__name__}")
        elif config['tracker_tags']:  # Only iterate if non-empty
            for tracker_name, tracker_config in config['tracker_tags'].items():
                if not isinstance(tracker_config, dict):
                    errors.append(f"tracker_tags['{tracker_name}'] must be a dictionary")
                    continue

                if 'tag' in tracker_config and not isinstance(tracker_config['tag'], str):
                    errors.append(f"tracker_tags['{tracker_name}']['tag'] must be a string")

                for limit_field in ['seed_time_limit', 'seed_ratio_limit']:
                    if limit_field in tracker_config:
                        value = tracker_config[limit_field]
                        # qBittorrent API: -2 = no limit, -1 = use global settings, >=0 = specific limit
                        if not isinstance(value, (int, float)) or (value < -2):
                            errors.append(f"tracker_tags['{tracker_name}']['{limit_field}'] must be >= -2 "
                                         f"(-2 = no limit, -1 = use global, 0+ = specific limit)")

    # Validate target_dir if present
    if 'target_dir' in config and config['target_dir']:
        target_dir = Path(config['target_dir'])
        # Don't validate existence, just format
        if not target_dir.is_absolute():
            logging.warning(f"target_dir should be an absolute path: {config['target_dir']}")

    # Validate scheduled_times format
    if 'scheduled_times' in config:
        # Type was already validated above in the list validation
        if isinstance(config['scheduled_times'], list) and config['scheduled_times']:
            for time_str in config['scheduled_times']:
                if not isinstance(time_str, str):
                    errors.append(f"scheduled_times must contain strings, got: {type(time_str).__name__}")
                    continue

                # Basic format check (HH:MM or HH:MM:SS)
                parts = time_str.split(':')
                if len(parts) not in [2, 3]:
                    errors.append(f"Invalid time format in scheduled_times: '{time_str}'. Expected 'HH:MM' or 'HH:MM:SS'")
                    continue

                try:
                    hour = int(parts[0])
                    minute = int(parts[1])
                    if len(parts) == 3:
                        second = int(parts[2])
                        if not (0 <= second <= 59):
                            errors.append(f"Invalid seconds in scheduled_times: '{time_str}'")

                    if not (0 <= hour <= 23):
                        errors.append(f"Invalid hour in scheduled_times: '{time_str}'")
                    if not (0 <= minute <= 59):
                        errors.append(f"Invalid minutes in scheduled_times: '{time_str}'")
                except ValueError:
                    errors.append(f"Invalid time format in scheduled_times: '{time_str}'")

    if errors:
        error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {error}" for error in errors)
        raise ConfigValidationError(error_msg)

    logging.info("Configuration validation passed")


def validate_exclude_patterns(exclude_files: List[str], exclude_dirs: List[str]) -> None:
    """
    Validate exclude patterns for potential issues.

    Args:
        exclude_files: List of file patterns to exclude
        exclude_dirs: List of directory paths to exclude
    """
    # Warn about potentially problematic patterns
    dangerous_file_patterns = ['*', '*.*', '**/*']
    for pattern in exclude_files:
        if pattern in dangerous_file_patterns:
            logging.warning(f"Exclude file pattern '{pattern}' will match ALL files - this may not be intended")

    # Check directory paths
    for dir_path in exclude_dirs:
        if not Path(dir_path).is_absolute():
            logging.warning(f"Exclude directory path should be absolute: '{dir_path}'")
