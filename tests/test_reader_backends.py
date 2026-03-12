"""Tests for additional reader backends."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestACR122UBackend:
    """Tests for ACR122U reader backend."""

    @pytest.mark.asyncio
    async def test_acr122u_connect_success(self):
        """ACR122U should connect successfully when reader found."""
        import sys
        mock_smartcard = MagicMock()
        mock_reader = MagicMock()
        mock_reader.__str__ = lambda self: "ACR122U USB Reader"
        mock_connection = MagicMock()
        mock_connection.getATR.return_value = [0x3B, 0x00]
        mock_reader.createConnection.return_value = mock_connection
        mock_smartcard.System.readers.return_value = [mock_reader]
        sys.modules['smartcard'] = mock_smartcard
        sys.modules['smartcard.System'] = mock_smartcard.System
        sys.modules['smartcard.util'] = mock_smartcard.util
        
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        connected = await reader.connect()
        
        assert connected is True
        assert reader.is_connected() is True

    @pytest.mark.asyncio
    async def test_acr122u_connect_no_readers(self):
        """ACR122U should fail when no readers found."""
        import sys
        mock_smartcard = MagicMock()
        mock_smartcard.System.readers.return_value = []
        sys.modules['smartcard'] = mock_smartcard
        sys.modules['smartcard.System'] = mock_smartcard.System
        sys.modules['smartcard.util'] = mock_smartcard.util
        
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        connected = await reader.connect()
        
        assert connected is False
        assert reader.is_connected() is False

    def test_acr122u_read_uid(self):
        """ACR122U should read UID via APDU."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        mock_connection.transmit.return_value = ([0x04, 0xAA, 0xBB, 0xCC], 0x90, 0x00)
        reader._connection = mock_connection
        
        uid = reader.read_uid()
        
        assert uid == b'\x04\xAA\xBB\xCC'

    def test_acr122u_read_uid_failure(self):
        """ACR122U should return None on read failure."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        mock_connection.transmit.return_value = ([], 0x63, 0x00)
        reader._connection = mock_connection
        
        uid = reader.read_uid()
        
        assert uid is None

    def test_acr122u_ntag_read_block(self):
        """ACR122U should read NTAG block."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        mock_connection.transmit.return_value = ([0x03, 0x10, 0xD1, 0x01], 0x90, 0x00)
        reader._connection = mock_connection
        
        data = reader.ntag2xx_read_block(4)
        
        assert data == b'\x03\x10\xD1\x01'

    def test_acr122u_ntag_write_block(self):
        """ACR122U should write NTAG block."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        mock_connection.transmit.return_value = ([], 0x90, 0x00)
        reader._connection = mock_connection
        
        success = reader.ntag2xx_write_block(4, b'\x03\x10\xD1\x01')
        
        assert success is True

    def test_acr122u_mifare_authenticate(self):
        """ACR122U should authenticate Mifare Classic."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        mock_connection.transmit.return_value = ([], 0x90, 0x00)
        reader._connection = mock_connection
        
        uid = b'\xAA\xBB\xCC\xDD'
        key = b'\xFF\xFF\xFF\xFF\xFF\xFF'
        success = reader.mifare_classic_authenticate_block(uid, 4, 0x60, key)
        
        assert success is True

    def test_acr122u_close(self):
        """ACR122U should close connection."""
        from nfc.acr122u_backend import ACR122UReader
        reader = ACR122UReader()
        
        mock_connection = MagicMock()
        reader._connection = mock_connection
        
        reader.close()
        
        assert reader._connection is None
        mock_connection.disconnect.assert_called_once()


class TestProxmarkBackend:
    """Tests for Proxmark3 reader backend."""

    @pytest.mark.asyncio
    async def test_proxmark_connect_success(self):
        """Proxmark should connect successfully."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Proxmark3 v4.0.0"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            
            connected = await reader.connect()
            
            assert connected is True
            assert reader.is_connected() is True

    @pytest.mark.asyncio
    async def test_proxmark_connect_failure(self):
        """Proxmark should fail when device not found."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            
            connected = await reader.connect()
            
            assert connected is False

    def test_proxmark_read_uid(self):
        """Proxmark should read UID."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="UID : 04 AA BB CC"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            reader._connected = True
            
            uid = reader.read_uid()
            
            assert uid == b'\x04\xAA\xBB\xCC'

    def test_proxmark_read_uid_iso14443b(self):
        """Proxmark should read ISO-14443B UID."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="UID : 01 02 03 04"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            reader._connected = True
            
            uid = reader.read_uid_iso14443b()
            
            assert uid == b'\x01\x02\x03\x04'

    def test_proxmark_ntag_read_block(self):
        """Proxmark should read NTAG block."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="0310D101"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            reader._connected = True
            
            data = reader.ntag2xx_read_block(4)
            
            assert data == b'\x03\x10\xD1\x01'

    def test_proxmark_ntag_write_block(self):
        """Proxmark should write NTAG block."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Write success"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            reader._connected = True
            
            success = reader.ntag2xx_write_block(4, b'\x03\x10\xD1\x01')
            
            assert success is True

    def test_proxmark_firmware_version(self):
        """Proxmark should return firmware version."""
        with patch('nfc.proxmark_backend.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Proxmark3 v4.2.1"
            )
            
            from nfc.proxmark_backend import ProxmarkReader
            reader = ProxmarkReader()
            reader._connected = True
            
            version = reader.firmware_version()
            
            assert version == (4, 2, 1, 0)

    def test_proxmark_close(self):
        """Proxmark should close connection."""
        from nfc.proxmark_backend import ProxmarkReader
        reader = ProxmarkReader()
        reader._connected = True
        
        reader.close()
        
        assert reader._connected is False


class TestReaderFactory:
    """Tests for reader factory with multiple backends."""

    @pytest.mark.asyncio
    async def test_factory_creates_pn532(self):
        """Factory should create PN532 reader."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "pn532_uart")
        
        reader = await plugin._create_reader()
        
        assert reader is not None
        assert reader.__class__.__name__ == "PN532UARTReader"

    @pytest.mark.asyncio
    async def test_factory_creates_acr122u(self):
        """Factory should create ACR122U reader."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "acr122u")
        
        with patch('nfc.acr122u_backend.ACR122UReader'):
            reader = await plugin._create_reader()
            assert reader is not None

    @pytest.mark.asyncio
    async def test_factory_creates_proxmark(self):
        """Factory should create Proxmark reader."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "proxmark")
        
        with patch('nfc.proxmark_backend.ProxmarkReader'):
            reader = await plugin._create_reader()
            assert reader is not None

    @pytest.mark.asyncio
    async def test_factory_unknown_type(self):
        """Factory should return None for unknown type."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "unknown")
        
        reader = await plugin._create_reader()
        
        assert reader is None

    @pytest.mark.asyncio
    async def test_settings_validation_reader_types(self):
        """Settings should validate reader types."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        
        assert await plugin.set_setting("reader_type", "pn532_uart") is True
        assert await plugin.set_setting("reader_type", "acr122u") is True
        assert await plugin.set_setting("reader_type", "proxmark") is True
        assert await plugin.set_setting("reader_type", "nfcpy") is True
        assert await plugin.set_setting("reader_type", "invalid") is False
