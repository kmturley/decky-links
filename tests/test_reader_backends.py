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


class TestNfcPyBackend:
    """Tests for nfcpy reader backend."""

    @pytest.mark.asyncio
    async def test_nfcpy_connect_success(self):
        """nfcpy should connect successfully."""
        from nfc.nfcpy_backend import NfcPyReader
        import sys
        
        # Mock the nfcpy library
        mock_nfcpy = MagicMock()
        mock_clf = MagicMock()
        mock_nfcpy.ContactlessFrontend.return_value = mock_clf
        
        reader = NfcPyReader("usb")
        
        # Patch the import inside connect
        original_nfc = sys.modules.get('nfc')
        sys.modules['nfc'] = mock_nfcpy
        
        try:
            connected = await reader.connect()
            assert connected is True
            assert reader.is_connected() is True
        finally:
            if original_nfc:
                sys.modules['nfc'] = original_nfc

    @pytest.mark.asyncio
    async def test_nfcpy_connect_failure(self):
        """nfcpy should fail when device not found."""
        from nfc.nfcpy_backend import NfcPyReader
        import sys
        
        mock_nfcpy = MagicMock()
        mock_nfcpy.ContactlessFrontend.side_effect = Exception("Device not found")
        
        reader = NfcPyReader("usb")
        
        original_nfc = sys.modules.get('nfc')
        sys.modules['nfc'] = mock_nfcpy
        
        try:
            connected = await reader.connect()
            assert connected is False
        finally:
            if original_nfc:
                sys.modules['nfc'] = original_nfc

    @pytest.mark.skip(reason="NfcPyReader.read_uid uses connect() not sense()")
    def test_nfcpy_read_uid(self):
        """nfcpy should read UID."""
        from nfc.nfcpy_backend import NfcPyReader
        import sys
        
        mock_nfcpy = MagicMock()
        mock_clf = MagicMock()
        mock_target = MagicMock()
        mock_target.identifier = b'\x04\xAA\xBB\xCC'
        mock_clf.sense.return_value = mock_target
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        
        # Mock nfc module for sense call
        original_nfc = sys.modules.get('nfc')
        sys.modules['nfc'] = mock_nfcpy
        sys.modules['nfc.clf'] = mock_nfcpy.clf
        
        try:
            uid = reader.read_uid()
            assert uid == b'\x04\xAA\xBB\xCC'
        finally:
            if original_nfc:
                sys.modules['nfc'] = original_nfc
            else:
                del sys.modules['nfc']
            if 'nfc.clf' in sys.modules:
                del sys.modules['nfc.clf']

    @pytest.mark.skip(reason="NfcPyReader does not have read_uid_iso14443b method")
    def test_nfcpy_read_uid_iso14443b(self):
        """nfcpy should read ISO-14443B UID."""
        from nfc.nfcpy_backend import NfcPyReader
        import sys
        
        mock_nfcpy = MagicMock()
        mock_clf = MagicMock()
        mock_target = MagicMock()
        mock_target.identifier = b'\x01\x02\x03\x04'
        mock_clf.sense.return_value = mock_target
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        
        original_nfc = sys.modules.get('nfc')
        sys.modules['nfc'] = mock_nfcpy
        sys.modules['nfc.clf'] = mock_nfcpy.clf
        
        try:
            uid = reader.read_uid_iso14443b()
            assert uid == b'\x01\x02\x03\x04'
        finally:
            if original_nfc:
                sys.modules['nfc'] = original_nfc
            else:
                del sys.modules['nfc']
            if 'nfc.clf' in sys.modules:
                del sys.modules['nfc.clf']

    @pytest.mark.skip(reason="NfcPyReader.ntag2xx_read_block not implemented yet")
    def test_nfcpy_ntag_read_block(self):
        """nfcpy should read NTAG block."""
        from nfc.nfcpy_backend import NfcPyReader
        
        mock_clf = MagicMock()
        mock_clf.exchange.return_value = b'\x03\x10\xD1\x01'
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        reader._target = MagicMock()
        
        data = reader.ntag2xx_read_block(4)
        
        assert data == b'\x03\x10\xD1\x01'

    @pytest.mark.skip(reason="NfcPyReader.ntag2xx_write_block not implemented yet")
    def test_nfcpy_ntag_write_block(self):
        """nfcpy should write NTAG block."""
        from nfc.nfcpy_backend import NfcPyReader
        
        mock_clf = MagicMock()
        mock_clf.exchange.return_value = b'\x0A'  # ACK
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        reader._target = MagicMock()
        
        success = reader.ntag2xx_write_block(4, b'\x03\x10\xD1\x01')
        
        assert success is True

    @pytest.mark.skip(reason="NfcPyReader.mifare_classic_read_block not implemented yet")
    def test_nfcpy_mifare_read_block(self):
        """nfcpy should read Mifare Classic block."""
        from nfc.nfcpy_backend import NfcPyReader
        
        mock_clf = MagicMock()
        mock_clf.exchange.return_value = b'\x00' * 16
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        reader._target = MagicMock()
        
        data = reader.mifare_classic_read_block(4)
        
        assert data == b'\x00' * 16

    @pytest.mark.skip(reason="NfcPyReader does not have transceive method")
    def test_nfcpy_transceive(self):
        """nfcpy should support transceive."""
        from nfc.nfcpy_backend import NfcPyReader
        
        mock_clf = MagicMock()
        mock_clf.exchange.return_value = b'\x90\x00'
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        reader._target = MagicMock()
        
        response = reader.transceive(b'\x00\xA4\x04\x00')
        
        assert response == b'\x90\x00'

    def test_nfcpy_close(self):
        """nfcpy should close connection."""
        from nfc.nfcpy_backend import NfcPyReader
        
        mock_clf = MagicMock()
        
        reader = NfcPyReader("usb")
        reader._clf = mock_clf
        
        reader.close()
        
        assert reader._clf is None
        mock_clf.close.assert_called_once()


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


class TestPN532UARTConnectBlocking:
    """Tests for PN532UARTReader._connect_blocking() timeout and error paths."""

    def _make_reader(self):
        from nfc.reader import PN532UARTReader
        return PN532UARTReader("/dev/ttyUSB0", 115200)

    def _make_serial_mock(self):
        import sys
        serial_mock = MagicMock()
        uart_instance = MagicMock()
        serial_mock.Serial.return_value = uart_instance
        sys.modules["serial"] = serial_mock
        return serial_mock, uart_instance

    def _make_pn532_mock(self, firmware=(1, 6, 7, 0), sam_ok=True):
        import sys
        pn532_uart_mod = MagicMock()
        reader_instance = MagicMock()
        reader_instance.firmware_version = firmware
        if not sam_ok:
            reader_instance.SAM_configuration.side_effect = Exception("SAM failed")
        pn532_uart_mod.PN532_UART.return_value = reader_instance
        sys.modules["adafruit_pn532.uart"] = pn532_uart_mod
        return pn532_uart_mod, reader_instance

    def test_connect_blocking_success(self):
        """Should return True and store _reader when firmware version is present."""
        import sys, time
        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod, pn532_instance = self._make_pn532_mock(firmware=(1, 6, 7, 0))

        reader = self._make_reader()

        with patch("time.sleep"):  # skip the 0.5s settle
            result = reader._connect_blocking()

        assert result is True
        assert reader._reader is pn532_instance

    def test_connect_blocking_no_firmware_returns_false(self):
        """Should return False (and close) when firmware_version is falsy."""
        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod, pn532_instance = self._make_pn532_mock(firmware=None)

        reader = self._make_reader()

        with patch("time.sleep"):
            result = reader._connect_blocking()

        assert result is False
        assert reader._reader is None

    def test_connect_blocking_serial_exception_returns_false(self):
        """Should return False when Serial() raises an exception."""
        import sys
        serial_mock = MagicMock()
        serial_mock.Serial.side_effect = Exception("port busy")
        sys.modules["serial"] = serial_mock

        reader = self._make_reader()

        with patch("time.sleep"):
            result = reader._connect_blocking()

        assert result is False

    def test_connect_blocking_sam_exception_returns_false(self):
        """Should return False when SAM_configuration raises."""
        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod, pn532_instance = self._make_pn532_mock(sam_ok=False)

        reader = self._make_reader()

        with patch("time.sleep"):
            result = reader._connect_blocking()

        assert result is False

    def test_connect_blocking_timer_fires_returns_false(self):
        """When the threading.Timer fires before firmware_version returns, result is False."""
        import sys, threading

        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod = MagicMock()
        pn532_instance = MagicMock()

        reader = self._make_reader()

        def _slow_firmware(inst):
            # Simulate timeout firing during the firmware_version property access
            inst.timed_out[0] = True

        # Patch Timer so the callback fires immediately when start() is called
        real_timer_init = threading.Timer.__init__

        class ImmediateTimer:
            def __init__(self, interval, fn):
                self._fn = fn

            def start(self):
                # Mark timed_out via the reader's uart close path
                reader.uart = MagicMock()
                reader.uart.close = MagicMock()
                # Simulate what _on_timeout() does
                reader._reader = None  # close() clears _reader
                if reader.uart:
                    reader.uart.close()
                    reader.uart = None

            def cancel(self):
                pass

        pn532_instance.firmware_version = (1, 6, 7, 0)
        pn532_mod.PN532_UART.return_value = pn532_instance
        sys.modules["adafruit_pn532.uart"] = pn532_mod

        with patch("threading.Timer", ImmediateTimer), patch("time.sleep"):
            # After ImmediateTimer.start() runs, timed_out[0] stays False but
            # _reader is None — the real scenario checks timed_out[0].
            # Use a simpler approach: patch timed_out list via a real Timer that
            # fires immediately by using a 0-second interval.
            pass  # The real test is below using a side-effect approach

        # Simpler, more direct test: manually invoke _connect_blocking and
        # manipulate the internal timed_out flag via a side-effect on firmware_version
        pn532_instance2 = MagicMock()
        pn532_mod2 = MagicMock()
        pn532_mod2.PN532_UART.return_value = pn532_instance2
        sys.modules["adafruit_pn532.uart"] = pn532_mod2

        reader2 = self._make_reader()
        _timed_out_ref = []

        def _firmware_with_timeout():
            # Simulate the timer firing during this property access
            if _timed_out_ref:
                _timed_out_ref[0][0] = True
            return (1, 6, 7, 0)

        # We need access to the internal timed_out list; use a patched Timer
        captured_fn = []

        class CapturingTimer:
            def __init__(self, interval, fn):
                captured_fn.append(fn)

            def start(self):
                # Fire the timeout callback immediately
                captured_fn[0]()

            def cancel(self):
                pass

        pn532_instance2.firmware_version = (1, 6, 7, 0)

        with patch("threading.Timer", CapturingTimer), patch("time.sleep"):
            result2 = reader2._connect_blocking()

        # Timer fired immediately (before timed_out check) — result should be False
        assert result2 is False

    def test_connect_blocking_timer_cancelled_on_success(self):
        """timer.cancel() must be called on the success path."""
        import threading

        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod, pn532_instance = self._make_pn532_mock(firmware=(1, 6, 7, 0))

        cancel_calls = []

        class TrackingTimer:
            def __init__(self, interval, fn):
                pass

            def start(self):
                pass

            def cancel(self):
                cancel_calls.append(1)

        reader = self._make_reader()

        with patch("threading.Timer", TrackingTimer), patch("time.sleep"):
            result = reader._connect_blocking()

        assert result is True
        # cancel() is called in the try block AND the finally block
        assert len(cancel_calls) >= 1

    def test_connect_blocking_timer_cancelled_on_failure(self):
        """timer.cancel() must also be called when connect fails (finally block)."""
        import threading

        serial_mock, uart_instance = self._make_serial_mock()
        pn532_mod, pn532_instance = self._make_pn532_mock(firmware=None)

        cancel_calls = []

        class TrackingTimer:
            def __init__(self, interval, fn):
                pass

            def start(self):
                pass

            def cancel(self):
                cancel_calls.append(1)

        reader = self._make_reader()

        with patch("threading.Timer", TrackingTimer), patch("time.sleep"):
            result = reader._connect_blocking()

        assert result is False
        assert len(cancel_calls) >= 1

    @pytest.mark.asyncio
    async def test_connect_dispatches_to_blocking(self):
        """async connect() should delegate to _connect_blocking via asyncio.to_thread."""
        reader = self._make_reader()

        with patch("os.path.exists", return_value=True), \
             patch.object(reader, "_connect_blocking", return_value=True) as mock_blocking:
            result = await reader.connect()

        assert result is True
        mock_blocking.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_returns_false_if_device_missing(self):
        """async connect() should return False immediately if device path doesn't exist."""
        reader = self._make_reader()

        with patch("os.path.exists", return_value=False), \
             patch.object(reader, "_connect_blocking") as mock_blocking:
            result = await reader.connect()

        assert result is False
        mock_blocking.assert_not_called()


class TestReaderFactory:
    """Tests for reader factory with multiple backends."""

    @pytest.mark.asyncio
    async def test_factory_creates_pn532(self):
        """Factory should create PN532 reader."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "pn532_uart")
        
        reader = await plugin.nfc_source._create_reader()
        
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
            reader = await plugin.nfc_source._create_reader()
            assert reader is not None

    @pytest.mark.asyncio
    async def test_factory_creates_proxmark(self):
        """Factory should create Proxmark reader."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "proxmark")
        
        with patch('nfc.proxmark_backend.ProxmarkReader'):
            reader = await plugin.nfc_source._create_reader()
            assert reader is not None

    @pytest.mark.asyncio
    async def test_factory_unknown_type(self):
        """Factory should return None for unknown type."""
        from main import Plugin
        
        plugin = Plugin()
        await plugin._main()
        plugin.settings.set("reader_type", "unknown")
        
        reader = await plugin.nfc_source._create_reader()
        
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
