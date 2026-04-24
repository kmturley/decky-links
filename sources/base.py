"""Base types for the media source abstraction.

Defines the enums, event dataclasses, and abstract base class that every
concrete source (NFC, storage, camera, etc.) must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Union


# ── Enums ──────────────────────────────────────────────────────────────────


class SourceType(Enum):
    """Identifies the category of a media source."""

    NFC = "nfc"
    STORAGE = "storage"
    CAMERA = "camera"
    MQTT = "mqtt"
    FILE_WATCH = "file_watch"


class SourceEventKind(Enum):
    """Lifecycle events for a source's hardware / connection."""

    CONNECTED = "connected"        # source hardware detected / broker reachable
    DISCONNECTED = "disconnected"  # source hardware lost / broker unreachable


class MediaEventKind(Enum):
    """Interaction events for media presented to a source."""

    LOAD = "load"      # media inserted / tag tapped / QR scanned
    UNLOAD = "unload"  # media ejected / tag removed / QR left frame


# ── Events ─────────────────────────────────────────────────────────────────


@dataclass
class SourceEvent:
    """Hardware / connection lifecycle event.

    Emitted when a source's underlying hardware is detected or lost.
    For example, an NFC reader being plugged in or a webcam disconnecting.
    """

    kind: SourceEventKind
    source_type: SourceType
    source_id: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaEvent:
    """Media interaction event.

    Emitted when physical or virtual media is presented to (LOAD) or
    removed from (UNLOAD) a source.  For example, an NFC tag being tapped
    or a USB floppy being ejected.
    """

    kind: MediaEventKind
    source_type: SourceType
    source_id: str
    media_id: str
    uri: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


# Union type for the shared event queue.
PluginEvent = Union[SourceEvent, MediaEvent]


# ── Base Class ─────────────────────────────────────────────────────────────


class MediaSource(ABC):
    """Abstract base class for all hardware and virtual trigger sources.

    Each concrete source:
    - Runs in its own ``asyncio.Task`` (managed by :class:`SourceManager`).
    - Returns ``PluginEvent`` instances from :meth:`poll`.
    - Reports its own connection lifecycle via ``SourceEvent``.
    - Reports media interactions via ``MediaEvent``.

    Subclasses must set ``source_type`` as a class attribute and implement
    all abstract methods.
    """

    source_type: SourceType

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Return a unique identifier for this source instance.

        Typically derived from the hardware path or configuration
        (e.g. ``"nfc:/dev/ttyUSB0"``, ``"camera:/dev/video0"``).
        """

    @property
    def poll_interval(self) -> float:
        """Seconds between poll cycles.  Override for source-specific timing."""
        return 0.5

    @abstractmethod
    async def start(self) -> bool:
        """Initialise the source hardware.

        Returns ``True`` if the source is ready for polling, ``False`` if
        initialisation failed (the manager will retry later).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Release all resources held by this source."""

    @abstractmethod
    def is_active(self) -> bool:
        """Return ``True`` if the source believes it is currently usable."""

    @abstractmethod
    async def poll(self) -> Optional[PluginEvent]:
        """Perform one poll cycle.

        Return a ``SourceEvent`` or ``MediaEvent`` if something happened,
        or ``None`` if there is nothing to report.  The caller
        (:class:`SourceManager`) sleeps for :attr:`poll_interval` seconds
        between calls.
        """
