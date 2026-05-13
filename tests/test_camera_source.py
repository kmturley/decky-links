"""
test_camera_source.py — unit tests for CameraSource.

All hardware-level dependencies (ffmpeg subprocess, pyzbar, Pillow, /dev/video*)
are mocked so the suite runs on any platform.
"""
import asyncio
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    from sources.camera_source import CameraSource
    return CameraSource(settings or {}, logger=MagicMock())


def _make_pyzbar_symbol(data: bytes, sym_type: str = "QRCODE"):
    sym = MagicMock()
    sym.type = sym_type
    sym.data = data
    return sym


def _stub_pyzbar_and_pil(monkeypatch, decoded_symbols=None):
    """Install minimal pyzbar and PIL stubs into sys.modules."""
    mock_pyzbar_mod = MagicMock()
    mock_pyzbar_mod.decode.return_value = decoded_symbols or []

    mock_pyzbar_pkg = MagicMock()
    mock_pyzbar_pkg.pyzbar = mock_pyzbar_mod

    mock_image_cls = MagicMock()
    mock_pil_pkg = MagicMock()
    mock_pil_pkg.Image = MagicMock()

    monkeypatch.setitem(sys.modules, "pyzbar", mock_pyzbar_pkg)
    monkeypatch.setitem(sys.modules, "pyzbar.pyzbar", mock_pyzbar_mod)
    monkeypatch.setitem(sys.modules, "PIL", mock_pil_pkg)
    monkeypatch.setitem(sys.modules, "PIL.Image", mock_pil_pkg.Image)

    return mock_pyzbar_mod, mock_pil_pkg


# ── source_id / poll_interval ─────────────────────────────────────────────────

class TestProperties:

    def test_source_id_includes_device(self):
        src = _make_source({"device": "/dev/video1"})
        assert src.source_id == "camera:/dev/video1"

    def test_source_id_default_device(self):
        src = _make_source()
        assert src.source_id == "camera:/dev/video0"

    def test_poll_interval_from_settings(self):
        src = _make_source({"poll_interval": 2.0})
        assert src.poll_interval == 2.0

    def test_poll_interval_defaults_to_one(self):
        src = _make_source()
        assert src.poll_interval == 1.0

    def test_poll_interval_clamps_out_of_range(self):
        src = _make_source({"poll_interval": 99.0})
        assert src.poll_interval == 1.0

    def test_poll_interval_clamps_too_small(self):
        src = _make_source({"poll_interval": 0.001})
        assert src.poll_interval == 1.0

    def test_poll_interval_invalid_type(self):
        src = _make_source({"poll_interval": "fast"})
        assert src.poll_interval == 1.0


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:

    @pytest.mark.asyncio
    async def test_start_returns_false_when_device_missing(self):
        src = _make_source({"device": "/dev/video99"})
        with patch("os.path.exists", return_value=False):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_pyzbar_missing(self):
        src = _make_source()
        with patch("os.path.exists", return_value=True):
            with patch.dict(sys.modules, {"pyzbar": None, "pyzbar.pyzbar": None}):
                ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_pillow_missing(self):
        src = _make_source()
        mock_pyzbar = MagicMock()
        with patch("os.path.exists", return_value=True):
            with patch.dict(sys.modules, {"pyzbar": mock_pyzbar,
                                          "pyzbar.pyzbar": mock_pyzbar.pyzbar,
                                          "PIL": None, "PIL.Image": None}):
                ok = await src.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_start_returns_true_when_ready(self, monkeypatch):
        src = _make_source()
        _stub_pyzbar_and_pil(monkeypatch)
        with patch("os.path.exists", return_value=True):
            ok = await src.start()
        assert ok is True
        assert src.is_active()

    @pytest.mark.asyncio
    async def test_start_logs_ready(self, monkeypatch):
        src = _make_source({"device": "/dev/video0"})
        _stub_pyzbar_and_pil(monkeypatch)
        with patch("os.path.exists", return_value=True):
            await src.start()
        src._logger.info.assert_called()


# ── stop() ────────────────────────────────────────────────────────────────────

class TestStop:

    @pytest.mark.asyncio
    async def test_stop_clears_active(self):
        src = _make_source()
        src._active = True
        await src.stop()
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_stop_clears_qr_state(self):
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/1"
        src._missing_count = 2
        await src.stop()
        assert src._last_qr is None
        assert src._missing_count == 0


# ── poll() ────────────────────────────────────────────────────────────────────

class TestPoll:

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_inactive(self):
        src = _make_source()
        assert not src.is_active()
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_marks_inactive_on_capture_failure(self):
        src = _make_source()
        src._active = True
        with patch.object(src, "_capture_frame", return_value=None):
            result = await src.poll()
        assert result is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_no_qr_in_frame(self):
        src = _make_source()
        src._active = True
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=None):
                result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_emits_load_on_new_qr(self):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        src._active = True
        mock_frame = MagicMock()
        uri = "steam://run/12345"
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=uri):
                result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.LOAD
        assert result.source_type == SourceType.CAMERA
        assert result.uri == uri
        assert result.media_id == uri
        assert src._last_qr == uri

    @pytest.mark.asyncio
    async def test_poll_returns_none_for_same_qr(self):
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/12345"
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value="steam://run/12345"):
                result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_emits_load_for_different_qr(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/111"
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value="steam://run/222"):
                result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.LOAD
        assert result.uri == "steam://run/222"

    @pytest.mark.asyncio
    async def test_poll_debounces_removal(self):
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/12345"
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=None):
                # First two misses — no event
                result1 = await src.poll()
                result2 = await src.poll()
        assert result1 is None
        assert result2 is None
        assert src._missing_count == 2
        assert src._last_qr == "steam://run/12345"

    @pytest.mark.asyncio
    async def test_poll_emits_unload_after_debounce_threshold(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/12345"
        src._missing_count = CameraSource_DEBOUNCE - 1
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=None):
                result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.UNLOAD
        assert result.uri == "steam://run/12345"
        assert src._last_qr is None
        assert src._missing_count == 0

    @pytest.mark.asyncio
    async def test_poll_closes_frame_after_decode(self):
        src = _make_source()
        src._active = True
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=None):
                await src.poll()
        mock_frame.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_closes_frame_even_if_decode_raises(self):
        src = _make_source()
        src._active = True
        mock_frame = MagicMock()
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", side_effect=RuntimeError("boom")):
                with pytest.raises(RuntimeError):
                    await src.poll()
        mock_frame.close.assert_called_once()


# Import the DEBOUNCE_THRESHOLD constant without importing at module level
def _get_debounce():
    from sources.camera_source import CameraSource
    return CameraSource.DEBOUNCE_THRESHOLD

CameraSource_DEBOUNCE = _get_debounce()


# ── _capture_frame() ──────────────────────────────────────────────────────────

class TestCaptureFrame:

    def test_successful_capture_returns_image(self, monkeypatch, tmp_path):
        src = _make_source({"device": "/dev/video0"})
        mock_pyzbar, mock_pil = _stub_pyzbar_and_pil(monkeypatch)

        mock_img = MagicMock()
        mock_pil.Image.open.return_value = mock_img
        mock_img.copy.return_value = mock_img

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = str(tmp_path / "frame.jpg")
                with patch("os.unlink"):
                    result = src._capture_frame()

        assert result is mock_img

    def test_ffmpeg_failure_returns_none(self, monkeypatch, tmp_path):
        src = _make_source({"device": "/dev/video0"})
        _stub_pyzbar_and_pil(monkeypatch)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr=b"Input/output error"
            )
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = str(tmp_path / "frame.jpg")
                with patch("os.unlink"):
                    result = src._capture_frame()

        assert result is None

    def test_subprocess_exception_returns_none(self, monkeypatch, tmp_path):
        src = _make_source({"device": "/dev/video0"})
        _stub_pyzbar_and_pil(monkeypatch)

        with patch("subprocess.run", side_effect=Exception("timeout")):
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = str(tmp_path / "frame.jpg")
                with patch("os.unlink"):
                    result = src._capture_frame()

        assert result is None

    def test_ffmpeg_called_with_v4l2_and_device(self, monkeypatch, tmp_path):
        src = _make_source({"device": "/dev/video2"})
        mock_pyzbar, mock_pil = _stub_pyzbar_and_pil(monkeypatch)
        mock_img = MagicMock()
        mock_pil.Image.open.return_value = mock_img
        mock_img.copy.return_value = mock_img

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = str(tmp_path / "f.jpg")
                with patch("os.unlink"):
                    src._capture_frame()

        cmd = mock_run.call_args[0][0]
        assert "ffmpeg" in cmd
        assert "v4l2" in cmd
        assert "/dev/video2" in cmd

    def test_tempfile_is_deleted_on_success(self, monkeypatch, tmp_path):
        src = _make_source()
        mock_pyzbar, mock_pil = _stub_pyzbar_and_pil(monkeypatch)
        mock_img = MagicMock()
        mock_pil.Image.open.return_value = mock_img
        mock_img.copy.return_value = mock_img
        tmppath = str(tmp_path / "frame.jpg")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = tmppath
                with patch("os.unlink") as mock_unlink:
                    src._capture_frame()

        mock_unlink.assert_called_once_with(tmppath)

    def test_tempfile_is_deleted_on_failure(self, monkeypatch, tmp_path):
        src = _make_source()
        _stub_pyzbar_and_pil(monkeypatch)
        tmppath = str(tmp_path / "frame.jpg")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr=b"error")
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value.name = tmppath
                with patch("os.unlink") as mock_unlink:
                    src._capture_frame()

        mock_unlink.assert_called_once_with(tmppath)


# ── _decode_qr() ──────────────────────────────────────────────────────────────

class TestDecodeQr:

    def test_qr_symbol_returns_uri(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        symbol = _make_pyzbar_symbol(b"steam://run/12345", "QRCODE")
        mock_pyzbar.decode.return_value = [symbol]

        result = src._decode_qr(MagicMock())
        assert result == "steam://run/12345"

    def test_non_qr_symbol_returns_none(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        symbol = _make_pyzbar_symbol(b"1234567890", "EAN13")
        mock_pyzbar.decode.return_value = [symbol]

        result = src._decode_qr(MagicMock())
        assert result is None

    def test_no_symbols_returns_none(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        mock_pyzbar.decode.return_value = []

        result = src._decode_qr(MagicMock())
        assert result is None

    def test_returns_first_qr_symbol(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        s1 = _make_pyzbar_symbol(b"steam://run/1", "QRCODE")
        s2 = _make_pyzbar_symbol(b"steam://run/2", "QRCODE")
        mock_pyzbar.decode.return_value = [s1, s2]

        result = src._decode_qr(MagicMock())
        assert result == "steam://run/1"

    def test_skips_barcode_before_qr(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        barcode = _make_pyzbar_symbol(b"0123456789", "EAN13")
        qr = _make_pyzbar_symbol(b"steam://run/42", "QRCODE")
        mock_pyzbar.decode.return_value = [barcode, qr]

        result = src._decode_qr(MagicMock())
        assert result == "steam://run/42"

    def test_empty_data_skipped(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        empty = _make_pyzbar_symbol(b"   ", "QRCODE")
        mock_pyzbar.decode.return_value = [empty]

        result = src._decode_qr(MagicMock())
        assert result is None

    def test_decode_exception_returns_none(self, monkeypatch):
        src = _make_source()
        mock_pyzbar, _ = _stub_pyzbar_and_pil(monkeypatch)
        mock_pyzbar.decode.side_effect = RuntimeError("pyzbar exploded")

        result = src._decode_qr(MagicMock())
        assert result is None


# ── Debounce integration ───────────────────────────────────────────────────────

class TestDebounce:

    @pytest.mark.asyncio
    async def test_full_debounce_cycle(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        uri = "steam://run/777"
        mock_frame = MagicMock()

        threshold = CameraSource_DEBOUNCE

        # QR arrives → LOAD
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=uri):
                load = await src.poll()
        assert load.kind == MediaEventKind.LOAD

        # (threshold - 1) misses → no event
        for _ in range(threshold - 1):
            with patch.object(src, "_capture_frame", return_value=mock_frame):
                with patch.object(src, "_decode_qr", return_value=None):
                    r = await src.poll()
            assert r is None

        # threshold-th miss → UNLOAD
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=None):
                unload = await src.poll()
        assert unload is not None
        assert unload.kind == MediaEventKind.UNLOAD
        assert unload.uri == uri

    @pytest.mark.asyncio
    async def test_qr_reappears_before_threshold_resets_counter(self):
        src = _make_source()
        src._active = True
        uri = "steam://run/999"
        src._last_qr = uri
        src._missing_count = CameraSource_DEBOUNCE - 1
        mock_frame = MagicMock()

        # QR reappears — counter resets, no UNLOAD
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=uri):
                result = await src.poll()

        assert result is None  # same URI, no new LOAD event
        assert src._missing_count == 0

    @pytest.mark.asyncio
    async def test_no_unload_when_no_prior_qr(self):
        src = _make_source()
        src._active = True
        mock_frame = MagicMock()

        # Poll with empty frame but no prior QR seen
        for _ in range(CameraSource_DEBOUNCE + 1):
            with patch.object(src, "_capture_frame", return_value=mock_frame):
                with patch.object(src, "_decode_qr", return_value=None):
                    result = await src.poll()
            assert result is None


# ── Integration ───────────────────────────────────────────────────────────────

class TestIntegration:

    @pytest.mark.asyncio
    async def test_start_to_qr_detect_to_remove(self, monkeypatch):
        from sources.base import MediaEventKind
        src = _make_source({"device": "/dev/video0"})
        _stub_pyzbar_and_pil(monkeypatch)

        with patch("os.path.exists", return_value=True):
            ok = await src.start()
        assert ok

        uri = "https://store.steampowered.com/app/12345"
        mock_frame = MagicMock()

        # QR scanned → LOAD
        with patch.object(src, "_capture_frame", return_value=mock_frame):
            with patch.object(src, "_decode_qr", return_value=uri):
                load = await src.poll()
        assert load.kind == MediaEventKind.LOAD
        assert load.uri == uri

        # Debounce through to removal
        for _ in range(CameraSource_DEBOUNCE):
            with patch.object(src, "_capture_frame", return_value=mock_frame):
                with patch.object(src, "_decode_qr", return_value=None):
                    unload = await src.poll()

        assert unload.kind == MediaEventKind.UNLOAD
        assert unload.uri == uri

        await src.stop()
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_camera_disconnect_mid_session(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        src._last_qr = "steam://run/1"

        # Simulate capture failing (camera unplugged)
        with patch.object(src, "_capture_frame", return_value=None):
            result = await src.poll()

        assert result is None
        assert not src.is_active()
