"""ACR122U reader backend using PC/SC interface.

This module provides direct support for ACR122U USB NFC readers via the
PC/SC (pcsclite) interface, bypassing nfcpy for lower-level control.
"""

from typing import Optional, Tuple
from nfc.reader import Reader


class ACR122UReader(Reader):
    """ACR122U reader using PC/SC smartcard interface."""

    def __init__(self, logger=None):
        self.logger = logger
        self._connection = None
        self._atr = None

    async def connect(self) -> bool:
        try:
            from smartcard.System import readers
            from smartcard.util import toHexString
            
            reader_list = readers()
            if not reader_list:
                if self.logger:
                    self.logger.error("No PC/SC readers found")
                return False
            
            # Find ACR122U reader
            acr_reader = None
            for r in reader_list:
                if "ACR122" in str(r):
                    acr_reader = r
                    break
            
            if not acr_reader:
                if self.logger:
                    self.logger.error("ACR122U reader not found")
                return False
            
            self._connection = acr_reader.createConnection()
            self._connection.connect()
            self._atr = self._connection.getATR()
            
            if self.logger:
                self.logger.info(f"Connected to ACR122U, ATR: {toHexString(self._atr)}")
            
            return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"ACR122U connect failed: {e}")
            return False

    def close(self) -> None:
        if self._connection:
            try:
                self._connection.disconnect()
            except Exception:
                pass
        self._connection = None
        self._atr = None

    def is_connected(self) -> bool:
        return self._connection is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """ACR122U firmware version via GET DATA command."""
        if not self._connection:
            return None
        try:
            cmd = [0xFF, 0x00, 0x48, 0x00, 0x00]
            data, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 == 0x90 and sw2 == 0x00 and len(data) >= 4:
                return tuple(data[:4])
        except Exception:
            pass
        return None

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID via APDU command."""
        if not self._connection:
            return None
        try:
            # Get UID command
            cmd = [0xFF, 0xCA, 0x00, 0x00, 0x00]
            data, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 == 0x90 and sw2 == 0x00 and data:
                return bytes(data)
        except Exception:
            pass
        return None

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read ISO-14443B UID (not directly supported by ACR122U)."""
        return None

    def transceive(self, data: bytes, timeout: float = 0.1) -> Optional[bytes]:
        """Send raw APDU command."""
        if not self._connection:
            return None
        try:
            cmd = [0xFF, 0x00, 0x00, 0x00, len(data)] + list(data)
            response, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 == 0x90 and sw2 == 0x00:
                return bytes(response)
        except Exception:
            pass
        return None

    def ntag2xx_read_block(self, block: int) -> Optional[bytes]:
        """Read NTAG block via APDU."""
        if not self._connection:
            return None
        try:
            cmd = [0xFF, 0xB0, 0x00, block, 0x04]
            data, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 == 0x90 and sw2 == 0x00:
                return bytes(data)
        except Exception:
            pass
        return None

    def ntag2xx_write_block(self, block: int, data: bytes) -> bool:
        """Write NTAG block via APDU."""
        if not self._connection or len(data) != 4:
            return False
        try:
            cmd = [0xFF, 0xD6, 0x00, block, 0x04] + list(data)
            _, sw1, sw2 = self._connection.transmit(cmd)
            return sw1 == 0x90 and sw2 == 0x00
        except Exception:
            return False

    def mifare_classic_authenticate_block(self, uid: bytes, block: int, key_type: int, key: bytes) -> bool:
        """Authenticate Mifare Classic block."""
        if not self._connection or len(key) != 6:
            return False
        try:
            # Load key
            cmd = [0xFF, 0x82, 0x00, 0x00, 0x06] + list(key)
            _, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 != 0x90 or sw2 != 0x00:
                return False
            
            # Authenticate
            key_num = 0x60 if key_type == 0x60 else 0x61
            cmd = [0xFF, 0x86, 0x00, 0x00, 0x05, 0x01, 0x00, block, key_num, 0x00]
            _, sw1, sw2 = self._connection.transmit(cmd)
            return sw1 == 0x90 and sw2 == 0x00
        except Exception:
            return False

    def mifare_classic_read_block(self, block: int) -> Optional[bytes]:
        """Read Mifare Classic block."""
        if not self._connection:
            return None
        try:
            cmd = [0xFF, 0xB0, 0x00, block, 0x10]
            data, sw1, sw2 = self._connection.transmit(cmd)
            if sw1 == 0x90 and sw2 == 0x00:
                return bytes(data)
        except Exception:
            pass
        return None

    def mifare_classic_write_block(self, block: int, data: bytes) -> bool:
        """Write Mifare Classic block."""
        if not self._connection or len(data) != 16:
            return False
        try:
            cmd = [0xFF, 0xD6, 0x00, block, 0x10] + list(data)
            _, sw1, sw2 = self._connection.transmit(cmd)
            return sw1 == 0x90 and sw2 == 0x00
        except Exception:
            return False
