"""File watcher virtual trigger source.

Polls a configured directory for ``.json`` files following the same
``decky-links.json`` payload schema used by :class:`StorageSource`.
A file appearing emits ``MediaEvent(LOAD)``; a file disappearing emits
``MediaEvent(UNLOAD)``.

Opt-in only — disabled by default via ``settings["enabled"] = False``.
The watch directory must be an absolute path and must exist at ``start()``
time.  It is re-checked each ``poll()`` cycle so temporary disappearance
(e.g. unmounted network share) marks the source inactive.

Security: the watch directory is validated as an absolute path; relative
paths and traversal sequences are rejected.
"""

import json
import os
import traceback
from collections import deque
from typing import Any, Dict, Optional, Set

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceType,
)

PAYLOAD_SCHEMA_VERSION = 1


class FileWatchSource(MediaSource):
    """Directory-watching trigger source.

    Scans ``watch_dir`` each poll cycle.  New ``.json`` files with valid
    payloads produce LOAD events; deleted files produce UNLOAD events.
    Multiple changes in one cycle are buffered and returned one per call.
    """

    source_type = SourceType.FILE_WATCH

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._watch_dir: str = settings.get("watch_dir", "")
        self._active = False
        self._seen: Dict[str, str] = {}   # filename → URI
        self._pending: deque = deque()    # buffered events

    @property
    def source_id(self) -> str:
        return f"file_watch:{self._watch_dir}"

    @property
    def poll_interval(self) -> float:
        try:
            v = float(self._settings.get("poll_interval", 2.0))
            return v if 0.5 <= v <= 60.0 else 2.0
        except (TypeError, ValueError):
            return 2.0

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Validate watch directory and begin scanning."""
        if not self._settings.get("enabled", False):
            return False

        watch_dir = self._watch_dir
        if not watch_dir:
            if self._logger:
                self._logger.warning("FileWatchSource: watch_dir not configured")
            return False

        if not os.path.isabs(watch_dir):
            if self._logger:
                self._logger.warning(
                    f"FileWatchSource: watch_dir must be absolute: {watch_dir!r}"
                )
            return False

        if not os.path.isdir(watch_dir):
            return False

        self._active = True
        if self._logger:
            self._logger.info(f"FileWatchSource: watching {watch_dir}")
        return True

    async def stop(self) -> None:
        """Stop watching and clear state."""
        self._active = False
        self._seen.clear()
        self._pending.clear()

    def is_active(self) -> bool:
        return self._active

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Return one buffered event, or scan for new changes."""
        if self._pending:
            return self._pending.popleft()

        if not self._active:
            return None

        try:
            entries: Set[str] = {
                f for f in os.listdir(self._watch_dir) if f.endswith(".json")
            }
        except OSError as e:
            if self._logger:
                self._logger.error(f"FileWatchSource: listdir failed: {e}")
            self._active = False
            return None

        # New files
        for fname in entries - set(self._seen):
            path = os.path.join(self._watch_dir, fname)
            payload = self._read_payload(path)
            if payload is None:
                continue
            uri = payload["uri"]
            self._seen[fname] = uri
            if self._logger:
                self._logger.info(f"FileWatchSource: new file {fname} uri={uri}")
            self._pending.append(MediaEvent(
                kind=MediaEventKind.LOAD,
                source_type=SourceType.FILE_WATCH,
                source_id=self.source_id,
                media_id=fname,
                uri=uri,
                payload={k: v for k, v in payload.items() if k != "uri"},
            ))

        # Removed files
        for fname in set(self._seen) - entries:
            uri = self._seen.pop(fname)
            if self._logger:
                self._logger.info(f"FileWatchSource: removed file {fname}")
            self._pending.append(MediaEvent(
                kind=MediaEventKind.UNLOAD,
                source_type=SourceType.FILE_WATCH,
                source_id=self.source_id,
                media_id=fname,
                uri=uri,
            ))

        return self._pending.popleft() if self._pending else None

    # ── Payload ────────────────────────────────────────────────────────

    def _read_payload(self, path: str) -> Optional[Dict[str, Any]]:
        """Read and validate a payload JSON file. Returns normalised dict or None."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(data, dict):
            return None
        if data.get("version") != PAYLOAD_SCHEMA_VERSION:
            return None
        if not isinstance(data.get("uri"), str) or not data["uri"]:
            return None

        return {
            "version": data["version"],
            "uri": data["uri"],
            "title": data.get("title", ""),
            "icon": data.get("icon", ""),
        }
