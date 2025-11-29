import logging
import json
import urllib.request
import urllib.error
import time
from typing import Dict, List, Optional, Any, Callable

try:
    import apprise

    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False


class NotificationManager:
    """
    Manages sending notifications via Apprise and Notifiarr.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
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

    def _retry_with_backoff(
        self,
        func: Callable[[], None],
        max_retries: int = 3,
        initial_delay: float = 1.0,
        *,
        reraise: bool = False,
    ) -> bool:
        """
        Retry a function with exponential backoff.

        Args:
            func: Function to retry (should raise exception on failure)
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay in seconds (doubles after each retry)

        Returns:
            True if function succeeded, False otherwise.
            If reraise is True, the last exception will be raised after all retries fail.
        """
        delay = initial_delay
        last_exc: Optional[BaseException] = None

        for attempt in range(max_retries):
            try:
                func()
                return True
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    logging.warning(f"Notification attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    logging.exception(f"Notification failed after {max_retries} attempts")

        if reraise and last_exc is not None:
            raise last_exc

        return False

    def send_summary(self, operation_results: Dict[str, List[str]]) -> None:
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
            self._send_notifiarr(title, body, has_failures=bool(failed))

    def _send_apprise(self, title: str, body: str) -> None:
        """Sends notification via Apprise with retry logic."""
        # Type assertion for type checker
        assert self.apprise_obj is not None, "apprise_obj should be initialized"

        def send():
            assert self.apprise_obj is not None  # For type checker in closure
            result = self.apprise_obj.notify(body=body, title=title)
            if result is False:
                raise RuntimeError("Apprise notify returned False")

        if self._retry_with_backoff(send):
            logging.info("Sent Apprise notification")
        else:
            logging.error("Failed to send Apprise notification after all retries")

    def _send_notifiarr(self, title: str, body: str, has_failures: bool = False) -> None:
        """
        Sends notification via Notifiarr Passthrough API using urllib with retry logic.

        Args:
            title: Notification title
            body: Notification body
            has_failures: Whether any operations failed (affects color)
        """
        # Type assertions for type checker
        assert self.notifiarr_key is not None, "notifiarr_key should be set when _notifiarr_enabled is True"
        assert self.notifiarr_channel is not None, "notifiarr_channel should be set when _notifiarr_enabled is True"

        url = "https://notifiarr.com/api/v1/notification/passthrough"

        # Choose color based on success/failure
        # Green (5025616) for success, Red (15158332) for failures
        color = "15158332" if has_failures else "5025616"

        # Discord has a 2000 character limit for message content
        # Truncate if necessary and add a note
        DISCORD_CHAR_LIMIT = 2000
        truncation_note = "\n\n... (message truncated due to length)"

        if len(body) > DISCORD_CHAR_LIMIT:
            # Reserve space for truncation note
            available_space = DISCORD_CHAR_LIMIT - len(truncation_note)
            body = body[:available_space] + truncation_note
            logging.warning(f"Notification body truncated to {DISCORD_CHAR_LIMIT} characters for Discord")

        # Construct payload
        # Based on Notifiarr Passthrough API documentation
        payload = {
            "notification": {"update": False, "name": "qbitunregistered", "event": title},
            "discord": {
                "color": color,
                "text": {"title": title, "content": body},
                "ids": {"channel": self.notifiarr_channel},
            },
        }

        headers = {"Content-Type": "application/json", "X-API-Key": self.notifiarr_key}

        def send():
            """Inner function for retry logic."""
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            # Keep notification I/O bounded so the main script doesn't hang
            with urllib.request.urlopen(req, timeout=10) as response:
                if 200 <= response.status < 300:
                    return  # Success
                else:
                    # Raise exception to trigger retry
                    raise Exception(f"Notifiarr returned status code: {response.status}")

        try:
            if self._retry_with_backoff(send, reraise=True):
                logging.info("Sent Notifiarr notification")
            else:
                logging.error("Failed to send Notifiarr notification after all retries")
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
                # Sanitize error body to prevent credential exposure
                if self.notifiarr_key and self.notifiarr_key in error_body:
                    error_body = error_body.replace(self.notifiarr_key, "***REDACTED***")
            except Exception:
                # If we can't read or sanitize the body, fall back to generic logging below.
                pass
            logging.exception(f"Failed to send Notifiarr notification: HTTP {e.code} - {e.reason}. Details: {error_body}")
        except Exception as e:
            # Generic connection or other unexpected errors should be logged but not crash the caller.
            logging.exception(f"Failed to send Notifiarr notification: {e}")
