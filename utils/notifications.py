import logging
import json
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Any

try:
    import apprise

    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False


class NotificationManager:
    """
    Manages sending notifications via Apprise and Notifiarr.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.apprise_url = config.get("apprise_url")
        self.notifiarr_key = config.get("notifiarr_key")
        self.notifiarr_channel = config.get("notifiarr_channel")

        # Notifiarr requires both key and channel
        self._notifiarr_enabled = bool(self.notifiarr_key and self.notifiarr_channel)

        if self.notifiarr_key and not self.notifiarr_channel:
            logging.warning("Notifiarr API key configured without channel; Notifiarr notifications disabled.")
        if self.notifiarr_channel and not self.notifiarr_key:
            logging.warning("Notifiarr channel configured without API key; Notifiarr notifications disabled.")

        self.apprise_obj = None
        if self.apprise_url:
            if APPRISE_AVAILABLE:
                self.apprise_obj = apprise.Apprise()
                self.apprise_obj.add(self.apprise_url)
            else:
                logging.warning("Apprise URL configured but 'apprise' module not found. Install it with: pip install apprise")

    def send_summary(self, operation_results: Dict[str, List[str]]):
        """
        Generates a summary from operation results and sends notifications.
        """
        if not (self.apprise_obj or self._notifiarr_enabled):
            return

        # Build summary message
        succeeded = operation_results.get("succeeded", [])
        failed = operation_results.get("failed", [])

        if not succeeded and not failed:
            return  # Nothing to report

        title = "qbitunregistered Summary"
        body_lines = []

        if succeeded:
            body_lines.append(f"✅ Succeeded: {len(succeeded)}")
            for op in succeeded:
                body_lines.append(f"  - {op}")

        if failed:
            body_lines.append(f"❌ Failed: {len(failed)}")
            for op in failed:
                body_lines.append(f"  - {op}")

        body = "\n".join(body_lines)

        # Send Apprise notification
        if self.apprise_obj:
            self._send_apprise(title, body)

        # Send Notifiarr notification
        if self._notifiarr_enabled:
            self._send_notifiarr(title, body)

    def _send_apprise(self, title: str, body: str):
        """Sends notification via Apprise."""
        try:
            self.apprise_obj.notify(
                body=body,
                title=title,
            )
            logging.info("Sent Apprise notification")
        except Exception:
            logging.exception("Failed to send Apprise notification")

    def _send_notifiarr(self, title: str, body: str):
        """
        Sends notification via Notifiarr Passthrough API using urllib.
        """
        url = "https://notifiarr.com/api/v1/notification/passthrough"

        # Construct payload
        # Based on Notifiarr Passthrough API documentation
        payload = {
            "notification": {"update": False, "name": "qbitunregistered", "event": title},
            "discord": {
                "color": "3066993",  # Blue-ish
                "text": {"title": title, "content": body},
                "ids": {"channel": self.notifiarr_channel},
            },
        }

        headers = {"Content-Type": "application/json", "X-API-Key": self.notifiarr_key}

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            # Keep notification I/O bounded so the main script doesn't hang
            with urllib.request.urlopen(req, timeout=10) as response:
                if 200 <= response.status < 300:
                    logging.info("Sent Notifiarr notification")
                else:
                    logging.error(f"Notifiarr returned status code: {response.status}")

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass
            logging.error(f"Failed to send Notifiarr notification: HTTP {e.code} - {e.reason}. Details: {error_body}")
        except Exception:
            logging.exception("Failed to send Notifiarr notification")
