"""Tests for notification manager."""

import sys
import pytest
from unittest.mock import MagicMock, patch

# Mock apprise module if not installed
if "apprise" not in sys.modules:
    sys.modules["apprise"] = MagicMock()

from utils.notifications import NotificationManager


class TestNotificationManager:
    """Test NotificationManager functionality."""

    @pytest.fixture
    def mock_apprise(self):
        # Since we mocked the module, we can just return the mock class
        mock_instance = sys.modules["apprise"].Apprise.return_value
        mock_instance.reset_mock()
        return mock_instance

    @pytest.fixture
    def mock_urlopen(self):
        with patch("urllib.request.urlopen") as mock:
            yield mock

    def test_init_apprise(self, mock_apprise):
        """Test initialization with Apprise URL."""
        config = {"apprise_url": "discord://webhook"}
        manager = NotificationManager(config)

        assert manager.apprise_obj is not None
        manager.apprise_obj.add.assert_called_with("discord://webhook")

    def test_init_no_apprise(self):
        """Test initialization without Apprise URL."""
        config = {}
        manager = NotificationManager(config)

        assert manager.apprise_obj is None

    def test_send_summary_apprise(self, mock_apprise):
        """Test sending summary via Apprise."""
        config = {"apprise_url": "discord://webhook"}
        manager = NotificationManager(config)

        operation_results = {"succeeded": ["Op 1", "Op 2"], "failed": []}

        manager.send_summary(operation_results)

        manager.apprise_obj.notify.assert_called_once()
        call_args = manager.apprise_obj.notify.call_args[1]
        assert "qbitunregistered Summary" in call_args["title"]
        assert "✅ Succeeded: 2" in call_args["body"]
        assert "Op 1" in call_args["body"]

    def test_send_summary_notifiarr(self, mock_urlopen):
        """Test sending summary via Notifiarr."""
        config = {"notifiarr_key": "test_key", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        operation_results = {"succeeded": [], "failed": ["Op Failed"]}

        # Setup mock response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        manager.send_summary(operation_results)

        assert mock_urlopen.called
        # Verify payload
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.full_url == "https://notifiarr.com/api/v1/notification/passthrough"
        assert req.headers["X-api-key"] == "test_key"

        import json

        data = json.loads(req.data)
        assert data["discord"]["ids"]["channel"] == "12345"
        assert "❌ Failed: 1" in data["discord"]["text"]["content"]

    def test_send_summary_no_config(self):
        """Test sending summary with no config."""
        config = {}
        manager = NotificationManager(config)

        operation_results = {"succeeded": ["Op 1"], "failed": []}

        # Should not raise error
        manager.send_summary(operation_results)

    def test_send_summary_empty_results(self, mock_apprise):
        """Test sending summary with empty results."""
        config = {"apprise_url": "discord://webhook"}
        manager = NotificationManager(config)

        operation_results = {"succeeded": [], "failed": []}

        manager.send_summary(operation_results)

        # Should not notify
        manager.apprise_obj.notify.assert_not_called()

    def test_apprise_notification_failure_with_retry(self, mock_apprise):
        """Test Apprise notification failure triggers retry logic."""
        config = {"apprise_url": "discord://webhook"}
        manager = NotificationManager(config)

        # Make notify raise an exception
        mock_apprise.notify.side_effect = Exception("Network error")

        operation_results = {"succeeded": ["Op 1"], "failed": []}

        # Should not raise exception (errors are caught and logged)
        with patch("time.sleep"):  # Speed up test by mocking sleep
            manager.send_summary(operation_results)

        # Verify retry attempts (should be called 3 times with max_retries=3)
        assert mock_apprise.notify.call_count == 3

    def test_notifiarr_notification_failure_with_retry(self, mock_urlopen):
        """Test Notifiarr notification failure triggers retry logic."""
        config = {"notifiarr_key": "test_key", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        # Make urlopen raise an exception
        mock_urlopen.side_effect = Exception("Connection error")

        operation_results = {"succeeded": ["Op 1"], "failed": []}

        # Should not raise exception (errors are caught and logged)
        with patch("time.sleep"):  # Speed up test by mocking sleep
            manager.send_summary(operation_results)

        # Verify retry attempts (should be called 3 times with max_retries=3)
        assert mock_urlopen.call_count == 3

    def test_discord_character_limit_truncation(self, mock_urlopen):
        """Test that long messages are truncated to Discord's 2000 char limit."""
        config = {"notifiarr_key": "test_key", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        # Create operation results with very long operation names
        long_operations = [f"Operation {i}: " + "x" * 100 for i in range(50)]
        operation_results = {"succeeded": long_operations, "failed": []}

        # Setup mock response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        manager.send_summary(operation_results)

        # Verify message was truncated
        call_args = mock_urlopen.call_args
        req = call_args[0][0]

        import json

        data = json.loads(req.data)
        content = data["discord"]["text"]["content"]

        # Should be truncated to 2000 chars
        assert len(content) <= 2000
        # Should contain truncation note
        assert "message truncated due to length" in content

    def test_conditional_notification_colors(self, mock_urlopen):
        """Test that notification colors change based on success/failure."""
        config = {"notifiarr_key": "test_key", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        # Setup mock response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        import json

        # Test success (green color)
        operation_results = {"succeeded": ["Op 1"], "failed": []}
        manager.send_summary(operation_results)

        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data)
        assert data["discord"]["color"] == "5025616"  # Green

        mock_urlopen.reset_mock()

        # Test failure (red color)
        operation_results = {"succeeded": ["Op 1"], "failed": ["Op 2"]}
        manager.send_summary(operation_results)

        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data)
        assert data["discord"]["color"] == "15158332"  # Red

    def test_notifiarr_http_error_handling(self, mock_urlopen):
        """Test proper handling of HTTP errors from Notifiarr."""
        import urllib.error

        config = {"notifiarr_key": "test_key", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        # Simulate HTTP 401 error
        mock_error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        mock_error.read = MagicMock(return_value=b'{"error": "Invalid API key"}')
        mock_urlopen.side_effect = mock_error

        operation_results = {"succeeded": ["Op 1"], "failed": []}

        # Should not raise exception (errors are caught and logged)
        with patch("time.sleep"):  # Speed up test
            manager.send_summary(operation_results)

        # Verify it tried to send (with retries)
        assert mock_urlopen.call_count == 3

    def test_notifiarr_credential_sanitization(self, mock_urlopen):
        """Test that API keys are sanitized in error messages."""
        import urllib.error

        config = {"notifiarr_key": "secret_key_12345", "notifiarr_channel": "12345"}
        manager = NotificationManager(config)

        # Simulate HTTP error with API key in response
        mock_error = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        error_body = b'{"error": "Invalid key: secret_key_12345"}'
        mock_error.read = MagicMock(return_value=error_body)
        mock_urlopen.side_effect = mock_error

        operation_results = {"succeeded": ["Op 1"], "failed": []}

        # Should not raise exception and should sanitize the key
        with patch("time.sleep"), patch("logging.exception") as mock_log:
            manager.send_summary(operation_results)

            # Verify credential was sanitized
            assert mock_log.called
            log_message = mock_log.call_args[0][0]
            assert "secret_key_12345" not in log_message or "***REDACTED***" in log_message
