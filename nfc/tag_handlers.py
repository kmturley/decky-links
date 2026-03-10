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
            except Exception:
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
        "iso15693": ISO15693Handler,
        "felica": FeliCaHandler,
        "desfire": DESFireHandler,
    }
    handler_class = handlers.get(tag_type)
    if handler_class:
        return handler_class(uid)
    return None
