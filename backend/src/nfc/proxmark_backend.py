"""Proxmark3 reader backend.

This module provides support for Proxmark3 devices via subprocess calls
to the proxmark3 client binary.
"""

import subprocess
import re
from typing import Optional, Tuple
from nfc.reader import Reader


class ProxmarkReader(Reader):
    """Proxmark3 reader using client subprocess."""

    def __init__(self, device_path: str = "/dev/ttyACM0", logger=None):
        self.device_path = device_path
        self.logger = logger
        self._connected = False

    async def connect(self) -> bool:
        try:
            # Test connection with hw version command
            result = self._run_command("hw version")
            if result and "Proxmark3" in result:
                self._connected = True
                if self.logger:
                    self.logger.info(f"Connected to Proxmark3 on {self.device_path}")
                return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"Proxmark connect failed: {e}")
        return False

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """Get Proxmark firmware version."""
        if not self._connected:
            return None
        try:
            result = self._run_command("hw version")
            if result:
                # Parse version from output
                match = re.search(r"v(\d+)\.(\d+)\.(\d+)", result)
                if match:
                    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0)
        except Exception:
            pass
        return None

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID using hf 14a reader command."""
        if not self._connected:
            return None
        try:
            result = self._run_command("hf 14a reader")
            if result:
                # Parse UID from output
                match = re.search(r"UID\s*:\s*([0-9a-fA-F\s]+)", result)
                if match:
                    uid_hex = match.group(1).replace(" ", "")
                    return bytes.fromhex(uid_hex)
        except Exception:
            pass
        return None

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read ISO-14443B UID using hf 14b reader command."""
        if not self._connected:
            return None
        try:
            result = self._run_command("hf 14b reader")
            if result:
                match = re.search(r"UID\s*:\s*([0-9a-fA-F\s]+)", result)
                if match:
                    uid_hex = match.group(1).replace(" ", "")
                    return bytes.fromhex(uid_hex)
        except Exception:
            pass
        return None

    def transceive(self, data: bytes, timeout: float = 0.1) -> Optional[bytes]:
        """Send raw command."""
        if not self._connected:
            return None
        try:
            hex_data = data.hex()
            result = self._run_command(f"hf 14a raw -c {hex_data}")
            if result:
                # Parse response
                match = re.search(r"([0-9a-fA-F\s]+)", result)
                if match:
                    response_hex = match.group(1).replace(" ", "")
                    return bytes.fromhex(response_hex)
        except Exception:
            pass
        return None

    def ntag2xx_read_block(self, block: int) -> Optional[bytes]:
        """Read NTAG block."""
        if not self._connected:
            return None
        try:
            result = self._run_command(f"hf mfu rdbl {block}")
            if result:
                match = re.search(r"([0-9a-fA-F]{8})", result)
                if match:
                    return bytes.fromhex(match.group(1))
        except Exception:
            pass
        return None

    def ntag2xx_write_block(self, block: int, data: bytes) -> bool:
        """Write NTAG block."""
        if not self._connected or len(data) != 4:
            return False
        try:
            hex_data = data.hex()
            result = self._run_command(f"hf mfu wrbl {block} {hex_data}")
            return result and "success" in result.lower()
        except Exception:
            return False

    def mifare_classic_authenticate_block(self, uid: bytes, block: int, key_type: int, key: bytes) -> bool:
        """Authenticate Mifare Classic block."""
        if not self._connected or len(key) != 6:
            return False
        try:
            key_hex = key.hex()
            key_letter = "A" if key_type == 0x60 else "B"
            result = self._run_command(f"hf mf auth {key_letter} {block} {key_hex}")
            return result and "success" in result.lower()
        except Exception:
            return False

    def mifare_classic_read_block(self, block: int) -> Optional[bytes]:
        """Read Mifare Classic block."""
        if not self._connected:
            return None
        try:
            result = self._run_command(f"hf mf rdbl {block}")
            if result:
                match = re.search(r"([0-9a-fA-F]{32})", result)
                if match:
                    return bytes.fromhex(match.group(1))
        except Exception:
            pass
        return None

    def mifare_classic_write_block(self, block: int, data: bytes) -> bool:
        """Write Mifare Classic block."""
        if not self._connected or len(data) != 16:
            return False
        try:
            hex_data = data.hex()
            result = self._run_command(f"hf mf wrbl {block} {hex_data}")
            return result and "success" in result.lower()
        except Exception:
            return False

    def _run_command(self, cmd: str) -> Optional[str]:
        """Execute proxmark3 client command."""
        try:
            full_cmd = ["proxmark3", self.device_path, "-c", cmd]
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=5.0
            )
            return result.stdout if result.returncode == 0 else None
        except Exception as e:
            if self.logger:
                self.logger.error(f"Proxmark command failed: {e}")
            return None
