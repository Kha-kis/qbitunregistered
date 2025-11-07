"""Event system for tracking and reporting qbitunregistered operations."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventLevel(Enum):
    """Event severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventType(Enum):
    """Types of events that can occur during operations."""

    # Torrent events
    UNREGISTERED_FOUND = "unregistered_found"
    TORRENT_DELETED = "torrent_deleted"
    TORRENT_TAGGED = "torrent_tagged"
    TORRENT_PAUSED = "torrent_paused"
    TORRENT_RESUMED = "torrent_resumed"

    # File events
    ORPHANED_FILES_FOUND = "orphaned_files_found"
    ORPHANED_FILES_DELETED = "orphaned_files_deleted"

    # Operation events
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    OPERATION_FAILED = "operation_failed"

    # System events
    CONNECTION_ESTABLISHED = "connection_established"
    CONNECTION_FAILED = "connection_failed"
    LARGE_OPERATION_WARNING = "large_operation_warning"


@dataclass
class Event:
    """Represents a single event in the application."""

    event_type: EventType
    level: EventLevel
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "event_type": self.event_type.value,
            "level": self.level.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }


class EventTracker:
    """Tracks events during qbitunregistered operations."""

    def __init__(self, max_events: int = 10000):
        """
        Initialize the event tracker.

        Args:
            max_events: Maximum number of events to store (prevents memory issues in long-running tasks).
                       Older events are dropped when limit is reached. Default: 10000
        """
        self.events: List[Event] = []
        self.max_events = max_events
        self._operation_start_time: Optional[datetime] = None
        self._dropped_events_count: int = 0

    def track(self, event_type: EventType, level: EventLevel, message: str, details: Optional[Dict[str, Any]] = None):
        """
        Track a new event.

        Args:
            event_type: Type of event
            level: Severity level
            message: Human-readable message
            details: Additional event details
        """
        event = Event(event_type=event_type, level=level, message=message, details=details or {})

        # Implement event rotation if limit reached
        if len(self.events) >= self.max_events:
            # Keep most recent events, drop oldest
            # Keep the last 90% of max_events to avoid frequent rotation
            keep_count = int(self.max_events * 0.9)
            dropped_count = len(self.events) - keep_count
            self.events = self.events[-keep_count:]
            self._dropped_events_count += dropped_count
            logger.debug(
                f"Event limit reached ({self.max_events}), dropped {dropped_count} oldest events. "
                f"Total dropped: {self._dropped_events_count}"
            )

        self.events.append(event)

        # Log event
        log_msg = f"[{event_type.value}] {message}"
        if level == EventLevel.INFO:
            logger.info(log_msg)
        elif level == EventLevel.WARNING:
            logger.warning(log_msg)
        elif level in (EventLevel.ERROR, EventLevel.CRITICAL):
            logger.error(log_msg)

    def start_operation(self, operation_name: str):
        """Mark the start of an operation."""
        self._operation_start_time = datetime.now()
        self.track(
            EventType.OPERATION_STARTED,
            EventLevel.INFO,
            f"Starting operation: {operation_name}",
            {"operation": operation_name},
        )

    def complete_operation(self, operation_name: str, success: bool = True):
        """Mark the completion of an operation."""
        duration = None
        if self._operation_start_time:
            duration = (datetime.now() - self._operation_start_time).total_seconds()

        event_type = EventType.OPERATION_COMPLETED if success else EventType.OPERATION_FAILED
        level = EventLevel.INFO if success else EventLevel.ERROR

        details = {"operation": operation_name}
        if duration:
            details["duration_seconds"] = duration

        message = f"Operation {'completed' if success else 'failed'}: {operation_name}"
        if duration:
            message += f" (took {duration:.2f}s)"

        self.track(event_type, level, message, details)

    def track_unregistered_found(self, count: int, tag: str = ""):
        """Track discovery of unregistered torrents."""
        self.track(
            EventType.UNREGISTERED_FOUND,
            EventLevel.WARNING,
            f"Found {count} unregistered torrent{'s' if count != 1 else ''}",
            {"count": count, "tag": tag},
        )

    def track_torrents_deleted(self, count: int, disk_freed_bytes: int = 0, tags: Optional[List[str]] = None):
        """Track torrent deletions."""
        disk_freed_gb = disk_freed_bytes / (1024**3) if disk_freed_bytes else 0

        message = f"Deleted {count} torrent{'s' if count != 1 else ''}"
        if disk_freed_gb > 0:
            message += f" (freed {disk_freed_gb:.2f} GB)"

        self.track(
            EventType.TORRENT_DELETED,
            EventLevel.WARNING,
            message,
            {"count": count, "disk_freed_bytes": disk_freed_bytes, "tags": tags or []},
        )

    def track_orphaned_files_found(self, count: int, total_size_bytes: int = 0):
        """Track discovery of orphaned files."""
        size_gb = total_size_bytes / (1024**3) if total_size_bytes else 0

        message = f"Found {count} orphaned file{'s' if count != 1 else ''}"
        if size_gb > 0:
            message += f" ({size_gb:.2f} GB)"

        self.track(
            EventType.ORPHANED_FILES_FOUND,
            EventLevel.WARNING,
            message,
            {"count": count, "total_size_bytes": total_size_bytes},
        )

    def track_orphaned_files_deleted(self, count: int, total_size_bytes: int = 0):
        """Track deletion of orphaned files."""
        size_gb = total_size_bytes / (1024**3) if total_size_bytes else 0

        message = f"Deleted {count} orphaned file{'s' if count != 1 else ''}"
        if size_gb > 0:
            message += f" (freed {size_gb:.2f} GB)"

        self.track(
            EventType.ORPHANED_FILES_DELETED,
            EventLevel.INFO,
            message,
            {"count": count, "total_size_bytes": total_size_bytes},
        )

    def track_large_operation_warning(self, operation: str, details: Dict[str, Any]):
        """Track warnings for large operations."""
        self.track(
            EventType.LARGE_OPERATION_WARNING,
            EventLevel.WARNING,
            f"Large operation detected: {operation}",
            details,
        )

    def get_events_by_level(self, min_level: EventLevel) -> List[Event]:
        """
        Get events at or above a certain severity level.

        Args:
            min_level: Minimum severity level to include

        Returns:
            List of events matching criteria
        """
        level_order = {EventLevel.INFO: 0, EventLevel.WARNING: 1, EventLevel.ERROR: 2, EventLevel.CRITICAL: 3}

        min_severity = level_order[min_level]
        return [event for event in self.events if level_order[event.level] >= min_severity]

    def get_events_by_type(self, event_type: EventType) -> List[Event]:
        """
        Get events of a specific type.

        Args:
            event_type: Type of events to retrieve

        Returns:
            List of events of the specified type
        """
        return [event for event in self.events if event.event_type == event_type]

    def get_summary(self) -> Dict[str, Any]:
        """
        Get a summary of tracked events.

        Returns:
            Dictionary with event statistics and summaries
        """
        summary = {
            "total_events": len(self.events),
            "events_by_level": {
                "info": len([e for e in self.events if e.level == EventLevel.INFO]),
                "warning": len([e for e in self.events if e.level == EventLevel.WARNING]),
                "error": len([e for e in self.events if e.level == EventLevel.ERROR]),
                "critical": len([e for e in self.events if e.level == EventLevel.CRITICAL]),
            },
            "events_by_type": {},
            "latest_events": [event.to_dict() for event in self.events[-10:]],  # Last 10 events
        }

        # Count events by type
        for event in self.events:
            event_type_str = event.event_type.value
            summary["events_by_type"][event_type_str] = summary["events_by_type"].get(event_type_str, 0) + 1

        return summary

    def clear(self):
        """Clear all tracked events."""
        self.events.clear()
        self._operation_start_time = None


# Global event tracker instance
_global_tracker: Optional[EventTracker] = None


def get_event_tracker() -> EventTracker:
    """
    Get the global event tracker instance.

    Returns:
        Global EventTracker instance
    """
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = EventTracker()
    return _global_tracker


def reset_event_tracker():
    """Reset the global event tracker (useful for testing)."""
    global _global_tracker
    _global_tracker = None
