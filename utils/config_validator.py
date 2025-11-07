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
    required_fields = ["host", "username", "password"]
    for field in required_fields:
        if field not in config:
            errors.append(f"Missing required field: '{field}'")
        elif not isinstance(config[field], str):
            errors.append(f"Field '{field}' must be a string, got: {type(config[field]).__name__}")
        elif not config[field].strip():
            errors.append(f"Field '{field}' cannot be empty or whitespace-only")

    # Validate host format
    if "host" in config:
        host = config["host"]
        if host:
            # Determine if this is a full URL (contains "://") or simple hostname:port format
            if "://" in host:
                # Parse as full URL (e.g., 'https://example.com:8080/qbittorrent')
                parsed = urlparse(host)

                # Validate scheme
                if parsed.scheme not in ["http", "https"]:
                    errors.append(f"Invalid host scheme: '{parsed.scheme}'. Use 'http' or 'https'")

                # Validate netloc is present
                if not parsed.netloc:
                    errors.append(f"Invalid host URL: '{host}'. Missing hostname/netloc")
            else:
                # Treat as simple 'hostname:port' format (e.g., 'localhost:8080')
                if ":" not in host:
                    errors.append(
                        f"Invalid host format: '{host}'. Expected 'hostname:port' or full URL like 'http://hostname:port/path'"
                    )
                else:
                    # Validate simple 'hostname:port' format
                    parts = host.split(":", 1)
                    hostname = parts[0].strip()

                    # Check hostname is not empty
                    if not hostname:
                        errors.append(f"Invalid host format: '{host}'. Hostname cannot be empty")

                    # Validate port
                    try:
                        port = int(parts[1])
                        if not (1 <= port <= 65535):
                            errors.append(f"Invalid port number: {port}. Must be between 1 and 65535")
                    except ValueError:
                        errors.append(f"Invalid port in host: '{parts[1]}'. Must be a number")

    # Validate dry_run is boolean
    if "dry_run" in config and not isinstance(config["dry_run"], bool):
        errors.append(f"'dry_run' must be a boolean, got: {type(config['dry_run']).__name__}")

    # Validate log_level
    if "log_level" in config:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        log_level = config["log_level"]
        if not isinstance(log_level, str):
            errors.append(f"'log_level' must be a string, got: {type(log_level).__name__}")
        elif log_level.upper() not in valid_levels:
            errors.append(f"'log_level' must be one of {valid_levels}, got: '{log_level}'")

    # Validate log_file is a string if provided
    if "log_file" in config and config["log_file"]:
        if not isinstance(config["log_file"], str):
            errors.append(f"'log_file' must be a string path, got: {type(config['log_file']).__name__}")

    # Validate tags are strings
    for tag_field in ["default_unregistered_tag", "cross_seeding_tag", "other_issues_tag"]:
        if tag_field in config and not isinstance(config[tag_field], str):
            errors.append(f"'{tag_field}' must be a string, got: {type(config[tag_field]).__name__}")

    # Validate boolean flags
    for bool_field in [
        "use_delete_tags",
        "use_delete_files",
        "auto_tmm_enabled",
        "torrent_changed_tmm_enabled",
        "save_path_changed_tmm_enabled",
        "category_changed_tmm_enabled",
    ]:
        if bool_field in config and not isinstance(config[bool_field], bool):
            errors.append(f"'{bool_field}' must be a boolean, got: {type(config[bool_field]).__name__}")

    # Validate lists
    for list_field in ["delete_tags", "exclude_files", "exclude_dirs", "unregistered", "scheduled_times"]:
        if list_field in config:
            if not isinstance(config[list_field], list):
                errors.append(f"'{list_field}' must be a list, got: {type(config[list_field]).__name__}")

    # Validate delete_files is a dict
    if "delete_files" in config:
        if not isinstance(config["delete_files"], dict):
            errors.append(f"'delete_files' must be a dictionary, got: {type(config['delete_files']).__name__}")

    # Validate tracker_tags structure
    if "tracker_tags" in config:
        if not isinstance(config["tracker_tags"], dict):
            errors.append(f"'tracker_tags' must be a dictionary, got: {type(config['tracker_tags']).__name__}")
        elif config["tracker_tags"]:  # Only iterate if non-empty
            for tracker_name, tracker_config in config["tracker_tags"].items():
                if not isinstance(tracker_config, dict):
                    errors.append(f"tracker_tags['{tracker_name}'] must be a dictionary")
                    continue

                if "tag" in tracker_config and not isinstance(tracker_config["tag"], str):
                    errors.append(f"tracker_tags['{tracker_name}']['tag'] must be a string")

                # Validate seed limits with appropriate type checking
                # seed_time_limit: Integer only (minutes)
                # seed_ratio_limit: Integer or float (upload:download ratio)
                if "seed_time_limit" in tracker_config:
                    value = tracker_config["seed_time_limit"]
                    # qBittorrent API: -2 = use global, -1 = no limit, >=0 = specific minutes
                    # Upper bound: 1 year = 525,600 minutes (525600)
                    MAX_SEED_TIME_MINUTES = 525600  # 1 year
                    if not isinstance(value, int) or (value < -2):
                        errors.append(
                            f"tracker_tags['{tracker_name}']['seed_time_limit'] must be an integer >= -2 "
                            f"(-2 = use global, -1 = no limit, 0+ = minutes)"
                        )
                    elif value > MAX_SEED_TIME_MINUTES:
                        errors.append(
                            f"tracker_tags['{tracker_name}']['seed_time_limit'] exceeds maximum allowed "
                            f"value of {MAX_SEED_TIME_MINUTES} minutes (1 year). Got: {value}"
                        )
                    # Warn about potentially confusing edge case
                    elif value == 0:
                        logging.warning(
                            f"tracker_tags['{tracker_name}']['seed_time_limit'] is 0, which means "
                            "torrents will stop seeding immediately. Use -1 for unlimited seeding."
                        )

                if "seed_ratio_limit" in tracker_config:
                    value = tracker_config["seed_ratio_limit"]
                    # qBittorrent API: -2 = use global, -1 = no limit, >=0 = specific ratio
                    # Upper bound: 100.0 (seeding 100x the download size is exceptionally high)
                    MAX_SEED_RATIO = 100.0
                    if not isinstance(value, (int, float)) or (value < -2):
                        errors.append(
                            f"tracker_tags['{tracker_name}']['seed_ratio_limit'] must be a number >= -2 "
                            f"(-2 = use global, -1 = no limit, 0+ = ratio)"
                        )
                    elif value > MAX_SEED_RATIO:
                        errors.append(
                            f"tracker_tags['{tracker_name}']['seed_ratio_limit'] exceeds maximum allowed "
                            f"value of {MAX_SEED_RATIO}. Got: {value}"
                        )
                    # Warn about potentially confusing edge case
                    elif value == 0:
                        logging.warning(
                            f"tracker_tags['{tracker_name}']['seed_ratio_limit'] is 0, which means "
                            "torrents will stop seeding immediately. Use -1 for unlimited seeding."
                        )

    # Validate target_dir if present
    if "target_dir" in config and config["target_dir"]:
        target_dir = Path(config["target_dir"])
        # Don't validate existence, just format
        if not target_dir.is_absolute():
            logging.warning(f"target_dir should be an absolute path: {config['target_dir']}")

    # Validate scheduled_times format
    if "scheduled_times" in config:
        # Type was already validated above in the list validation
        if isinstance(config["scheduled_times"], list) and config["scheduled_times"]:
            for time_str in config["scheduled_times"]:
                if not isinstance(time_str, str):
                    errors.append(f"scheduled_times must contain strings, got: {type(time_str).__name__}")
                    continue

                # Basic format check (HH:MM or HH:MM:SS)
                parts = time_str.split(":")
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

    # Validate webhooks configuration
    if "webhooks" in config:
        if not isinstance(config["webhooks"], list):
            errors.append("'webhooks' must be a list")
        else:
            for idx, webhook in enumerate(config["webhooks"]):
                if not isinstance(webhook, dict):
                    errors.append(f"Webhook {idx} must be a dictionary")
                    continue

                # Required field: url
                if "url" not in webhook:
                    errors.append(f"Webhook {idx} missing required field 'url'")
                elif not isinstance(webhook["url"], str) or not webhook["url"].strip():
                    errors.append(f"Webhook {idx} 'url' must be a non-empty string")
                else:
                    # Basic URL validation
                    url = webhook["url"].strip()
                    if not url.startswith(("http://", "https://")):
                        errors.append(f"Webhook {idx} 'url' must start with http:// or https://")

                # Validate format field
                if "format" in webhook:
                    valid_formats = ["discord", "slack", "generic"]
                    if webhook["format"] not in valid_formats:
                        errors.append(f"Webhook {idx} invalid format: '{webhook['format']}'. Must be one of {valid_formats}")

                # Validate min_level field
                if "min_level" in webhook:
                    valid_levels = ["info", "warning", "error", "critical"]
                    min_level = webhook["min_level"]
                    if not isinstance(min_level, str):
                        errors.append(f"Webhook {idx} 'min_level' must be a string")
                    elif min_level.lower() not in valid_levels:
                        errors.append(f"Webhook {idx} invalid min_level: '{min_level}'. Must be one of {valid_levels}")

                # Validate enabled field
                if "enabled" in webhook and not isinstance(webhook["enabled"], bool):
                    errors.append(f"Webhook {idx} 'enabled' must be a boolean")

                # Validate numeric fields
                if "retry_attempts" in webhook:
                    value = webhook["retry_attempts"]
                    if not isinstance(value, int) or value < 0:
                        errors.append(f"Webhook {idx} 'retry_attempts' must be a non-negative integer")
                    elif value > 10:
                        logging.warning(
                            f"Webhook {idx} 'retry_attempts' is {value}, which is quite high. "
                            "Consider using a lower value to avoid long delays."
                        )

                if "retry_delay" in webhook:
                    value = webhook["retry_delay"]
                    if not isinstance(value, (int, float)) or value < 0:
                        errors.append(f"Webhook {idx} 'retry_delay' must be a non-negative number")
                    elif value > 60:
                        logging.warning(
                            f"Webhook {idx} 'retry_delay' is {value}s, which is quite long. " "Consider using a shorter delay."
                        )

                if "timeout" in webhook:
                    value = webhook["timeout"]
                    if not isinstance(value, (int, float)) or value <= 0:
                        errors.append(f"Webhook {idx} 'timeout' must be a positive number")
                    elif value > 300:
                        logging.warning(
                            f"Webhook {idx} 'timeout' is {value}s, which is very long. " "Consider using a shorter timeout."
                        )

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
    dangerous_file_patterns = ["*", "*.*", "**/*"]
    for pattern in exclude_files:
        if pattern in dangerous_file_patterns:
            logging.warning(f"Exclude file pattern '{pattern}' will match ALL files - this may not be intended")

    # Check directory paths - must be absolute for security
    for dir_path in exclude_dirs:
        if not Path(dir_path).is_absolute():
            raise ConfigValidationError(f"Exclude directory path must be absolute (security requirement): '{dir_path}'")
