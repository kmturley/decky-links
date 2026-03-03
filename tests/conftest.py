"""
conftest.py — shared fixtures and hardware mocks for decky-links unit tests.

All hardware-specific modules (decky, serial, adafruit_pn532, ndef) are mocked
at the sys.modules level *before* main.py is imported, so the test suite runs
without any physical NFC reader or SteamOS environment.
"""
import sys
import os
import asyncio
import types
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# -----------------------------------------------------------------------
# Mock all external/hardware modules BEFORE importing main
# -----------------------------------------------------------------------

def _make_decky_mock():
    m = MagicMock()
    m.DECKY_PLUGIN_DIR          = "/tmp/decky_test"
    m.DECKY_PLUGIN_SETTINGS_DIR = "/tmp/decky_test/settings"
    m.logger                    = MagicMock()
    m.logger.info               = MagicMock()
    m.logger.error              = MagicMock()
    m.logger.warning            = MagicMock()
    m.emit                      = AsyncMock(return_value=None)
    return m


_mock_decky      = _make_decky_mock()
_mock_ndef_mod   = MagicMock()
_mock_serial_mod = types.ModuleType("serial")
_mock_serial_mod.Serial = MagicMock()
_mock_serial_tools_mod = types.ModuleType("serial.tools")
_mock_serial_list_ports_mod = types.ModuleType("serial.tools.list_ports")
_mock_serial_list_ports_mod.comports = MagicMock(return_value=[])
_mock_serial_tools_mod.list_ports = _mock_serial_list_ports_mod
_mock_serial_mod.tools = _mock_serial_tools_mod

_mock_pn532_pkg = types.ModuleType("adafruit_pn532")
_mock_pn532_uart = types.ModuleType("adafruit_pn532.uart")
_mock_pn532_uart.PN532_UART = MagicMock()
_mock_pn532_pkg.uart = _mock_pn532_uart

sys.modules.setdefault("decky",                _mock_decky)
sys.modules.setdefault("serial",               _mock_serial_mod)
sys.modules.setdefault("serial.tools",         _mock_serial_tools_mod)
sys.modules.setdefault("serial.tools.list_ports", _mock_serial_list_ports_mod)
sys.modules.setdefault("ndef",                 _mock_ndef_mod)
sys.modules.setdefault("adafruit_pn532",       _mock_pn532_pkg)
sys.modules.setdefault("adafruit_pn532.uart",  _mock_pn532_uart)


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_decky_mocks():
    """Reset call tracking on the shared decky mock between tests."""
    _mock_decky.emit.reset_mock()
    _mock_decky.logger.info.reset_mock()
    _mock_decky.logger.error.reset_mock()
    _mock_decky.logger.warning.reset_mock()
    yield


@pytest.fixture
def mock_decky():
    return _mock_decky


@pytest.fixture
def plugin(tmp_path):
    """
    Return a Plugin instance ready for unit testing.

    Hardware dependencies (reader, uart) are MagicMock objects.
    The polling task is NOT started — individual methods are called directly.
    """
    # Import after mocks are in place
    from main import Plugin, PluginState, SettingsManager

    p = Plugin()

    # Settings mock — returns sensible defaults
    _settings = {
        "device_path":      "/dev/ttyUSB0",
        "baudrate":         115200,
        "polling_interval": 0.5,
        "auto_launch":      True,
        "auto_close":       False,
    }
    mock_settings        = MagicMock(spec=SettingsManager)
    mock_settings.get    = lambda k: _settings.get(k)
    mock_settings.set    = lambda k, v: _settings.update({k: v})
    mock_settings.settings = _settings
    p.settings = mock_settings

    # Hardware mocks
    p.reader = MagicMock()
    p.uart   = MagicMock()

    # Plugin state
    p.state           = PluginState.READY
    p.is_pairing      = False
    p.pairing_uri     = None
    p.running_game_id = None
    p.current_tag_uid = None
    p.current_tag_uri = None

    return p


@pytest.fixture
def uid_bytes():
    """A representative 4-byte NFC UID."""
    return bytes([0xDE, 0xAD, 0xBE, 0xEF])


@pytest.fixture
def uid_hex(uid_bytes):
    return uid_bytes.hex().upper()
