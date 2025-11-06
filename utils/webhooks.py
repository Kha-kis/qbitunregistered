"""Webhook notification system for qbitunregistered events."""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from utils.events import Event, EventLevel, EventTracker

logger = logging.getLogger(__name__)


class WebhookFormat(Enum):
    """Supported webhook formats."""

    DISCORD = "discord"
    SLACK = "slack"
    GENERIC = "generic"


@dataclass
class WebhookConfig:
    """Configuration for a webhook endpoint."""

    url: str
    format: WebhookFormat = WebhookFormat.GENERIC
    min_level: EventLevel = EventLevel.INFO
    enabled: bool = True
    retry_attempts: int = 3
    retry_delay: float = 1.0  # seconds
    timeout: float = 10.0  # seconds


class WebhookDelivery:
    """Handles delivery of events to webhook endpoints."""

    def __init__(self, config: WebhookConfig):
        """
        Initialize webhook delivery.

        Args:
            config: Webhook configuration
        """
        self.config = config

    def send_event(self, event: Event) -> bool:
        """
        Send a single event to the webhook.

        Args:
            event: Event to send

        Returns:
            True if delivery succeeded, False otherwise
        """
        if not self.config.enabled:
            logger.debug(f"Webhook disabled, skipping event: {event.event_type.value}")
            return False

        # Check event level
        level_order = {EventLevel.INFO: 0, EventLevel.WARNING: 1, EventLevel.ERROR: 2, EventLevel.CRITICAL: 3}
        if level_order[event.level] < level_order[self.config.min_level]:
            logger.debug(f"Event level {event.level.value} below min level {self.config.min_level.value}, skipping")
            return False

        # Format payload based on webhook type
        payload = self._format_payload(event)

        # Send with retry logic
        return self._send_with_retry(payload)

    def send_summary(self, event_tracker: EventTracker) -> bool:
        """
        Send a summary of all events to the webhook.

        Args:
            event_tracker: Event tracker with collected events

        Returns:
            True if delivery succeeded, False otherwise
        """
        if not self.config.enabled:
            return False

        summary = event_tracker.get_summary()
        payload = self._format_summary_payload(summary)

        return self._send_with_retry(payload)

    def _format_payload(self, event: Event) -> Dict[str, Any]:
        """
        Format event as payload for webhook.

        Args:
            event: Event to format

        Returns:
            Formatted payload dictionary
        """
        if self.config.format == WebhookFormat.DISCORD:
            return self._format_discord(event)
        elif self.config.format == WebhookFormat.SLACK:
            return self._format_slack(event)
        else:
            return self._format_generic(event)

    def _format_summary_payload(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """
        Format summary as payload for webhook.

        Args:
            summary: Summary dictionary from EventTracker

        Returns:
            Formatted payload dictionary
        """
        if self.config.format == WebhookFormat.DISCORD:
            return self._format_discord_summary(summary)
        elif self.config.format == WebhookFormat.SLACK:
            return self._format_slack_summary(summary)
        else:
            return {"type": "summary", "data": summary}

    def _format_discord(self, event: Event) -> Dict[str, Any]:
        """Format event for Discord webhook."""
        # Map event level to Discord color
        color_map = {
            EventLevel.INFO: 0x3498DB,  # Blue
            EventLevel.WARNING: 0xF39C12,  # Orange
            EventLevel.ERROR: 0xE74C3C,  # Red
            EventLevel.CRITICAL: 0x992D22,  # Dark red
        }

        # Map event level to emoji
        emoji_map = {
            EventLevel.INFO: "ℹ️",
            EventLevel.WARNING: "⚠️",
            EventLevel.ERROR: "❌",
            EventLevel.CRITICAL: "🚨",
        }

        fields = []
        for key, value in event.details.items():
            fields.append({"name": key.replace("_", " ").title(), "value": str(value), "inline": True})

        embed = {
            "title": f"{emoji_map[event.level]} {event.event_type.value.replace('_', ' ').title()}",
            "description": event.message,
            "color": color_map[event.level],
            "timestamp": event.timestamp.isoformat(),
            "fields": fields,
        }

        return {"embeds": [embed]}

    def _format_slack(self, event: Event) -> Dict[str, Any]:
        """Format event for Slack webhook."""
        # Map event level to Slack color
        color_map = {
            EventLevel.INFO: "#3498db",  # Blue
            EventLevel.WARNING: "#f39c12",  # Orange
            EventLevel.ERROR: "#e74c3c",  # Red
            EventLevel.CRITICAL: "#992d22",  # Dark red
        }

        # Map event level to emoji
        emoji_map = {
            EventLevel.INFO: ":information_source:",
            EventLevel.WARNING: ":warning:",
            EventLevel.ERROR: ":x:",
            EventLevel.CRITICAL: ":rotating_light:",
        }

        fields = []
        for key, value in event.details.items():
            fields.append({"title": key.replace("_", " ").title(), "value": str(value), "short": True})

        attachment = {
            "color": color_map[event.level],
            "title": f"{emoji_map[event.level]} {event.event_type.value.replace('_', ' ').title()}",
            "text": event.message,
            "ts": int(event.timestamp.timestamp()),
            "fields": fields,
        }

        return {"attachments": [attachment]}

    def _format_generic(self, event: Event) -> Dict[str, Any]:
        """Format event as generic JSON payload."""
        return event.to_dict()

    def _format_discord_summary(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Format summary for Discord webhook."""
        # Calculate total issues
        events_by_level = summary["events_by_level"]
        total_issues = events_by_level["warning"] + events_by_level["error"] + events_by_level["critical"]

        # Choose color based on issues
        if events_by_level["critical"] > 0 or events_by_level["error"] > 0:
            color = 0xE74C3C  # Red
        elif events_by_level["warning"] > 0:
            color = 0xF39C12  # Orange
        else:
            color = 0x2ECC71  # Green

        # Build fields
        fields = [
            {"name": "Total Events", "value": str(summary["total_events"]), "inline": True},
            {"name": "Issues Found", "value": str(total_issues), "inline": True},
            {"name": "Info", "value": str(events_by_level["info"]), "inline": True},
            {"name": "Warnings", "value": str(events_by_level["warning"]), "inline": True},
            {"name": "Errors", "value": str(events_by_level["error"]), "inline": True},
            {"name": "Critical", "value": str(events_by_level["critical"]), "inline": True},
        ]

        embed = {
            "title": "🔔 qbitunregistered Operation Summary",
            "description": "Summary of operations completed",
            "color": color,
            "fields": fields,
        }

        return {"embeds": [embed]}

    def _format_slack_summary(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Format summary for Slack webhook."""
        events_by_level = summary["events_by_level"]
        total_issues = events_by_level["warning"] + events_by_level["error"] + events_by_level["critical"]

        # Choose color based on issues
        if events_by_level["critical"] > 0 or events_by_level["error"] > 0:
            color = "#e74c3c"  # Red
        elif events_by_level["warning"] > 0:
            color = "#f39c12"  # Orange
        else:
            color = "#2ecc71"  # Green

        fields = [
            {"title": "Total Events", "value": str(summary["total_events"]), "short": True},
            {"title": "Issues Found", "value": str(total_issues), "short": True},
            {"title": "Info", "value": str(events_by_level["info"]), "short": True},
            {"title": "Warnings", "value": str(events_by_level["warning"]), "short": True},
            {"title": "Errors", "value": str(events_by_level["error"]), "short": True},
            {"title": "Critical", "value": str(events_by_level["critical"]), "short": True},
        ]

        attachment = {
            "color": color,
            "title": ":bell: qbitunregistered Operation Summary",
            "text": "Summary of operations completed",
            "fields": fields,
        }

        return {"attachments": [attachment]}

    def _send_with_retry(self, payload: Dict[str, Any]) -> bool:
        """
        Send payload with retry logic.

        Args:
            payload: Payload to send

        Returns:
            True if successful, False otherwise
        """
        for attempt in range(self.config.retry_attempts):
            try:
                data = json.dumps(payload).encode("utf-8")

                request = Request(self.config.url, data=data, headers={"Content-Type": "application/json"}, method="POST")

                with urlopen(request, timeout=self.config.timeout) as response:
                    if response.status in (200, 201, 204):
                        logger.debug(f"Webhook delivered successfully to {self.config.url}")
                        return True
                    else:
                        logger.warning(f"Webhook returned unexpected status {response.status}: {response.read().decode()}")

            except HTTPError as e:
                logger.warning(f"Webhook HTTP error (attempt {attempt + 1}/{self.config.retry_attempts}): {e}")
                if e.code in (400, 401, 403, 404):  # Don't retry client errors
                    return False

            except URLError as e:
                logger.warning(f"Webhook URL error (attempt {attempt + 1}/{self.config.retry_attempts}): {e}")

            except Exception as e:
                logger.error(f"Webhook error (attempt {attempt + 1}/{self.config.retry_attempts}): {e}")

            # Wait before retry (exponential backoff)
            if attempt < self.config.retry_attempts - 1:
                delay = self.config.retry_delay * (2**attempt)
                time.sleep(delay)

        logger.error(f"Failed to deliver webhook to {self.config.url} after {self.config.retry_attempts} attempts")
        return False


class WebhookManager:
    """Manages multiple webhook endpoints."""

    def __init__(self):
        """Initialize webhook manager."""
        self.webhooks: List[WebhookDelivery] = []

    def add_webhook(self, config: WebhookConfig):
        """
        Add a webhook endpoint.

        Args:
            config: Webhook configuration
        """
        webhook = WebhookDelivery(config)
        self.webhooks.append(webhook)
        logger.info(f"Added webhook: {config.format.value} endpoint")

    def configure_from_dict(self, config: Dict[str, Any]):
        """
        Configure webhooks from configuration dictionary.

        Args:
            config: Configuration dictionary with 'webhooks' key
        """
        webhooks_config = config.get("webhooks", [])

        if not isinstance(webhooks_config, list):
            logger.warning("'webhooks' configuration must be a list")
            return

        for webhook_cfg in webhooks_config:
            try:
                # Parse format
                format_str = webhook_cfg.get("format", "generic").lower()
                try:
                    webhook_format = WebhookFormat(format_str)
                except ValueError:
                    logger.warning(f"Invalid webhook format: {format_str}, using generic")
                    webhook_format = WebhookFormat.GENERIC

                # Parse min level
                level_str = webhook_cfg.get("min_level", "info").upper()
                try:
                    min_level = EventLevel[level_str]
                except KeyError:
                    logger.warning(f"Invalid webhook min_level: {level_str}, using INFO")
                    min_level = EventLevel.INFO

                webhook_config = WebhookConfig(
                    url=webhook_cfg["url"],
                    format=webhook_format,
                    min_level=min_level,
                    enabled=webhook_cfg.get("enabled", True),
                    retry_attempts=webhook_cfg.get("retry_attempts", 3),
                    retry_delay=webhook_cfg.get("retry_delay", 1.0),
                    timeout=webhook_cfg.get("timeout", 10.0),
                )

                self.add_webhook(webhook_config)

            except KeyError as e:
                logger.error(f"Missing required webhook configuration field: {e}")
            except Exception as e:
                logger.error(f"Error configuring webhook: {e}")

    def send_event(self, event: Event):
        """
        Send event to all configured webhooks.

        Args:
            event: Event to send
        """
        if not self.webhooks:
            return

        for webhook in self.webhooks:
            try:
                webhook.send_event(event)
            except Exception as e:
                logger.error(f"Error sending event to webhook: {e}")

    def send_summary(self, event_tracker: EventTracker):
        """
        Send summary to all configured webhooks.

        Args:
            event_tracker: Event tracker with collected events
        """
        if not self.webhooks:
            return

        for webhook in self.webhooks:
            try:
                webhook.send_summary(event_tracker)
            except Exception as e:
                logger.error(f"Error sending summary to webhook: {e}")


# Global webhook manager instance
_global_manager: Optional[WebhookManager] = None


def get_webhook_manager() -> WebhookManager:
    """
    Get the global webhook manager instance.

    Returns:
        Global WebhookManager instance
    """
    global _global_manager
    if _global_manager is None:
        _global_manager = WebhookManager()
    return _global_manager


def reset_webhook_manager():
    """Reset the global webhook manager (useful for testing)."""
    global _global_manager
    _global_manager = None
