"""Tests for webhook and event notification system."""

import json
import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError, URLError

from utils.events import (
    Event,
    EventLevel,
    EventType,
    EventTracker,
    get_event_tracker,
    reset_event_tracker,
)
from utils.webhooks import (
    WebhookConfig,
    WebhookDelivery,
    WebhookFormat,
    WebhookManager,
    get_webhook_manager,
    reset_webhook_manager,
)


class TestEvent:
    """Tests for Event class."""

    def test_event_creation(self):
        """Test creating an event."""
        event = Event(
            event_type=EventType.UNREGISTERED_FOUND, level=EventLevel.WARNING, message="Test message", details={"count": 5}
        )

        assert event.event_type == EventType.UNREGISTERED_FOUND
        assert event.level == EventLevel.WARNING
        assert event.message == "Test message"
        assert event.details["count"] == 5
        assert isinstance(event.timestamp, datetime)

    def test_event_to_dict(self):
        """Test converting event to dictionary."""
        event = Event(
            event_type=EventType.TORRENT_DELETED, level=EventLevel.ERROR, message="Deleted torrent", details={"hash": "abc123"}
        )

        event_dict = event.to_dict()

        assert event_dict["event_type"] == "torrent_deleted"
        assert event_dict["level"] == "error"
        assert event_dict["message"] == "Deleted torrent"
        assert event_dict["details"]["hash"] == "abc123"
        assert "timestamp" in event_dict


class TestEventTracker:
    """Tests for EventTracker class."""

    def setup_method(self):
        """Set up test fixtures."""
        reset_event_tracker()
        self.tracker = EventTracker()

    def test_track_event(self):
        """Test tracking an event."""
        self.tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Connected", {"host": "localhost"})

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.CONNECTION_ESTABLISHED
        assert event.level == EventLevel.INFO
        assert event.message == "Connected"
        assert event.details["host"] == "localhost"

    def test_start_operation(self):
        """Test starting an operation."""
        self.tracker.start_operation("test_operation")

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.OPERATION_STARTED
        assert "test_operation" in event.message
        assert event.details["operation"] == "test_operation"

    def test_complete_operation_success(self):
        """Test completing an operation successfully."""
        self.tracker.start_operation("test_operation")
        self.tracker.complete_operation("test_operation", success=True)

        assert len(self.tracker.events) == 2
        completion_event = self.tracker.events[1]
        assert completion_event.event_type == EventType.OPERATION_COMPLETED
        assert completion_event.level == EventLevel.INFO
        assert "duration_seconds" in completion_event.details

    def test_complete_operation_failure(self):
        """Test completing an operation with failure."""
        self.tracker.start_operation("test_operation")
        self.tracker.complete_operation("test_operation", success=False)

        assert len(self.tracker.events) == 2
        completion_event = self.tracker.events[1]
        assert completion_event.event_type == EventType.OPERATION_FAILED
        assert completion_event.level == EventLevel.ERROR

    def test_track_unregistered_found(self):
        """Test tracking unregistered torrents."""
        self.tracker.track_unregistered_found(10, "unregistered")

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.UNREGISTERED_FOUND
        assert event.level == EventLevel.WARNING
        assert event.details["count"] == 10
        assert event.details["tag"] == "unregistered"

    def test_track_torrents_deleted(self):
        """Test tracking torrent deletions."""
        disk_freed = 5 * 1024**3  # 5 GB
        self.tracker.track_torrents_deleted(3, disk_freed, ["unregistered"])

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.TORRENT_DELETED
        assert event.details["count"] == 3
        assert event.details["disk_freed_bytes"] == disk_freed
        assert "5.00 GB" in event.message

    def test_track_orphaned_files_found(self):
        """Test tracking orphaned files found."""
        total_size = 2 * 1024**3  # 2 GB
        self.tracker.track_orphaned_files_found(50, total_size)

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.ORPHANED_FILES_FOUND
        assert event.details["count"] == 50
        assert "2.00 GB" in event.message

    def test_track_orphaned_files_deleted(self):
        """Test tracking orphaned files deleted."""
        total_size = 1 * 1024**3  # 1 GB
        self.tracker.track_orphaned_files_deleted(20, total_size)

        assert len(self.tracker.events) == 1
        event = self.tracker.events[0]
        assert event.event_type == EventType.ORPHANED_FILES_DELETED
        assert event.level == EventLevel.INFO

    def test_get_events_by_level(self):
        """Test filtering events by level."""
        self.tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Info event")
        self.tracker.track(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Warning event")
        self.tracker.track(EventType.TORRENT_DELETED, EventLevel.ERROR, "Error event")

        # Get warnings and above
        high_priority = self.tracker.get_events_by_level(EventLevel.WARNING)
        assert len(high_priority) == 2
        assert all(e.level in (EventLevel.WARNING, EventLevel.ERROR, EventLevel.CRITICAL) for e in high_priority)

    def test_get_events_by_type(self):
        """Test filtering events by type."""
        self.tracker.track(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Unregistered 1")
        self.tracker.track(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Unregistered 2")
        self.tracker.track(EventType.TORRENT_DELETED, EventLevel.ERROR, "Deleted")

        unregistered_events = self.tracker.get_events_by_type(EventType.UNREGISTERED_FOUND)
        assert len(unregistered_events) == 2

    def test_get_summary(self):
        """Test getting event summary."""
        self.tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Connected")
        self.tracker.track(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Unregistered")
        self.tracker.track(EventType.TORRENT_DELETED, EventLevel.ERROR, "Deleted")

        summary = self.tracker.get_summary()

        assert summary["total_events"] == 3
        assert summary["events_by_level"]["info"] == 1
        assert summary["events_by_level"]["warning"] == 1
        assert summary["events_by_level"]["error"] == 1
        assert len(summary["latest_events"]) == 3

    def test_clear(self):
        """Test clearing events."""
        self.tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Test")
        assert len(self.tracker.events) > 0

        self.tracker.clear()
        assert len(self.tracker.events) == 0

    def test_global_tracker(self):
        """Test global tracker singleton."""
        tracker1 = get_event_tracker()
        tracker2 = get_event_tracker()

        assert tracker1 is tracker2

        reset_event_tracker()
        tracker3 = get_event_tracker()
        assert tracker3 is not tracker1


class TestWebhookConfig:
    """Tests for WebhookConfig."""

    def test_default_config(self):
        """Test default webhook configuration."""
        config = WebhookConfig(url="https://example.com/webhook")

        assert config.url == "https://example.com/webhook"
        assert config.format == WebhookFormat.GENERIC
        assert config.min_level == EventLevel.INFO
        assert config.enabled is True
        assert config.retry_attempts == 3

    def test_custom_config(self):
        """Test custom webhook configuration."""
        config = WebhookConfig(
            url="https://discord.com/webhook",
            format=WebhookFormat.DISCORD,
            min_level=EventLevel.WARNING,
            enabled=False,
            retry_attempts=5,
        )

        assert config.format == WebhookFormat.DISCORD
        assert config.min_level == EventLevel.WARNING
        assert config.enabled is False
        assert config.retry_attempts == 5


class TestWebhookDelivery:
    """Tests for WebhookDelivery class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = WebhookConfig(url="https://example.com/webhook", format=WebhookFormat.GENERIC)
        self.delivery = WebhookDelivery(self.config)

    def test_send_event_disabled(self):
        """Test that disabled webhooks don't send."""
        self.config.enabled = False
        event = Event(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Test")

        result = self.delivery.send_event(event)

        assert result is False

    def test_send_event_below_min_level(self):
        """Test that events below min level are not sent."""
        self.config.min_level = EventLevel.ERROR
        event = Event(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Test")

        result = self.delivery.send_event(event)

        assert result is False

    def test_format_generic(self):
        """Test generic payload formatting."""
        event = Event(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Test", details={"count": 5})

        payload = self.delivery._format_generic(event)

        assert payload["event_type"] == "unregistered_found"
        assert payload["level"] == "warning"
        assert payload["message"] == "Test"
        assert payload["details"]["count"] == 5

    def test_format_discord(self):
        """Test Discord payload formatting."""
        self.config.format = WebhookFormat.DISCORD
        self.delivery = WebhookDelivery(self.config)

        event = Event(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Test message", details={"count": 5})

        payload = self.delivery._format_discord(event)

        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "⚠️" in embed["title"]
        assert embed["description"] == "Test message"
        assert embed["color"] == 0xF39C12  # Orange for warning

    def test_format_slack(self):
        """Test Slack payload formatting."""
        self.config.format = WebhookFormat.SLACK
        self.delivery = WebhookDelivery(self.config)

        event = Event(EventType.TORRENT_DELETED, EventLevel.ERROR, "Deleted", details={"count": 3})

        payload = self.delivery._format_slack(event)

        assert "attachments" in payload
        assert len(payload["attachments"]) == 1
        attachment = payload["attachments"][0]
        assert attachment["color"] == "#e74c3c"  # Red for error
        assert ":x:" in attachment["title"]

    @patch("utils.webhooks.urlopen")
    def test_send_with_retry_success(self, mock_urlopen):
        """Test successful webhook delivery."""
        # Mock successful HTTP response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        payload = {"test": "data"}
        result = self.delivery._send_with_retry(payload)

        assert result is True
        assert mock_urlopen.called

    @patch("utils.webhooks.urlopen")
    def test_send_with_retry_http_error(self, mock_urlopen):
        """Test webhook delivery with HTTP error."""
        # Mock HTTP 500 error
        mock_urlopen.side_effect = HTTPError(url="test", code=500, msg="Server Error", hdrs={}, fp=None)

        self.config.retry_attempts = 2
        self.config.retry_delay = 0.1
        payload = {"test": "data"}

        result = self.delivery._send_with_retry(payload)

        assert result is False
        assert mock_urlopen.call_count == 2  # Retried

    @patch("utils.webhooks.urlopen")
    def test_send_with_retry_client_error_no_retry(self, mock_urlopen):
        """Test that client errors (4xx) are not retried."""
        # Mock HTTP 404 error
        mock_urlopen.side_effect = HTTPError(url="test", code=404, msg="Not Found", hdrs={}, fp=None)

        self.config.retry_attempts = 3
        payload = {"test": "data"}

        result = self.delivery._send_with_retry(payload)

        assert result is False
        assert mock_urlopen.call_count == 1  # Not retried

    @patch("utils.webhooks.urlopen")
    def test_send_with_retry_url_error(self, mock_urlopen):
        """Test webhook delivery with URL error."""
        mock_urlopen.side_effect = URLError("Connection refused")

        self.config.retry_attempts = 2
        self.config.retry_delay = 0.1
        payload = {"test": "data"}

        result = self.delivery._send_with_retry(payload)

        assert result is False


class TestWebhookManager:
    """Tests for WebhookManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        reset_webhook_manager()
        self.manager = WebhookManager()

    def test_add_webhook(self):
        """Test adding a webhook."""
        config = WebhookConfig(url="https://example.com/webhook")
        self.manager.add_webhook(config)

        assert len(self.manager.webhooks) == 1

    def test_configure_from_dict_valid(self):
        """Test configuring webhooks from dictionary."""
        config_dict = {
            "webhooks": [
                {"url": "https://discord.com/webhook", "format": "discord", "min_level": "warning"},
                {"url": "https://slack.com/webhook", "format": "slack", "enabled": False},
            ]
        }

        self.manager.configure_from_dict(config_dict)

        assert len(self.manager.webhooks) == 2
        assert self.manager.webhooks[0].config.format == WebhookFormat.DISCORD
        assert self.manager.webhooks[1].config.enabled is False

    def test_configure_from_dict_invalid_format(self):
        """Test configuration with invalid format falls back to generic."""
        config_dict = {"webhooks": [{"url": "https://example.com/webhook", "format": "invalid"}]}

        self.manager.configure_from_dict(config_dict)

        assert len(self.manager.webhooks) == 1
        assert self.manager.webhooks[0].config.format == WebhookFormat.GENERIC

    def test_configure_from_dict_missing_url(self):
        """Test configuration with missing URL is skipped."""
        config_dict = {"webhooks": [{"format": "discord"}]}  # Missing URL

        self.manager.configure_from_dict(config_dict)

        assert len(self.manager.webhooks) == 0

    def test_configure_from_dict_not_list(self):
        """Test configuration when webhooks is not a list."""
        config_dict = {"webhooks": "not_a_list"}

        self.manager.configure_from_dict(config_dict)

        assert len(self.manager.webhooks) == 0

    @patch.object(WebhookDelivery, "send_event")
    def test_send_event(self, mock_send):
        """Test sending event to all webhooks."""
        mock_send.return_value = True

        config1 = WebhookConfig(url="https://example.com/webhook1")
        config2 = WebhookConfig(url="https://example.com/webhook2")
        self.manager.add_webhook(config1)
        self.manager.add_webhook(config2)

        event = Event(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Test")
        self.manager.send_event(event)

        assert mock_send.call_count == 2

    @patch.object(WebhookDelivery, "send_summary")
    def test_send_summary(self, mock_send):
        """Test sending summary to all webhooks."""
        mock_send.return_value = True

        config = WebhookConfig(url="https://example.com/webhook")
        self.manager.add_webhook(config)

        tracker = EventTracker()
        tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Test")

        self.manager.send_summary(tracker)

        assert mock_send.called

    def test_global_manager(self):
        """Test global webhook manager singleton."""
        manager1 = get_webhook_manager()
        manager2 = get_webhook_manager()

        assert manager1 is manager2

        reset_webhook_manager()
        manager3 = get_webhook_manager()
        assert manager3 is not manager1


@pytest.mark.integration
class TestWebhookIntegration:
    """Integration tests for webhook system."""

    def setup_method(self):
        """Set up test fixtures."""
        reset_event_tracker()
        reset_webhook_manager()

    def test_full_workflow(self):
        """Test complete workflow from event tracking to webhook delivery."""
        # Set up event tracker
        tracker = get_event_tracker()

        # Set up webhook manager
        manager = get_webhook_manager()
        config_dict = {"webhooks": [{"url": "https://example.com/webhook", "format": "generic", "min_level": "warning"}]}
        manager.configure_from_dict(config_dict)

        # Track some events
        tracker.track(EventType.CONNECTION_ESTABLISHED, EventLevel.INFO, "Connected")
        tracker.track(EventType.UNREGISTERED_FOUND, EventLevel.WARNING, "Found 5 unregistered", {"count": 5})
        tracker.track(EventType.TORRENT_DELETED, EventLevel.ERROR, "Deleted 2 torrents", {"count": 2})

        # Get summary
        summary = tracker.get_summary()

        assert summary["total_events"] == 3
        assert summary["events_by_level"]["warning"] == 1
        assert summary["events_by_level"]["error"] == 1

    @patch("utils.webhooks.urlopen")
    def test_discord_webhook_formatting(self, mock_urlopen):
        """Test full Discord webhook formatting and delivery."""
        # Mock successful response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        # Set up webhook
        config = WebhookConfig(url="https://discord.com/api/webhooks/test", format=WebhookFormat.DISCORD)
        delivery = WebhookDelivery(config)

        # Send event
        event = Event(
            EventType.UNREGISTERED_FOUND,
            EventLevel.WARNING,
            "Found 10 unregistered torrents",
            details={"count": 10, "tag": "unregistered"},
        )

        result = delivery.send_event(event)

        assert result is True
        assert mock_urlopen.called

        # Verify payload structure
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        payload = json.loads(request.data.decode())

        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["color"] == 0xF39C12  # Warning color
