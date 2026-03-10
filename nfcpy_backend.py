"""Lightweight nfcpy-compatible reader backend.

This backend is intentionally minimal: it provides the same surface the
plugin expects from a reader object (connect/close/is_connected/read_uid,
firmware_version) plus PN532-like method names used by existing code.

If the real ``nfc`` package is unavailable, ``connect`` returns False and the
plugin will keep retrying/remaining idle.
"""

from typing import Optional, Tuple


class NfcPyReader:
    def __init__(self, device_path: str, logger=None):
        self.device_path = device_path
        self.logger = logger
        self._clf = None
        self._connected = False

    async def connect(self) -> bool:
        try:
            import nfc  # type: ignore
            self._clf = nfc.ContactlessFrontend(self.device_path)
            self._connected = bool(self._clf)
            return self._connected
        except Exception as e:
            if self.logger:
                self.logger.error(f"NfcPyReader.connect failed: {e}")
            self._clf = None
            self._connected = False
            return False

    def close(self) -> None:
        try:
            if self._clf:
                self._clf.close()
        except Exception:
            pass
        self._clf = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._clf is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        # nfcpy does not expose PN532-style firmware tuple in a generic way.
        return None

    def read_uid(self, timeout: float = 0.2):
        if not self._clf:
            return None
        try:
            tag = self._clf.connect(rdwr={"on-connect": lambda t: False}, terminate=lambda: False)
            if not tag:
                return None
            identifier = getattr(tag, "identifier", None)
            if identifier is None:
                return None
            return bytes(identifier)
        except Exception:
            return None

    # Optional compatibility methods used by current plugin paths.
    # nfcpy backend does not implement these yet.
    def mifare_classic_authenticate_block(self, *args, **kwargs):
        return False

    def mifare_classic_read_block(self, *args, **kwargs):
        return None

    def mifare_classic_write_block(self, *args, **kwargs):
        return False

    def ntag2xx_read_block(self, *args, **kwargs):
        return None

    def ntag2xx_write_block(self, *args, **kwargs):
        return False
