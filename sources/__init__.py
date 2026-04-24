"""Media source abstraction layer for Decky Links.

This package provides a unified interface for different hardware and virtual
trigger sources (NFC, storage media, cameras, MQTT, file watchers).  Each
source runs as an independent asyncio task and pushes events into a shared
queue consumed by the plugin's main loop.
"""

from sources.base import (
    SourceType,
    SourceEventKind,
    MediaEventKind,
    SourceEvent,
    MediaEvent,
    PluginEvent,
    MediaSource,
)
from sources.manager import SourceManager

__all__ = [
    "SourceType",
    "SourceEventKind",
    "MediaEventKind",
    "SourceEvent",
    "MediaEvent",
    "PluginEvent",
    "MediaSource",
    "SourceManager",
]
