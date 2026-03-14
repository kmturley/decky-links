"""Tag-type-specific handlers for NFC read/write operations.

This module provides a unified interface for reading and writing NDEF data
to different NFC tag families. Each handler encapsulates the protocol-specific
logic for its tag type.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
import time


class TagHandler(ABC):
    """Abstract base for tag-type handlers."""

    @abstractmethod
    def read_ndef(self, reader) -> bytes:
        """Read raw NDEF data from the tag. Return empty bytes if read fails."""

    @abstractmethod
    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to the tag. Return (success, error_msg)."""

    @abstractmethod
    def get_capacity(self) -> int:
        """Return usable capacity in bytes for NDEF payload."""


class NTAGHandler(TagHandler):
    """Handler for NTAG21x family (NTAG213, NTAG215, NTAG216, etc.)."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.user_pages = list(range(4, 134))
        self.batch_size = 4

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from NTAG pages with batching."""
        data = bytearray()
        page_idx = 0
        
        while page_idx < len(self.user_pages):
            try:
                # Try batch read if explicitly available
                if hasattr(reader, 'ntag2xx_read_blocks'):
                    try:
                        batch_pages = self.user_pages[page_idx:page_idx + self.batch_size]
                        blocks = reader.ntag2xx_read_blocks(batch_pages)
                        if blocks is not None and len(blocks) > 0:
                            for block in blocks:
                                data.extend(block)
                                if 0xFE in block:
                                    return bytes(data)
                            page_idx += len(batch_pages)
                            continue
                    except (TypeError, AttributeError):
                        pass
                
                # Fallback to single-block read
                page = self.user_pages[page_idx]
                block = reader.ntag2xx_read_block(page)
                if block:
                    data.extend(block)
                    if 0xFE in block:
                        break
                    page_idx += 1
                else:
                    break
            except (TimeoutError, IOError) as e:
                # Transient errors - log and stop reading
                if hasattr(reader, '_logger'):
                    reader._logger.debug(f"Transient read error at page {page_idx}: {e}")
                break
            except Exception as e:
                # Unexpected errors - log warning
                if hasattr(reader, '_logger'):
                    reader._logger.warning(f"Unexpected error reading page {page_idx}: {e}")
                break
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to NTAG pages."""
        while len(data) % 4 != 0:
            data = data + b"\x00"

        required_pages = len(data) // 4
        if required_pages > len(self.user_pages):
            return False, f"Data too large: needs {required_pages} pages, available {len(self.user_pages)}"

        try:
            for i in range(0, len(data), 4):
                page_num = self.user_pages[i // 4]
                page_data = data[i : i + 4]
                if not reader.ntag2xx_write_block(page_num, page_data):
                    return False, f"Write failed at page {page_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """NTAG21x capacity: ~520 bytes usable."""
        return len(self.user_pages) * 4


class MifareClassicHandler(TagHandler):
    """Handler for Mifare Classic 1K/4K."""

    FIRST_DATA_BLOCK = 4
    MAX_BLOCK = 62
    BLOCK_SIZE = 16
    DEFAULT_KEYS = [
        b'\xFF\xFF\xFF\xFF\xFF\xFF',
        b'\xD3\xF7\xD3\xF7\xD3\xF7',
        b'\xA0\xA1\xA2\xA3\xA4\xA5',
    ]

    def __init__(self, uid: bytes, key_manager=None):
        self.uid = uid
        self.key_manager = key_manager
        self.data_blocks = self._compute_data_blocks()
        self.batch_size = 3

    def _get_keys_to_try(self) -> list:
        """Get list of keys to try: custom keys first, then defaults."""
        keys = []
        if self.key_manager:
            uid_hex = self.uid.hex().upper()
            custom = self.key_manager.get_keys(uid_hex)
            if custom:
                try:
                    keys.append(bytes.fromhex(custom[0]))
                    keys.append(bytes.fromhex(custom[1]))
                except (ValueError, IndexError):
                    pass
        keys.extend(self.DEFAULT_KEYS)
        return keys

    def _compute_data_blocks(self) -> List[int]:
        """Return list of writable data blocks (skip trailer blocks)."""
        blocks = []
        for block in range(self.FIRST_DATA_BLOCK, self.MAX_BLOCK + 1):
            if block % 4 != 3:
                blocks.append(block)
        return blocks

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from Mifare Classic blocks with batching."""
        data = bytearray()
        block_idx = 0
        
        while block_idx < len(self.data_blocks):
            try:
                # Try batch read if explicitly available
                if hasattr(reader, 'mifare_classic_read_blocks'):
                    try:
                        batch_blocks = self.data_blocks[block_idx:block_idx + self.batch_size]
                        blocks = reader.mifare_classic_read_blocks(batch_blocks)
                        if blocks is not None and len(blocks) > 0:
                            for block in blocks:
                                data.extend(block)
                                if 0xFE in block:
                                    return bytes(data)
                            block_idx += len(batch_blocks)
                            continue
                    except (TypeError, AttributeError):
                        pass
                
                # Fallback to single-block read
                block = self.data_blocks[block_idx]
                block_data = reader.mifare_classic_read_block(block)
                if block_data:
                    data.extend(block_data)
                    if 0xFE in block_data:
                        break
                    block_idx += 1
                else:
                    break
            except Exception:
                break
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to Mifare Classic blocks."""
        while len(data) % self.BLOCK_SIZE != 0:
            data = data + b"\x00"

        required_blocks = len(data) // self.BLOCK_SIZE
        if required_blocks > len(self.data_blocks):
            return False, f"Data too large: needs {required_blocks} blocks, available {len(self.data_blocks)}"

        try:
            for i in range(0, len(data), self.BLOCK_SIZE):
                block_num = self.data_blocks[i // self.BLOCK_SIZE]
                block_data = data[i : i + self.BLOCK_SIZE]
                if not reader.mifare_classic_write_block(block_num, block_data):
                    return False, f"Write failed at block {block_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """Mifare Classic 1K capacity: ~176 bytes usable."""
        return len(self.data_blocks) * self.BLOCK_SIZE

    def get_sector_info(self, reader) -> List[dict]:
        """Get lock status for all sectors.
        
        Returns list of dicts with sector number, blocks, and lock status.
        """
        sectors = []
        for sector in range(16):  # Mifare Classic 1K has 16 sectors
            trailer_block = sector * 4 + 3
            first_block = sector * 4
            
            sector_info = {
                "sector": sector,
                "first_block": first_block,
                "trailer_block": trailer_block,
                "locked": False,
                "readable": False,
                "writable": False,
            }
            
            # Try to authenticate and read trailer
            keys = self._get_keys_to_try()
            authenticated = False
            
            for key in keys:
                try:
                    if reader.mifare_classic_authenticate_block(self.uid, trailer_block, 0x60, key):
                        authenticated = True
                        # Read trailer block to check access bits
                        trailer = reader.mifare_classic_read_block(trailer_block)
                        if trailer and len(trailer) >= 16:
                            # Access bits are in bytes 6-8
                            # Simplified: if we can read, mark as readable
                            sector_info["readable"] = True
                            # Try to write to test block to check writability
                            test_block = first_block if first_block != 0 else first_block + 1
                            if test_block % 4 != 3:  # Skip trailer
                                try:
                                    original = reader.mifare_classic_read_block(test_block)
                                    if original:
                                        # Try writing same data back
                                        if reader.mifare_classic_write_block(test_block, original):
                                            sector_info["writable"] = True
                                except Exception:
                                    pass
                        break
                except Exception:
                    continue
            
            if not authenticated:
                sector_info["locked"] = True
            
            sectors.append(sector_info)
        
        return sectors

    def lock_sector(self, reader, sector: int, key_a: bytes, key_b: bytes) -> Tuple[bool, Optional[str]]:
        """Lock a sector by setting access bits to read-only.
        
        Args:
            reader: NFC reader instance
            sector: Sector number (0-15)
            key_a: Key A for authentication (6 bytes)
            key_b: Key B for authentication (6 bytes)
            
        Returns:
            (success, error_message)
        """
        if sector < 0 or sector > 15:
            return False, f"Invalid sector {sector}, must be 0-15"
        
        trailer_block = sector * 4 + 3
        
        # Try to authenticate with provided keys
        try:
            if not reader.mifare_classic_authenticate_block(self.uid, trailer_block, 0x60, key_a):
                return False, f"Authentication failed for sector {sector}"
        except Exception as e:
            return False, f"Authentication error: {e}"
        
        # Read current trailer
        try:
            trailer = reader.mifare_classic_read_block(trailer_block)
            if not trailer or len(trailer) < 16:
                return False, "Failed to read trailer block"
        except Exception as e:
            return False, f"Read error: {e}"
        
        # Build new trailer with read-only access bits
        # Access bits format: C1 C2 C3 (bytes 6-8)
        # For read-only: 0x78 0x77 0x88 (simplified)
        new_trailer = bytearray(trailer)
        new_trailer[0:6] = key_a  # Key A
        new_trailer[6:9] = bytes([0x78, 0x77, 0x88])  # Access bits (read-only)
        new_trailer[9] = 0x69  # GPB
        new_trailer[10:16] = key_b  # Key B
        
        # Write new trailer
        try:
            if not reader.mifare_classic_write_block(trailer_block, bytes(new_trailer)):
                return False, "Failed to write trailer block"
        except Exception as e:
            return False, f"Write error: {e}"
        
        return True, None


class UltralightHandler(TagHandler):
    """Handler for Mifare Ultralight / NTAG21x variants."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.user_pages = list(range(4, 16))
        self.batch_size = 4

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from Ultralight pages."""
        data = bytearray()
        for page in self.user_pages:
            try:
                block = reader.ntag2xx_read_block(page)
                if block:
                    data.extend(block)
                    if 0xFE in block:
                        break
                else:
                    break
            except Exception:
                break
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to Ultralight pages."""
        while len(data) % 4 != 0:
            data = data + b"\x00"

        required_pages = len(data) // 4
        if required_pages > len(self.user_pages):
            return False, f"Data too large: needs {required_pages} pages, available {len(self.user_pages)}"

        try:
            for i in range(0, len(data), 4):
                page_num = self.user_pages[i // 4]
                page_data = data[i : i + 4]
                if not reader.ntag2xx_write_block(page_num, page_data):
                    return False, f"Write failed at page {page_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """Ultralight capacity: ~48 bytes usable."""
        return len(self.user_pages) * 4


class ISO15693Handler(TagHandler):
    """Handler for ISO-15693 / NFC-V tags."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.block_size = 4
        self.max_blocks = 512

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from ISO-15693 blocks via transceive."""
        data = bytearray()
        try:
            for block_num in range(self.max_blocks):
                cmd = bytes([0x20, 0x21, block_num])
                response = reader.transceive(cmd, timeout=0.1)
                if response and len(response) >= self.block_size:
                    block_data = response[:self.block_size]
                    data.extend(block_data)
                    if 0xFE in block_data:
                        break
                else:
                    break
        except Exception:
            pass
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to ISO-15693 blocks via transceive."""
        while len(data) % self.block_size != 0:
            data = data + b"\x00"

        required_blocks = len(data) // self.block_size
        if required_blocks > self.max_blocks:
            return False, f"Data too large: needs {required_blocks} blocks, available {self.max_blocks}"

        try:
            for i in range(0, len(data), self.block_size):
                block_num = i // self.block_size
                block_data = data[i : i + self.block_size]
                cmd = bytes([0x20, 0x21, block_num]) + block_data
                response = reader.transceive(cmd, timeout=0.1)
                if not response:
                    return False, f"Write failed at block {block_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """ISO-15693 capacity: ~2KB."""
        return self.max_blocks * self.block_size


class FeliCaHandler(TagHandler):
    """Handler for FeliCa / NFC-F tags."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.block_size = 16
        self.max_blocks = 16

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from FeliCa blocks via transceive."""
        data = bytearray()
        try:
            for block_num in range(self.max_blocks):
                cmd = bytes([0x06, block_num])
                response = reader.transceive(cmd, timeout=0.1)
                if response and len(response) >= self.block_size:
                    block_data = response[:self.block_size]
                    data.extend(block_data)
                    if 0xFE in block_data:
                        break
                else:
                    break
        except Exception:
            pass
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to FeliCa blocks via transceive."""
        while len(data) % self.block_size != 0:
            data = data + b"\x00"

        required_blocks = len(data) // self.block_size
        if required_blocks > self.max_blocks:
            return False, f"Data too large: needs {required_blocks} blocks, available {self.max_blocks}"

        try:
            for i in range(0, len(data), self.block_size):
                block_num = i // self.block_size
                block_data = data[i : i + self.block_size]
                cmd = bytes([0x08, block_num]) + block_data
                response = reader.transceive(cmd, timeout=0.1)
                if not response:
                    return False, f"Write failed at block {block_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """FeliCa capacity: ~256 bytes."""
        return self.max_blocks * self.block_size


class ISO14443BHandler(TagHandler):
    """Handler for ISO-14443B tags."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.block_size = 4
        self.max_blocks = 256

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from ISO-14443B blocks via transceive."""
        data = bytearray()
        try:
            for block_num in range(self.max_blocks):
                cmd = bytes([0x30, block_num])
                response = reader.transceive(cmd, timeout=0.1)
                if response and len(response) >= self.block_size:
                    block_data = response[:self.block_size]
                    data.extend(block_data)
                    if 0xFE in block_data:
                        break
                else:
                    break
        except Exception:
            pass
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to ISO-14443B blocks via transceive."""
        while len(data) % self.block_size != 0:
            data = data + b"\x00"

        required_blocks = len(data) // self.block_size
        if required_blocks > self.max_blocks:
            return False, f"Data too large: needs {required_blocks} blocks, available {self.max_blocks}"

        try:
            for i in range(0, len(data), self.block_size):
                block_num = i // self.block_size
                block_data = data[i : i + self.block_size]
                cmd = bytes([0xA2, block_num]) + block_data
                response = reader.transceive(cmd, timeout=0.1)
                if not response:
                    return False, f"Write failed at block {block_num}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """ISO-14443B capacity: ~1KB."""
        return self.max_blocks * self.block_size


class DESFireHandler(TagHandler):
    """Handler for DESFire tags."""

    def __init__(self, uid: bytes):
        self.uid = uid
        self.block_size = 16
        self.max_blocks = 256

    def read_ndef(self, reader) -> bytes:
        """Read NDEF data from DESFire file via transceive."""
        data = bytearray()
        try:
            select_cmd = bytes([0x51, 0x02])
            reader.transceive(select_cmd, timeout=0.1)
            
            for offset in range(0, self.max_blocks * self.block_size, self.block_size):
                offset_bytes = offset.to_bytes(3, 'little')
                length_bytes = self.block_size.to_bytes(3, 'little')
                read_cmd = bytes([0x3D]) + offset_bytes + length_bytes
                response = reader.transceive(read_cmd, timeout=0.1)
                if response and len(response) >= self.block_size:
                    block_data = response[:self.block_size]
                    data.extend(block_data)
                    if 0xFE in block_data:
                        break
                else:
                    break
        except Exception:
            pass
        return bytes(data)

    def write_ndef(self, reader, data: bytes) -> Tuple[bool, Optional[str]]:
        """Write NDEF data to DESFire file via transceive."""
        while len(data) % self.block_size != 0:
            data = data + b"\x00"

        required_blocks = len(data) // self.block_size
        if required_blocks > self.max_blocks:
            return False, f"Data too large: needs {required_blocks} blocks, available {self.max_blocks}"

        try:
            select_cmd = bytes([0x51, 0x02])
            reader.transceive(select_cmd, timeout=0.1)
            
            for i in range(0, len(data), self.block_size):
                offset = i
                block_data = data[i : i + self.block_size]
                offset_bytes = offset.to_bytes(3, 'little')
                length_bytes = len(block_data).to_bytes(3, 'little')
                write_cmd = bytes([0x3D]) + offset_bytes + length_bytes + block_data
                response = reader.transceive(write_cmd, timeout=0.1)
                if not response:
                    return False, f"Write failed at offset {offset}"
            return True, None
        except Exception as e:
            return False, str(e)

    def get_capacity(self) -> int:
        """DESFire capacity: ~4KB."""
        return self.max_blocks * self.block_size


def get_handler(tag_type: str, uid: bytes, key_manager=None) -> Optional[TagHandler]:
    """Factory function to get the appropriate handler for a tag type.
    
    Args:
        tag_type: Type of tag (e.g. 'mifare-classic', 'ntag21x')
        uid: Tag UID bytes
        key_manager: Optional KeyManager for Mifare Classic custom keys
    """
    if tag_type == "mifare-classic":
        return MifareClassicHandler(uid, key_manager)
    
    handlers = {
        "ntag21x": NTAGHandler,
        "ultralight": UltralightHandler,
        "iso14443b": ISO14443BHandler,
        "iso15693": ISO15693Handler,
        "felica": FeliCaHandler,
        "desfire": DESFireHandler,
    }
    handler_class = handlers.get(tag_type)
    if handler_class:
        return handler_class(uid)
    return None
