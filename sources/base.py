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
    SERIAL = "serial"
    FILE_WATCH = "file_watch"


class SourceEventKind(Enum):
    """Lifecycle events for a source's hardware / connection."""

    CONNECTED = "connected"        # source hardware detected / broker reachable
    DISCONNECTED = "disconnected"  # source hardware lost / broker unreachable


class MediaEventKind(Enum):
    """Interaction events for media presented to a source."""

    LOAD = "load"      # media inserted / tag tapped / QR scanned
    UNLOAD = "unload"  # media ejected / tag removed / QR left frame
    # Media is present but not yet readable. Emitted by sources whose read is
    # slow enough to look like a hang: a floppy can take a minute to mount, and
    # until this existed the panel said "No disk" for that whole minute. Always
    # followed by a LOAD (readable, blank or unreadable) for the same medium.
    LOADING = "loading"


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

    def is_enabled(self) -> bool:
        """Return ``True`` when the user wants this source running at all.

        Distinct from :meth:`is_active`, which reports whether the hardware is
        currently working. A source that is switched off must not be retried on
        a backoff timer forever — that burns wakeups on a battery device and
        makes a deliberate "off" indistinguishable from a hardware fault in the
        logs. Sources with no ``enabled`` setting are always on.
        """
        settings = getattr(self, "_settings", None)
        if not isinstance(settings, dict):
            return True
        return bool(settings.get("enabled", True))

    def has_media(self) -> bool:
        """Return True when physical media is actively present.

        Defaults to ``is_active()``.  Override in sources where the source
        can be "active" (infrastructure running) without media being present —
        e.g. StorageSource whose udev monitor is always up on Linux.
        """
        return self.is_active()

    def has_drive(self) -> bool:
        """Return True when this source's hardware is connected and usable.

        Distinct from :meth:`has_media`: a floppy drive with no disk in it is
        still connected. This is what the panel's source row reports, so that
        ejecting a disk does not make the whole source look broken.
        """
        return self.is_active()

    def sub_devices(self) -> Dict[str, Dict[str, bool]]:
        """Per-category presence and enablement, for sources covering several.

        Storage is one source spanning floppy, optical, USB and card readers,
        and the panel shows a row for each — so "some drive is attached" is not
        enough to render it. Every other source is a single device and returns
        ``{}``.

        This is part of the contract rather than something the plugin
        discovers, because it used to be reached for with
        ``hasattr(source, "drive_kinds_present")`` and the enablement half was
        recomputed in the plugin from the source's own settings dict plus an
        imported copy of its defaults. Two places deciding the same thing.

        Shape: ``{category: {"present": bool, "enabled": bool}}``.
        """
        return {}

    # ── Pairing ────────────────────────────────────────────────────────

    def can_write(self) -> bool:
        """Return ``True`` when media on this source can be paired.

        Pairing means persisting a URI onto the medium itself, so that it
        launches the same game on any Deck. Sources that only observe —
        a camera reading QR codes, an MQTT topic — return ``False``.
        """
        return False

    async def write_uri(
        self, media_id: str, uri: str, title: str = ""
    ) -> "tuple[bool, Optional[str]]":
        """Persist ``uri`` onto the medium identified by ``media_id``.

        Returns ``(success, error_message)``. ``media_id`` is whatever this
        source puts in :attr:`MediaEvent.media_id` — a tag UID for NFC, a
        device node for storage.

        ``title`` is the human name of what the URI launches, recorded so the
        medium says what it is without a Steam lookup. It is advisory: sources
        whose format has nowhere to put it ignore it. NFC is one — an NDEF URI
        record carries a URI and nothing else, and a second record would eat
        scarce tag memory to store what the app id already resolves to.
        """
        return False, f"{self.source_type.value} media cannot be paired"

    def rearm(self) -> None:
        """Re-report media that is already present on the next poll.

        Sources only emit LOAD on arrival, so media sitting in place when the
        user presses "Pair" would never produce an event and the button would
        appear to hang. Called by ``Plugin.start_pairing`` on every source.
        """
        return None

    @abstractmethod
    async def poll(self) -> Optional[PluginEvent]:
        """Perform one poll cycle.

        Return a ``SourceEvent`` or ``MediaEvent`` if something happened,
        or ``None`` if there is nothing to report.  The caller
        (:class:`SourceManager`) sleeps for :attr:`poll_interval` seconds
        between calls.

        **This runs on the plugin's only event loop.** Every source task, the
        event loop that drains the queue, and every RPC from the frontend
        share it, so a blocking call here does not stall one source — it
        stalls the whole plugin, including the panel's twice-a-second status
        poll. Being declared ``async`` does not make a serial read, a
        ``subprocess.run`` or a ``time.sleep`` cooperative; it only hides
        them.

        So anything that blocks — serial, subprocess, filesystem, or a
        CPU-bound decode — must be pushed to a worker thread::

            async def poll(self):
                return await asyncio.to_thread(self._poll_blocking)

        The same applies to :meth:`write_uri` and :meth:`start`, which are
        awaited from the same loop.
        """
