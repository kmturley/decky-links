"""Camera media source — USB webcam QR code scanning.

Captures frames from a USB webcam (``/dev/video*``) using ffmpeg,
decodes QR codes via ``zxing-cpp`` and ``Pillow``, and emits MediaEvents
for QR code arrival and departure.

Gracefully degrades when zxing-cpp/Pillow are not installed or no camera
device is present — ``start()`` returns False and the source stays inactive.

Capture strategy: one JPEG frame per :attr:`poll_interval` (default 1 s).
QR removal is debounced over :attr:`DEBOUNCE_THRESHOLD` consecutive empty
frames to avoid flicker when the code briefly leaves the sensor.
"""

import asyncio
import os
import subprocess
import tempfile
import traceback
from typing import Optional

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceType,
)


class CameraSource(MediaSource):
    """USB webcam QR-code scanning source.

    Captures frames at :attr:`poll_interval` using ffmpeg, decodes QR codes
    via zxing-cpp, and emits LOAD/UNLOAD events for QR code arrival/departure.
    """

    source_type = SourceType.CAMERA

    DEBOUNCE_THRESHOLD = 3  # consecutive empty frames before UNLOAD is emitted

    # Consecutive *capture* failures before the source is torn down and
    # restarted. A single dropped frame is normal for a webcam — USB bandwidth
    # contention, the device busy with autoexposure — and tearing down on one
    # of them cost a full stop/start plus an ffmpeg spawn to recover from
    # something that would have fixed itself on the next poll.
    CAPTURE_FAILURE_THRESHOLD = 3

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._device: str = settings.get("device", "/dev/video0")
        self._active: bool = False
        self._last_qr: Optional[str] = None   # URI of currently-visible QR code
        self._missing_count: int = 0
        self._capture_failures: int = 0

    @property
    def source_id(self) -> str:
        return f"camera:{self._device}"

    @property
    def poll_interval(self) -> float:
        try:
            v = float(self._settings.get("poll_interval", 1.0))
            return v if 0.1 <= v <= 10.0 else 1.0
        except (TypeError, ValueError):
            return 1.0

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Verify device presence and that zxing-cpp/Pillow are importable."""
        if not os.path.exists(self._device):
            return False
        # Imported one at a time, and the real exception is logged. Reporting
        # "zxing-cpp/Pillow not available" for either failure said nothing about
        # which module, or why — and the interesting failure for a compiled
        # wheel is not ImportError-because-missing but a missing shared library,
        # which reports as ImportError too with the reason in its message.
        for module, package in (("zxingcpp", "zxing-cpp"), ("PIL.Image", "Pillow")):
            try:
                __import__(module)
            except Exception as e:
                if self._logger:
                    self._logger.warning(
                        f"CameraSource: cannot import {module} ({package}): "
                        f"{type(e).__name__}: {e} — camera source disabled"
                    )
                return False

        self._active = True
        if self._logger:
            self._logger.info(f"CameraSource: ready on {self._device}")
        return True

    async def stop(self) -> None:
        """Reset all state."""
        self._active = False
        self._last_qr = None
        self._missing_count = 0
        self._capture_failures = 0

    def is_active(self) -> bool:
        return self._active

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Capture one frame and emit an event if QR state changed.

        Capture and decode run on a worker thread. This was the single worst
        stall in the plugin: ``ffmpeg`` is a subprocess with a 5s timeout and
        the decode is CPU-bound, and both ran on the event loop that every
        other source and every frontend RPC shares — so switching the camera
        trigger on made the whole plugin unresponsive for seconds at a time.

        Both halves go in one hop so the PIL image never crosses back to the
        loop; only the decoded string does.
        """
        if not self._active:
            return None

        captured, uri = await asyncio.to_thread(self._capture_and_decode)
        if not captured:
            self._capture_failures += 1
            if self._logger:
                self._logger.warning(
                    f"CameraSource: frame capture failed on {self._device} "
                    f"({self._capture_failures}/{self.CAPTURE_FAILURE_THRESHOLD})"
                )
            # Only give up after several in a row. One failure is a dropped
            # frame, which is ordinary; several is the device having actually
            # gone away, and only that is worth a stop/start cycle.
            if self._capture_failures >= self.CAPTURE_FAILURE_THRESHOLD:
                if self._logger:
                    self._logger.error(
                        f"CameraSource: {self._device} failed "
                        f"{self._capture_failures} captures in a row — reconnecting"
                    )
                self._active = False
            return None

        self._capture_failures = 0

        if uri:
            self._missing_count = 0
            if uri != self._last_qr:
                self._last_qr = uri
                if self._logger:
                    self._logger.info(f"CameraSource: QR detected uri={uri}")
                return MediaEvent(
                    kind=MediaEventKind.LOAD,
                    source_type=SourceType.CAMERA,
                    source_id=self.source_id,
                    media_id=uri,
                    uri=uri,
                )
        else:
            if self._last_qr:
                self._missing_count += 1
                if self._missing_count >= self.DEBOUNCE_THRESHOLD:
                    removed_uri = self._last_qr
                    self._last_qr = None
                    self._missing_count = 0
                    if self._logger:
                        self._logger.info(
                            f"CameraSource: QR gone after {self.DEBOUNCE_THRESHOLD} misses"
                        )
                    return MediaEvent(
                        kind=MediaEventKind.UNLOAD,
                        source_type=SourceType.CAMERA,
                        source_id=self.source_id,
                        media_id=removed_uri,
                        uri=removed_uri,
                    )

        return None

    # ── Frame capture ──────────────────────────────────────────────────

    def _capture_and_decode(self) -> "tuple[bool, Optional[str]]":
        """Capture a frame and decode it. Blocking; runs on a worker thread.

        Returns ``(captured, uri)``. The flag keeps a camera that has gone
        away — which must deactivate the source — distinguishable from a
        frame that simply held no QR code, which is the normal idle case.
        """
        frame = self._capture_frame()
        if frame is None:
            return False, None
        try:
            return True, self._decode_qr(frame)
        finally:
            try:
                frame.close()
            except Exception:
                pass

    def _capture_frame(self):
        """Capture one JPEG frame from the webcam using ffmpeg.

        Returns a PIL Image (caller must close it) or None on failure.
        The temp file is deleted before this method returns.
        """
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmpfile = f.name
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "v4l2",
                    "-i", self._device,
                    "-frames:v", "1",
                    "-q:v", "3",
                    tmpfile,
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                if self._logger:
                    stderr = result.stderr.decode(errors="replace").strip()
                    self._logger.warning(
                        f"CameraSource: ffmpeg failed: {stderr[-200:]}"
                    )
                return None
            from PIL import Image
            img = Image.open(tmpfile)
            return img.copy()  # copy before deleting the file
        except Exception as e:
            if self._logger:
                self._logger.error(f"CameraSource: capture error: {e}")
                self._logger.error(traceback.format_exc())
            return None
        finally:
            try:
                os.unlink(tmpfile)
            except Exception:
                pass

    # ── QR decode ──────────────────────────────────────────────────────

    def _decode_qr(self, image) -> Optional[str]:
        """Return the data string of the first QR code found, or None.

        zxing-cpp rather than pyzbar: pyzbar is a thin wrapper over the *system*
        libzbar0, which a stock SteamOS does not have and no Python wheel can
        supply, so this source could never work on an unmodified Deck. zxing-cpp
        ships the decoder inside its own manylinux wheel.
        """
        try:
            import zxingcpp
            # Greyscale first: the decoder wants a single channel anyway, and
            # this makes the call independent of whatever mode the capture path
            # produced (JPEG frames arrive as RGB, but a palette or 1-bit image
            # would otherwise be a decode failure with no explanation).
            results = zxingcpp.read_barcodes(
                image.convert("L"), formats=zxingcpp.BarcodeFormat.QRCode
            )
            for result in results:
                data = (result.text or "").strip()
                if data:
                    return data
        except Exception as e:
            if self._logger:
                self._logger.error(f"CameraSource: QR decode error: {e}")
        return None
