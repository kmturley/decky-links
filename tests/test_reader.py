import os
import sys
import pytest
from unittest.mock import MagicMock

from nfc.reader import PN532UARTReader, Reader


class DummySerial:
    def __init__(self, *args, **kwargs):
        pass
    def close(self):
        pass


class DummyPN532:
    def __init__(self, uart, debug=False):
        self.uart = uart
        self.debug = debug
        # pretend firmware version exists
        self.firmware_version = (0x32, 1, 2, 3)
    def SAM_configuration(self):
        self._configured = True
    def read_passive_target(self, timeout=0.2):
        return b"\xAA\xBB"
    def ntag2xx_read_block(self, page):
        return b"BLOB"


@pytest.fixture(autouse=True)
def patch_hardware_modules(monkeypatch):
    """Monkey-patch the serial and PN532 modules with dummies."""
    monkeypatch.setattr("serial.Serial", DummySerial, raising=False)

    fake_pkg = MagicMock()
    fake_pkg.PN532_UART = DummyPN532
    monkeypatch.setitem(sys.modules, "adafruit_pn532.uart", fake_pkg)
    yield


@ pytest.mark.asyncio
async def test_connect_success(tmp_path):
    device = tmp_path / "ttyUSB0"
    device.write_text("")

    reader = PN532UARTReader(str(device), baudrate=115200)
    ok = await reader.connect()
    assert ok is True
    assert reader.is_connected()
    assert reader.firmware_version() == (0x32, 1, 2, 3)


@ pytest.mark.asyncio
async def test_connect_missing_path(tmp_path):
    reader = PN532UARTReader(str(tmp_path / "nope"), baudrate=115200)
    ok = await reader.connect()
    assert ok is False
    assert not reader.is_connected()


@ pytest.mark.asyncio
async def test_read_uid_and_delegation(tmp_path):
    device = tmp_path / "ttyUSB1"
    device.write_text("")
    reader = PN532UARTReader(str(device), baudrate=115200)
    await reader.connect()
    uid = reader.read_uid(timeout=0.1)
    assert uid == b"\xAA\xBB"
    # ensure attribute delegation works
    assert reader.ntag2xx_read_block(5) == b"BLOB"


@ pytest.mark.asyncio
async def test_close_discards_reader(tmp_path):
    device = tmp_path / "ttyUSB2"
    device.write_text("")
    reader = PN532UARTReader(str(device), baudrate=115200)
    await reader.connect()
    reader.close()
    assert not reader.is_connected()
    assert reader.firmware_version() is None
