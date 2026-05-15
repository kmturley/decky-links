"""nfcpy reader backend.

This module provides support for NFC readers via the nfcpy library,
which supports a wide range of readers including ACR122U, SCL3711, and others.
"""

from typing import Optional, Tuple
from nfc.reader import Reader


class NfcPyReader(Reader):
    """NFC reader using nfcpy library."""

    def __init__(self, device_path: Optional[str] = None, logger=None):
        self.device_path = device_path or 'usb'
        self.logger = logger
        self._clf = None
        self._target = None

    async def connect(self) -> bool:
        try:
            import nfc
            
            # Open contactless frontend
            self._clf = nfc.ContactlessFrontend(self.device_path)
            
            if self._clf:
                if self.logger:
                    self.logger.info(f"Connected to nfcpy reader: {self._clf}")
                return True
            return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"nfcpy connect failed: {e}")
            return False

    def close(self) -> None:
        if self._clf:
            try:
                self._clf.close()
            except Exception:
                pass
        self._clf = None
        self._target = None

    def is_connected(self) -> bool:
        return self._clf is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """nfcpy doesn't expose firmware version directly."""
        return None

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID using nfcpy sense."""
        if not self._clf:
            return None
        
        try:
            import nfc
            
            # Sense for ISO-14443A tags
            target = self._clf.sense(
                nfc.clf.RemoteTarget('106A'),
                iterations=int(timeout * 10),
                interval=0.1
            )
            
            if target and hasattr(target, 'identifier'):
                self._target = target
                return target.identifier
        except Exception:
            pass
        
        return None

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID from ISO-14443B tag."""
        if not self._clf:
            return None
        
        try:
            import nfc
            
            # Sense for ISO-14443B tags
            target = self._clf.sense(
                nfc.clf.RemoteTarget('106B'),
                iterations=int(timeout * 10),
                interval=0.1
            )
            
            if target and hasattr(target, 'identifier'):
                self._target = target
                return target.identifier
        except Exception:
            pass
        
        return None

    def transceive(self, data: bytes, timeout: float = 0.1) -> Optional[bytes]:
        """Send raw command via nfcpy exchange."""
        if not self._clf or not self._target:
            return None
        
        try:
            response = self._clf.exchange(data, timeout=timeout)
            return response if response else None
        except Exception:
            return None

    def ntag2xx_read_block(self, block: int) -> Optional[bytes]:
        """Read NTAG block using READ command."""
        if not self._clf or not self._target:
            return None
        
        try:
            # NTAG READ command: 0x30 + block number
            cmd = bytes([0x30, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            if response and len(response) >= 4:
                return response[:4]
        except Exception:
            pass
        
        return None

    def ntag2xx_write_block(self, block: int, data: bytes) -> bool:
        """Write NTAG block using WRITE command."""
        if not self._clf or not self._target or len(data) != 4:
            return False
        
        try:
            # NTAG WRITE command: 0xA2 + block number + 4 bytes data
            cmd = bytes([0xA2, block]) + data
            response = self._clf.exchange(cmd, timeout=0.1)
            
            # ACK is 0x0A for successful write
            return response == bytes([0x0A]) if response else False
        except Exception:
            return False

    def mifare_classic_authenticate_block(self, uid: bytes, block: int, key_type: int, key: bytes) -> bool:
        """Authenticate Mifare Classic block."""
        if not self._clf or not self._target or len(key) != 6:
            return False
        
        try:
            import nfc
            
            # nfcpy uses Tag object for Mifare Classic operations
            if hasattr(self._target, 'authenticate'):
                # key_type: 0x60 = Key A, 0x61 = Key B
                key_name = 'A' if key_type == 0x60 else 'B'
                return self._target.authenticate(block, key_name, key)
            
            # Fallback: manual authentication via exchange
            # AUTH command: 0x60/0x61 + block + key
            cmd = bytes([key_type, block]) + key
            response = self._clf.exchange(cmd, timeout=0.1)
            return response is not None
        except Exception:
            return False

    def mifare_classic_read_block(self, block: int) -> Optional[bytes]:
        """Read Mifare Classic block."""
        if not self._clf or not self._target:
            return None
        
        try:
            # Mifare Classic READ command: 0x30 + block number
            cmd = bytes([0x30, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            if response and len(response) >= 16:
                return response[:16]
        except Exception:
            pass
        
        return None

    def mifare_classic_write_block(self, block: int, data: bytes) -> bool:
        """Write Mifare Classic block."""
        if not self._clf or not self._target or len(data) != 16:
            return False
        
        try:
            # Mifare Classic WRITE command: 0xA0 + block number
            cmd = bytes([0xA0, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            # Check for ACK (0x0A)
            if response != bytes([0x0A]):
                return False
            
            # Send data
            response = self._clf.exchange(data, timeout=0.1)
            return response == bytes([0x0A]) if response else False
        except Exception:
            return False

    def SAM_configuration(self):
        """Compatibility method - nfcpy doesn't need SAM configuration."""
        pass

    def read_passive_target(self, baud_rate: int = 0, timeout: float = 0.2) -> Optional[bytes]:
        """Compatibility method for PN532-style API."""
        if baud_rate == 3:
            # ISO-14443B
            return self.read_uid_iso14443b(timeout)
        else:
            # ISO-14443A (default)
            return self.read_uid(timeout)
