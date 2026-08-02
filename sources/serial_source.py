"""Serial port virtual trigger source.

Listens on a configurable serial port for newline-delimited URI strings.
Each complete line received is treated as a URI and emits a
``MediaEvent(LOAD)``.  No UNLOAD is produced — serial is a one-shot trigger.

Opt-in only — disabled by default via ``settings["enabled"] = False``.

pyserial is already a dependency of the NFC reader module, so no new
packages are required.
"""

import traceback
from typing import Optional

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceType,
)


class SerialSource(MediaSource):
    """Serial port line-trigger source.

    Accumulates bytes from the serial port into an internal buffer and
    emits a LOAD event for each newline-terminated, non-empty line.
    """

    source_type = SourceType.SERIAL

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._serial = None
        self._buffer: str = ""
        self._active = False

    @property
    def source_id(self) -> str:
        port = self._settings.get("port", "/dev/ttyUSB0")
        return f"serial:{port}"

    @property
    def poll_interval(self) -> float:
        return 0.1  # fast polling so lines aren't delayed

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Open the serial port."""
        if not self._settings.get("enabled", False):
            return False

        try:
            import serial as pyserial
        except ImportError:
            if self._logger:
                self._logger.warning(
                    "SerialSource: pyserial not available — serial source disabled"
                )
            return False

        port = self._settings.get("port", "/dev/ttyUSB0")
        baud = int(self._settings.get("baudrate", 9600))

        try:
            self._serial = pyserial.Serial(port, baud, timeout=0)
            self._buffer = ""
            self._active = True
            if self._logger:
                self._logger.info(
                    f"SerialSource: opened {port} at {baud} baud"
                )
            return True
        except Exception as e:
            if self._logger:
                self._logger.error(f"SerialSource: failed to open {port}: {e}")
            self._serial = None
            return False

    async def stop(self) -> None:
        """Close the serial port."""
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._active = False
        self._buffer = ""

    def is_active(self) -> bool:
        return self._active

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Read available bytes, return a LOAD event for the next complete line."""
        if not self._active or not self._serial:
            return None

        try:
            waiting = self._serial.in_waiting
            if waiting:
                raw = self._serial.read(waiting)
                self._buffer += raw.decode("utf-8", errors="ignore")
        except Exception as e:
            if self._logger:
                self._logger.error(f"SerialSource: read error: {e}")
                self._logger.error(traceback.format_exc())
            self._active = False
            return None

        if "\n" not in self._buffer:
            return None

        line, self._buffer = self._buffer.split("\n", 1)
        uri = line.strip()
        if not uri:
            return None

        if self._logger:
            self._logger.info(f"SerialSource: trigger uri={uri}")

        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.SERIAL,
            source_id=self.source_id,
            media_id=uri,
            uri=uri,
        )
