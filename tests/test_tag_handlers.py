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


class TestHandlerFactory:
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
