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


def build_all(settings_for, logger=None, **extras):
    """Construct every registered source, in a fixed order.

    Adding a source used to mean editing eight places: Plugin.__init__,
    Plugin._main, _all_sources, get_source_statuses, set_source_setting's
    allowlist, SourceType, the TypeScript SourceType, and the panel's icon
    map. This collapses the first three into one entry below.

    ``settings_for`` is a callable taking a source-type string and returning
    that source's live settings dict — the plugin's
    ``SettingsManager.get_source_settings``. ``extras`` supplies constructor
    arguments only some sources take; each is passed only to sources that
    declare it, so NFC can have its key manager without every other source
    growing an unused parameter.
    """
    import inspect

    built = []
    for source_type, cls in source_classes().items():
        kwargs = {
            "settings": settings_for(source_type.value),
            "logger": logger,
        }
        accepted = inspect.signature(cls.__init__).parameters
        kwargs.update({k: v for k, v in extras.items() if k in accepted})
        built.append(cls(**kwargs))
    return built


def source_classes():
    """Every source the plugin knows how to build, in start order.

    Adding a source is one entry here. It used to mean editing eight places:
    Plugin.__init__, Plugin._main, _all_sources, get_source_statuses,
    set_source_setting's allowlist, SourceType, the TypeScript SourceType and
    the panel's icon map.

    A function rather than a module-level dict because each concrete source
    imports from sources.base — resolving them at import time would make this
    package import itself.
    """
    from sources.nfc_source import NfcSource
    from sources.storage_source import StorageSource
    from sources.camera_source import CameraSource
    from sources.mqtt_source import MqttSource
    from sources.serial_source import SerialSource
    from sources.file_watch_source import FileWatchSource

    return {
        SourceType.NFC: NfcSource,
        SourceType.STORAGE: StorageSource,
        SourceType.CAMERA: CameraSource,
        SourceType.MQTT: MqttSource,
        SourceType.SERIAL: SerialSource,
        SourceType.FILE_WATCH: FileWatchSource,
    }


__all__ = [
    "source_classes",
    "build_all",
    "SourceType",
    "SourceEventKind",
    "MediaEventKind",
    "SourceEvent",
    "MediaEvent",
    "PluginEvent",
    "MediaSource",
    "SourceManager",
]
