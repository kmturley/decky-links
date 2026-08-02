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
    
    Interface Contract:
    - Implementations must support both synchronous and asynchronous operations
    - All methods should be thread-safe or clearly document thread constraints
    - Connection state must be queryable via is_connected()
    - Firmware version should be available after successful connection
    - UID reading should support configurable timeout
    - Additional hardware-specific methods can be delegated via __getattr__
    
    Supported Methods (minimum):
    - connect(): Establish connection to hardware
    - close(): Release hardware resources
    - is_connected(): Query connection status
    - firmware_version(): Get firmware/version tuple
    - read_uid(timeout): Read passive target UID
    - read_uid_iso14443b(timeout): Read ISO-14443B UID
    
    Additional Methods (delegated):
    - mifare_classic_authenticate_block(uid, block, key_type, key)
    - mifare_classic_read_block(block)
    - mifare_classic_write_block(block, data)
    - ntag2xx_read_block(page)
    - ntag2xx_write_block(page, data)
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

    @abstractmethod
    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID from ISO-14443B tag.
        
        Returns UID bytes or None if no tag present.
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
        import asyncio
        return await asyncio.to_thread(self._connect_blocking)

    def _connect_blocking(self) -> bool:
        """Synchronous connect with a hard 5-second deadline via threading.Timer.

        asyncio.wait_for cannot cancel a running thread on Python 3.9, so we
        use threading.Timer to force-close the serial port when the deadline
        expires.  Closing the port causes the adafruit_pn532 I/O loops to
        raise within one poll cycle (~10 ms) rather than blocking for 30 s.

        The timer must start BEFORE PN532_UART() is constructed because the
        constructor itself calls _wakeup() → SAM_configuration() and reads
        firmware_version — all of which can block on the wrong device.
        """
        import serial
        import time
        import threading
        from adafruit_pn532.uart import PN532_UART

        timed_out = [False]

        def _on_timeout():
            timed_out[0] = True
            self.close()  # closing the port causes in_waiting/write to raise

        timer = threading.Timer(5.0, _on_timeout)
        try:
            self.uart = serial.Serial(
                self.device_path,
                baudrate=self.baudrate,
                timeout=0.1,
                write_timeout=0.5,
            )
            # Start timer BEFORE PN532_UART.__init__ — the constructor calls
            # _wakeup() which runs SAM_configuration + firmware_version and
            # can block for 30 s on a wrong/unresponsive device without this.
            timer.start()
            self._reader = PN532_UART(self.uart, debug=False)
            if timed_out[0]:
                return False

            timer.cancel()
            # Brief settle — some PN532 modules glitch the serial line
            # immediately after SAM configuration before accepting polls.
            time.sleep(0.5)
            return True

        except Exception as e:
            if self.logger:
                self.logger.error(f"PN532UARTReader.connect failed: {e}")
            self.close()
            return False
        finally:
            timer.cancel()

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

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID from ISO-14443B tag using baud rate 3 (106 kbps Type B)."""
        if self._reader:
            try:
                return self._reader.read_passive_target(baud_rate=3, timeout=timeout)
            except Exception:
                pass
        return None

    # allow callers to transparently access any other PN532_UART methods
    def __getattr__(self, name):
        if self._reader and hasattr(self._reader, name):
            return getattr(self._reader, name)
        raise AttributeError(f"{self.__class__.__name__!r} object has no attribute {name!r}")
