"""Reader abstraction for NFC hardware.

This module defines a generic :class:`Reader` interface used by the
Decky Links plugin plus a concrete implementation for the PN532 over
UART.  The goal is to isolate transport- and chip-specific details
from the main plugin logic so additional backends can be added later.

The interface is intentionally thin: the plugin still relies on the
PN532-style method names (e.g. ``mifare_classic_read_block``) so the
wrapper merely forwards calls.  Higher-level refactors can introduce a
more uniform API once multiple readers exist.
"""

import os
from abc import ABC, abstractmethod
from typing import Optional, Tuple

# The serial and adafruit_pn532 imports are lazy to allow tests to run
# without hardware.  Mocks supply substitute modules via sys.modules.


class Reader(ABC):
    """Abstract base class for NFC readers.

    Subclasses should implement ``connect``/``close`` plus a minimal set of
    convenience helpers.  The :meth:`__getattr__` hook in
    :class:`PN532UARTReader` permits delegating additional methods directly
    to the underlying driver so the existing plugin code can continue to
    call familiar names without change.
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Attempt to initialise the reader hardware.

        Returns ``True`` on success (ready for use) or ``False`` on
        failure.  The plugin will treat a failure as if no reader were
        present and will retry periodically.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any resources held by the reader."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return ``True`` if the reader believes it is currently usable."""

    @abstractmethod
    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """Return the firmware/version tuple, or ``None`` if unavailable."""

    @abstractmethod
    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Low‑level helper for reading a passive target UID.

        The semantics mirror ``PN532_UART.read_passive_target``; other
        backends should provide a compatible behaviour.
        """


class PN532UARTReader(Reader):
    """PN532 reader connected over a UART serial port.

    This implementation wraps :class:`adafruit_pn532.uart.PN532_UART` and
    exposes the same methods transparently.  A caller can treat the
    instance as if it *were* the underlying PN532 object (e.g. invoking
    ``ntag2xx_read_block``) thanks to ``__getattr__``.
    """

    def __init__(self, device_path: str, baudrate: int, logger=None):
        self.device_path = device_path
        self.baudrate = baudrate
        self.logger = logger
        self.uart = None
        self._reader = None

    async def connect(self) -> bool:
        # ensure the path is present before attempting to open serial
        if not os.path.exists(self.device_path):
            return False
        try:
            import serial
            from adafruit_pn532.uart import PN532_UART

            self.uart = serial.Serial(self.device_path, baudrate=self.baudrate, timeout=0.1)
            self._reader = PN532_UART(self.uart, debug=False)

            version = self._reader.firmware_version
            if version:
                # configure normal card-present polling mode
                self._reader.SAM_configuration()
                return True
            # firmware fetch failed
        except Exception as e:
            if self.logger:
                self.logger.error(f"PN532UARTReader.connect failed: {e}")
        # on any failure clean up
        self.close()
        return False

    def close(self) -> None:
        if self.uart:
            try:
                self.uart.close()
            except Exception:
                pass
        self.uart = None
        self._reader = None

    def is_connected(self) -> bool:
        return self._reader is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        if self._reader:
            return self._reader.firmware_version
        return None

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        if self._reader:
            return self._reader.read_passive_target(timeout=timeout)
        return None

    # allow callers to transparently access any other PN532_UART methods
    def __getattr__(self, name):
        if self._reader and hasattr(self._reader, name):
            return getattr(self._reader, name)
        raise AttributeError(f"{self.__class__.__name__!r} object has no attribute {name!r}")
