"""Tests for tag-type-specific handlers."""

import pytest
from unittest.mock import MagicMock, call
from nfc.tag_handlers import (
    NTAGHandler,
    MifareClassicHandler,
    UltralightHandler,
    ISO15693Handler,
    FeliCaHandler,
    DESFireHandler,
    get_handler,
)
from nfc.key_manager import KeyManager


class TestNTAGHandler:
    """Tests for NTAG21x handler."""

    def test_ntag_read_ndef_success(self):
        """NTAG read should iterate pages and collect data."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_read_block.side_effect = [
            b"\x03\x10\xD1\x01",
            b"\x0C\x55\x65\x78",
            b"\x61\x6D\xFE\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data
        assert reader.ntag2xx_read_block.call_count == 3

    def test_ntag_read_ndef_empty_on_failure(self):
        """NTAG read should return empty bytes on read failure."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_read_block.side_effect = RuntimeError("Read failed")
        
        data = handler.read_ndef(reader)
        
        assert data == b""

    def test_ntag_write_ndef_success(self):
        """NTAG write should pad data and write to pages."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_write_block.return_value = True
        
        data = b"\x03\x10\xD1\x01\x0C\x55"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None
        assert reader.ntag2xx_write_block.call_count == 2

    def test_ntag_write_ndef_too_large(self):
        """NTAG write should reject data exceeding capacity."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        data = b"x" * (130 * 4 + 100)
        
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "too large" in error.lower()

    def test_ntag_write_ndef_failure(self):
        """NTAG write should return error on write failure."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_write_block.return_value = False
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "failed" in error.lower()

    def test_ntag_capacity(self):
        """NTAG capacity should be ~520 bytes."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == 130 * 4


class TestMifareClassicHandler:
    """Tests for Mifare Classic handler."""

    def test_classic_read_ndef_success(self):
        """Classic read should iterate blocks and collect data."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_read_block.side_effect = [
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F",
            b"\x6D\xFE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data

    def test_classic_read_ndef_skips_trailer_blocks(self):
        """Classic read should skip trailer blocks (every 4th block)."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        assert 7 not in handler.data_blocks
        assert 11 not in handler.data_blocks
        assert all((b % 4) != 3 for b in handler.data_blocks)

    def test_classic_write_ndef_success(self):
        """Classic write should pad data and write to blocks."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_write_block.return_value = True
        
        data = b"\x03\x10\xD1\x01\x0C\x55"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None
        assert reader.mifare_classic_write_block.call_count == 1

    def test_classic_write_ndef_too_large(self):
        """Classic write should reject data exceeding capacity."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        data = b"x" * (len(handler.data_blocks) * 16 + 100)
        
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "too large" in error.lower()

    def test_classic_write_ndef_failure(self):
        """Classic write should return error on write failure."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_write_block.return_value = False
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "failed" in error.lower()

    def test_classic_capacity(self):
        """Classic capacity should reflect writable blocks."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == len(handler.data_blocks) * 16
        assert capacity > 0


class TestUltralightHandler:
    """Tests for Mifare Ultralight handler."""

    def test_ultralight_read_ndef_success(self):
        """Ultralight read should iterate pages and collect data."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = UltralightHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_read_block.side_effect = [
            b"\x03\x10\xD1\x01",
            b"\x0C\x55\x65\x78",
            b"\x61\x6D\xFE\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data

    def test_ultralight_write_ndef_success(self):
        """Ultralight write should pad and write to pages."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = UltralightHandler(uid)
        
        reader = MagicMock()
        reader.ntag2xx_write_block.return_value = True
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None

    def test_ultralight_capacity(self):
        """Ultralight capacity should be ~48 bytes."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = UltralightHandler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == 12 * 4


class TestISO15693Handler:
    """Tests for ISO-15693 handler."""

    def test_iso15693_read_ndef_success(self):
        """ISO-15693 read should use transceive to read blocks."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = ISO15693Handler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = [
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78",
            b"\x61\x6D\xFE\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data

    def test_iso15693_read_ndef_empty_on_failure(self):
        """ISO-15693 read should return empty bytes on transceive failure."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = ISO15693Handler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = RuntimeError("Transceive failed")
        
        data = handler.read_ndef(reader)
        
        assert data == b""

    def test_iso15693_write_ndef_success(self):
        """ISO-15693 write should use transceive to write blocks."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = ISO15693Handler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = b"\x00"
        
        data = b"\x03\x10\xD1\x01\x0C\x55"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None

    def test_iso15693_write_ndef_failure(self):
        """ISO-15693 write should return error on transceive failure."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = ISO15693Handler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = None
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "failed" in error.lower()

    def test_iso15693_capacity(self):
        """ISO-15693 capacity should be 2KB."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = ISO15693Handler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == 2048


class TestFeliCaHandler:
    """Tests for FeliCa handler."""

    def test_felica_read_ndef_success(self):
        """FeliCa read should use transceive to read blocks."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = FeliCaHandler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = [
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F",
            b"\x6D\xFE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data

    def test_felica_read_ndef_empty_on_failure(self):
        """FeliCa read should return empty bytes on transceive failure."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = FeliCaHandler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = RuntimeError("Transceive failed")
        
        data = handler.read_ndef(reader)
        
        assert data == b""

    def test_felica_write_ndef_success(self):
        """FeliCa write should use transceive to write blocks."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = FeliCaHandler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = b"\x00"
        
        data = b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None

    def test_felica_write_ndef_failure(self):
        """FeliCa write should return error on transceive failure."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = FeliCaHandler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = None
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "failed" in error.lower()

    def test_felica_capacity(self):
        """FeliCa capacity should be ~256 bytes."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = FeliCaHandler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == 16 * 16


class TestDESFireHandler:
    """Tests for DESFire handler."""

    def test_desfire_read_ndef_success(self):
        """DESFire read should use transceive to read file."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = DESFireHandler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = [
            b"\x00",  # Select file response
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F",
            b"\x6D\xFE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data

    def test_desfire_read_ndef_empty_on_failure(self):
        """DESFire read should return empty bytes on transceive failure."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = DESFireHandler(uid)
        
        reader = MagicMock()
        reader.transceive.side_effect = RuntimeError("Transceive failed")
        
        data = handler.read_ndef(reader)
        
        assert data == b""

    def test_desfire_write_ndef_success(self):
        """DESFire write should use transceive to write file."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = DESFireHandler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = b"\x00"
        
        data = b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F"
        success, error = handler.write_ndef(reader, data)
        
        assert success is True
        assert error is None

    def test_desfire_write_ndef_failure(self):
        """DESFire write should return error on transceive failure."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = DESFireHandler(uid)
        
        reader = MagicMock()
        reader.transceive.return_value = None
        
        data = b"\x03\x10\xD1\x01"
        success, error = handler.write_ndef(reader, data)
        
        assert success is False
        assert "failed" in error.lower()

    def test_desfire_capacity(self):
        """DESFire capacity should be ~4KB."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = DESFireHandler(uid)
        
        capacity = handler.get_capacity()
        
        assert capacity == 256 * 16


class TestMifareClassicKeyManagement:
    """Tests for Mifare Classic custom key management."""

    def test_classic_with_custom_keys(self):
        """Classic handler should use custom keys when provided."""
        uid = b"\xAA\xBB\xCC\xDD"
        uid_hex = uid.hex().upper()
        
        km = KeyManager()
        km.set_key(uid_hex, "A0A1A2A3A4A5", "B0B1B2B3B4B5")
        
        handler = MifareClassicHandler(uid, km)
        keys = handler._get_keys_to_try()
        
        # Custom keys should be first
        assert keys[0] == bytes.fromhex("A0A1A2A3A4A5")
        assert keys[1] == bytes.fromhex("B0B1B2B3B4B5")
        # Default keys should follow
        assert len(keys) > 2

    def test_classic_without_custom_keys(self):
        """Classic handler should use default keys when no custom keys."""
        uid = b"\xAA\xBB\xCC\xDD"
        
        km = KeyManager()
        handler = MifareClassicHandler(uid, km)
        keys = handler._get_keys_to_try()
        
        # Should only have default keys
        assert len(keys) == 3
        assert keys == MifareClassicHandler.DEFAULT_KEYS

    def test_classic_without_key_manager(self):
        """Classic handler should work without key manager."""
        uid = b"\xAA\xBB\xCC\xDD"
        
        handler = MifareClassicHandler(uid)
        keys = handler._get_keys_to_try()
        
        # Should only have default keys
        assert len(keys) == 3
        assert keys == MifareClassicHandler.DEFAULT_KEYS

    def test_classic_invalid_custom_keys_ignored(self):
        """Classic handler should ignore invalid custom keys."""
        uid = b"\xAA\xBB\xCC\xDD"
        uid_hex = uid.hex().upper()
        
        km = KeyManager()
        # Store invalid keys (wrong format)
        km.tag_keys[uid_hex] = ["INVALID", "KEYS"]
        
        handler = MifareClassicHandler(uid, km)
        keys = handler._get_keys_to_try()
        
        # Should fall back to default keys
        assert len(keys) == 3
        assert keys == MifareClassicHandler.DEFAULT_KEYS

    def test_factory_passes_key_manager(self):
        """Factory should pass key manager to Classic handler."""
        uid = b"\xAA\xBB\xCC\xDD"
        uid_hex = uid.hex().upper()
        
        km = KeyManager()
        km.set_key(uid_hex, "A0A1A2A3A4A5", "B0B1B2B3B4B5")
        
        handler = get_handler("mifare-classic", uid, km)
        
        assert isinstance(handler, MifareClassicHandler)
        assert handler.key_manager is km
        keys = handler._get_keys_to_try()
        assert keys[0] == bytes.fromhex("A0A1A2A3A4A5")

    def test_factory_without_key_manager(self):
        """Factory should work without key manager."""
        uid = b"\xAA\xBB\xCC\xDD"
        
        handler = get_handler("mifare-classic", uid)
        
        assert isinstance(handler, MifareClassicHandler)
        assert handler.key_manager is None
        keys = handler._get_keys_to_try()
        assert keys == MifareClassicHandler.DEFAULT_KEYS



    """Tests for get_handler factory function."""

    def test_get_handler_ntag(self):
        """Factory should return NTAGHandler for ntag21x type."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = get_handler("ntag21x", uid)
        
        assert isinstance(handler, NTAGHandler)
        assert handler.uid == uid

    def test_get_handler_classic(self):
        """Factory should return MifareClassicHandler for mifare-classic type."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = get_handler("mifare-classic", uid)
        
        assert isinstance(handler, MifareClassicHandler)
        assert handler.uid == uid

    def test_get_handler_ultralight(self):
        """Factory should return UltralightHandler for ultralight type."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = get_handler("ultralight", uid)
        
        assert isinstance(handler, UltralightHandler)
        assert handler.uid == uid

    def test_get_handler_iso15693(self):
        """Factory should return ISO15693Handler for iso15693 type."""
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        handler = get_handler("iso15693", uid)
        
        assert isinstance(handler, ISO15693Handler)
        assert handler.uid == uid

    def test_get_handler_felica(self):
        """Factory should return FeliCaHandler for felica type."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        handler = get_handler("felica", uid)
        
        assert isinstance(handler, FeliCaHandler)
        assert handler.uid == uid

    def test_get_handler_desfire(self):
        """Factory should return DESFireHandler for desfire type."""
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        handler = get_handler("desfire", uid)
        
        assert isinstance(handler, DESFireHandler)
        assert handler.uid == uid

    def test_get_handler_unknown_type(self):
        """Factory should return None for unknown type."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = get_handler("unknown-type", uid)
        
        assert handler is None

    def test_get_handler_empty_type(self):
        """Factory should return None for empty type."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = get_handler("", uid)
        
        assert handler is None


class TestHandlerIntegration:
    """Integration tests for handlers with different tag types."""

    def test_ntag_and_classic_different_capacities(self):
        """Different handlers should report different capacities."""
        uid = b"\xAA\xBB\xCC\xDD"
        
        ntag_handler = NTAGHandler(uid)
        classic_handler = MifareClassicHandler(uid)
        ultralight_handler = UltralightHandler(uid)
        
        ntag_capacity = ntag_handler.get_capacity()
        classic_capacity = classic_handler.get_capacity()
        ultralight_capacity = ultralight_handler.get_capacity()
        
        assert ultralight_capacity < ntag_capacity
        assert ultralight_capacity < classic_capacity

    def test_handler_read_write_roundtrip(self):
        """Handler should be able to write and read back data."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        test_data = b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65"
        
        reader.ntag2xx_write_block.return_value = True
        success, error = handler.write_ndef(reader, test_data)
        assert success is True
        
        reader.ntag2xx_read_block.side_effect = [
            test_data[:4],
            test_data[4:8],
            test_data[8:12],
            b"\x65\xFE\x00\x00",
        ]
        read_data = handler.read_ndef(reader)
        assert len(read_data) > 0


class TestPerformanceOptimization:
    """Tests for multi-block read/write optimization."""

    def test_ntag_batch_read_when_available(self):
        """NTAG should use batch read if reader supports it."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        # Mock batch read method
        reader.ntag2xx_read_blocks.return_value = [
            b"\x03\x10\xD1\x01",
            b"\x0C\x55\x65\x78",
            b"\x61\x6D\xFE\x00",
            b"\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data
        reader.ntag2xx_read_blocks.assert_called_once()

    def test_ntag_fallback_to_single_read(self):
        """NTAG should fallback to single read if batch not available."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        reader = MagicMock()
        # No batch read method
        del reader.ntag2xx_read_blocks
        reader.ntag2xx_read_block.side_effect = [
            b"\x03\x10\xD1\x01",
            b"\x0C\x55\x65\x78",
            b"\x61\x6D\xFE\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data
        assert reader.ntag2xx_read_block.call_count == 3

    def test_classic_batch_read_when_available(self):
        """Classic should use batch read if reader supports it."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        # Mock batch read method
        reader.mifare_classic_read_blocks.return_value = [
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F",
            b"\x6D\xFE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data
        reader.mifare_classic_read_blocks.assert_called_once()

    def test_classic_fallback_to_single_read(self):
        """Classic should fallback to single read if batch not available."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        # No batch read method
        del reader.mifare_classic_read_blocks
        reader.mifare_classic_read_block.side_effect = [
            b"\x03\x10\xD1\x01\x0C\x55\x65\x78\x61\x6D\x70\x6C\x65\x2E\x63\x6F",
            b"\x6D\xFE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ]
        
        data = handler.read_ndef(reader)
        
        assert len(data) > 0
        assert 0xFE in data
        assert reader.mifare_classic_read_block.call_count == 2

    def test_ntag_batch_size_respected(self):
        """NTAG batch read should respect batch size limit."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = NTAGHandler(uid)
        
        assert handler.batch_size == 4

    def test_classic_batch_size_respected(self):
        """Classic batch read should respect batch size limit."""
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        assert handler.batch_size == 3



class TestSectorLockDetection:
    """Tests for Mifare Classic sector lock detection."""

    def test_get_sector_info_all_unlocked(self):
        """Should detect all sectors as unlocked when authentication succeeds."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        reader.mifare_classic_write_block.return_value = True
        
        sectors = handler.get_sector_info(reader)
        
        assert len(sectors) == 16
        assert all(not s["locked"] for s in sectors)
        assert all(s["readable"] for s in sectors)
        assert all(s["writable"] for s in sectors)

    def test_get_sector_info_some_locked(self):
        """Should detect locked sectors when authentication fails."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        # Sectors 0-7 unlocked, 8-15 locked
        def auth_side_effect(uid, block, key_type, key):
            sector = block // 4
            return sector < 8
        
        reader.mifare_classic_authenticate_block.side_effect = auth_side_effect
        reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        reader.mifare_classic_write_block.return_value = True
        
        sectors = handler.get_sector_info(reader)
        
        assert len(sectors) == 16
        # First 8 sectors unlocked
        assert all(not sectors[i]["locked"] for i in range(8))
        # Last 8 sectors locked
        assert all(sectors[i]["locked"] for i in range(8, 16))

    def test_get_sector_info_read_only(self):
        """Should detect read-only sectors when write fails."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        reader.mifare_classic_write_block.return_value = False  # Write fails
        
        sectors = handler.get_sector_info(reader)
        
        assert len(sectors) == 16
        assert all(not s["locked"] for s in sectors)
        assert all(s["readable"] for s in sectors)
        assert all(not s["writable"] for s in sectors)

    def test_get_sector_info_structure(self):
        """Should return correct sector structure."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        reader.mifare_classic_write_block.return_value = True
        
        sectors = handler.get_sector_info(reader)
        
        # Check first sector structure
        assert sectors[0]["sector"] == 0
        assert sectors[0]["first_block"] == 0
        assert sectors[0]["trailer_block"] == 3
        
        # Check last sector structure
        assert sectors[15]["sector"] == 15
        assert sectors[15]["first_block"] == 60
        assert sectors[15]["trailer_block"] == 63



class TestSectorLocking:
    """Tests for Mifare Classic sector locking."""

    def test_lock_sector_success(self):
        """Should successfully lock a sector."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\\xFF" * 6 + b"\\x00" * 4 + b"\\xFF" * 6
        reader.mifare_classic_write_block.return_value = True
        
        key_a = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        key_b = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        
        success, error = handler.lock_sector(reader, 1, key_a, key_b)
        
        assert success is True
        assert error is None
        reader.mifare_classic_write_block.assert_called_once()

    def test_lock_sector_invalid_sector(self):
        """Should reject invalid sector numbers."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        key_a = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        key_b = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        
        success, error = handler.lock_sector(reader, -1, key_a, key_b)
        assert success is False
        assert "Invalid sector" in error
        
        success, error = handler.lock_sector(reader, 16, key_a, key_b)
        assert success is False
        assert "Invalid sector" in error

    def test_lock_sector_auth_failure(self):
        """Should fail when authentication fails."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = False
        
        key_a = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        key_b = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        
        success, error = handler.lock_sector(reader, 1, key_a, key_b)
        
        assert success is False
        assert "Authentication failed" in error

    def test_lock_sector_read_failure(self):
        """Should fail when trailer read fails."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = None
        
        key_a = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        key_b = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        
        success, error = handler.lock_sector(reader, 1, key_a, key_b)
        
        assert success is False
        assert "Failed to read" in error

    def test_lock_sector_write_failure(self):
        """Should fail when trailer write fails."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xAA\\xBB\\xCC\\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\\xFF" * 16
        reader.mifare_classic_write_block.return_value = False
        
        key_a = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        key_b = b"\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF"
        
        success, error = handler.lock_sector(reader, 1, key_a, key_b)
        
        assert success is False
        assert "Failed to write" in error

    def test_lock_sector_trailer_structure(self):
        """Should write correct trailer structure."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\xAA\xBB\xCC\xDD"
        handler = MifareClassicHandler(uid)
        
        reader = MagicMock()
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\x00" * 16
        reader.mifare_classic_write_block.return_value = True
        
        key_a = b"\xAA\xBB\xCC\xDD\xEE\xFF"
        key_b = b"\x11\x22\x33\x44\x55\x66"
        
        handler.lock_sector(reader, 1, key_a, key_b)
        
        # Check the written trailer
        call_args = reader.mifare_classic_write_block.call_args[0]
        written_trailer = call_args[1]
        
        # Verify structure
        assert written_trailer[0:6] == key_a
        assert written_trailer[6:9] == bytes([0x78, 0x77, 0x88])  # Access bits
        assert written_trailer[10:16] == key_b
