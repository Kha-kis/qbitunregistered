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
        
        operation_results = {
            "succeeded": ["Op 1", "Op 2"],
            "failed": []
        }
        
        manager.send_summary(operation_results)
        
        manager.apprise_obj.notify.assert_called_once()
        call_args = manager.apprise_obj.notify.call_args[1]
        assert "qbitunregistered Summary" in call_args["title"]
        assert "✅ Succeeded: 2" in call_args["body"]
        assert "Op 1" in call_args["body"]

    def test_send_summary_notifiarr(self, mock_urlopen):
        """Test sending summary via Notifiarr."""
        config = {
            "notifiarr_key": "test_key",
            "notifiarr_channel": "12345"
        }
        manager = NotificationManager(config)
        
        operation_results = {
            "succeeded": [],
            "failed": ["Op Failed"]
        }
        
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
