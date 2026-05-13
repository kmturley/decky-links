"""
test_serial_source.py — unit tests for SerialSource.

pyserial is mocked so the suite runs without physical hardware.
"""
import sys
import pytest
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    from sources.serial_source import SerialSource
    defaults = {"enabled": True, "port": "/dev/ttyUSB0", "baudrate": 9600}
    if settings:
        defaults.update(settings)
    return SerialSource(defaults, logger=MagicMock())


def _make_serial_mock(data: bytes = b""):
    """Return a mock serial.Serial instance pre-loaded with data."""
    mock_serial = MagicMock()
    mock_serial.in_waiting = len(data)
    mock_serial.read.return_value = data
    return mock_serial


def _make_pyserial_mod(serial_instance=None):
    """Return a stub pyserial module."""
    mod = MagicMock()
    if serial_instance is not None:
        mod.Serial.return_value = serial_instance
    return mod


# ── source_id / poll_interval ─────────────────────────────────────────────────

class TestProperties:

    def test_source_id_includes_port(self):
        src = _make_source({"port": "/dev/ttyUSB2"})
        assert src.source_id == "serial:/dev/ttyUSB2"

    def test_poll_interval_is_fast(self):
        src = _make_source()
        assert src.poll_interval <= 0.5


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:

    @pytest.mark.asyncio
    async def test_start_returns_false_when_disabled(self):
        src = _make_source({"enabled": False})
        ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_pyserial_missing(self):
        src = _make_source()
        with patch.dict(sys.modules, {"serial": None}):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_opens_serial_port(self):
        src = _make_source({"port": "/dev/ttyUSB0", "baudrate": 115200})
        mock_serial = MagicMock()
        mock_mod = _make_pyserial_mod(mock_serial)
        with patch.dict(sys.modules, {"serial": mock_mod}):
            ok = await src.start()
        assert ok is True
        assert src.is_active()
        mock_mod.Serial.assert_called_once_with("/dev/ttyUSB0", 115200, timeout=0)

    @pytest.mark.asyncio
    async def test_start_returns_false_on_serial_error(self):
        src = _make_source()
        mock_mod = MagicMock()
        mock_mod.Serial.side_effect = Exception("device not found")
        with patch.dict(sys.modules, {"serial": mock_mod}):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_resets_buffer(self):
        src = _make_source()
        src._buffer = "leftover"
        mock_serial = MagicMock()
        mock_mod = _make_pyserial_mod(mock_serial)
        with patch.dict(sys.modules, {"serial": mock_mod}):
            await src.start()
        assert src._buffer == ""


# ── stop() ────────────────────────────────────────────────────────────────────

class TestStop:

    @pytest.mark.asyncio
    async def test_stop_closes_serial(self):
        src = _make_source()
        mock_serial = MagicMock()
        src._serial = mock_serial
        src._active = True
        await src.stop()
        mock_serial.close.assert_called_once()
        assert src._serial is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_stop_clears_buffer(self):
        src = _make_source()
        src._active = True
        src._buffer = "partial data"
        src._serial = MagicMock()
        await src.stop()
        assert src._buffer == ""

    @pytest.mark.asyncio
    async def test_stop_tolerates_close_error(self):
        src = _make_source()
        mock_serial = MagicMock()
        mock_serial.close.side_effect = RuntimeError("already closed")
        src._serial = mock_serial
        src._active = True
        await src.stop()   # should not raise
        assert not src.is_active()


# ── poll() ────────────────────────────────────────────────────────────────────

class TestPoll:

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_inactive(self):
        src = _make_source()
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_no_data(self):
        src = _make_source()
        src._active = True
        src._serial = _make_serial_mock(b"")
        src._serial.in_waiting = 0
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_returns_none_for_partial_line(self):
        src = _make_source()
        src._active = True
        src._serial = _make_serial_mock(b"steam://run/123")
        result = await src.poll()
        assert result is None
        assert src._buffer == "steam://run/123"

    @pytest.mark.asyncio
    async def test_poll_emits_load_for_complete_line(self):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        src._active = True
        src._serial = _make_serial_mock(b"steam://run/999\n")
        result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.LOAD
        assert result.source_type == SourceType.SERIAL
        assert result.uri == "steam://run/999"
        assert result.media_id == "steam://run/999"

    @pytest.mark.asyncio
    async def test_poll_strips_whitespace_from_line(self):
        src = _make_source()
        src._active = True
        src._serial = _make_serial_mock(b"  steam://run/1  \r\n")
        result = await src.poll()
        assert result.uri == "steam://run/1"

    @pytest.mark.asyncio
    async def test_poll_skips_blank_lines(self):
        src = _make_source()
        src._active = True
        src._serial = _make_serial_mock(b"\n")
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_accumulates_partial_then_complete(self):
        src = _make_source()
        src._active = True
        mock_serial = MagicMock()
        src._serial = mock_serial

        # First read: partial line
        mock_serial.in_waiting = len(b"steam://run/")
        mock_serial.read.return_value = b"steam://run/"
        r1 = await src.poll()
        assert r1 is None

        # Second read: rest of line + newline
        mock_serial.in_waiting = len(b"42\n")
        mock_serial.read.return_value = b"42\n"
        r2 = await src.poll()
        assert r2 is not None
        assert r2.uri == "steam://run/42"

    @pytest.mark.asyncio
    async def test_poll_marks_inactive_on_read_error(self):
        src = _make_source()
        src._active = True
        mock_serial = MagicMock()
        mock_serial.in_waiting = 5
        mock_serial.read.side_effect = OSError("I/O error")
        src._serial = mock_serial
        result = await src.poll()
        assert result is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_poll_returns_first_line_when_buffer_has_multiple(self):
        src = _make_source()
        src._active = True
        src._buffer = "steam://run/1\nsteam://run/2\n"
        src._serial = _make_serial_mock(b"")
        src._serial.in_waiting = 0
        result = await src.poll()
        assert result.uri == "steam://run/1"
        assert "steam://run/2" in src._buffer


# ── Integration ───────────────────────────────────────────────────────────────

class TestIntegration:

    @pytest.mark.asyncio
    async def test_start_then_receive_lines(self):
        from sources.base import MediaEventKind
        src = _make_source({"port": "/dev/ttyUSB0", "baudrate": 9600})
        mock_serial = MagicMock()
        mock_mod = _make_pyserial_mod(mock_serial)

        with patch.dict(sys.modules, {"serial": mock_mod}):
            ok = await src.start()
        assert ok

        # Feed two lines in separate reads
        mock_serial.in_waiting = len(b"steam://run/10\n")
        mock_serial.read.return_value = b"steam://run/10\n"
        ev1 = await src.poll()
        assert ev1.kind == MediaEventKind.LOAD
        assert ev1.uri == "steam://run/10"

        mock_serial.in_waiting = len(b"steam://run/20\n")
        mock_serial.read.return_value = b"steam://run/20\n"
        ev2 = await src.poll()
        assert ev2.uri == "steam://run/20"

        await src.stop()
        assert not src.is_active()
