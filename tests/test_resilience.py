"""
test_resilience.py — the audit's smaller resilience findings.

Each of these was a failure the user could not see, or a cost paid on every
cycle for no reason. None was load-bearing on its own, which is exactly why they
sat open: nothing forced the issue.
"""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sources.base import SourceEvent, SourceEventKind, SourceType


# ── Unload awaits the loop it cancelled ───────────────────────────────────────

class TestUnloadAwaitsTheEventLoop:
    """cancel() only *schedules* the CancelledError. Returning without awaiting
    let unload race the loop: stop_all() tore sources down while the loop was
    still mid-iteration and might touch them."""

    @pytest.mark.asyncio
    async def test_unload_waits_for_the_task_to_finish(self, plugin):
        started = asyncio.Event()
        finished = []

        async def _loop():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # A real handler does teardown here; the point is that unload
                # must not return before it runs.
                finished.append("cleaned up")
                raise

        plugin.polling_task = asyncio.create_task(_loop())
        await started.wait()

        await plugin._unload()

        assert plugin.polling_task.done()
        assert finished == ["cleaned up"]

    @pytest.mark.asyncio
    async def test_a_loop_that_raises_does_not_break_unload(self, plugin):
        """Unload has to finish stopping the sources regardless."""
        async def _bad_loop():
            raise RuntimeError("loop died on the way out")

        plugin.polling_task = asyncio.create_task(_bad_loop())
        await asyncio.sleep(0)

        await plugin._unload()  # must not raise


# ── Dropped events are visible ────────────────────────────────────────────────

class TestDroppedEventsAreVisible:
    """The loop catches bare Exception, logs, and drops the event. That is the
    right call — a handler that raised halfway has already applied part of its
    effect, so retrying is worse. What was missing was any trace beyond a log
    line: a tap that silently does nothing looks exactly like a tap the reader
    never saw, which sends people off debugging hardware."""

    @pytest.mark.asyncio
    async def test_a_dropped_event_is_counted_and_announced(self, plugin, mock_decky):
        plugin._event_queue = asyncio.Queue()
        await plugin._event_queue.put(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.NFC,
            source_id="nfc:0",
        ))

        with patch.object(
            plugin, "_handle_source_event",
            AsyncMock(side_effect=RuntimeError("handler exploded")),
        ):
            task = asyncio.create_task(plugin._event_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert plugin._dropped_events >= 1
        assert "handler exploded" in plugin._last_drop_reason
        assert "plugin_health" in [c.args[0] for c in mock_decky.emit.call_args_list]

    @pytest.mark.asyncio
    async def test_the_count_is_askable_after_the_fact(self, plugin):
        """A drop that happened before the panel opened has no event to have
        missed, so the count has to be reachable by RPC too."""
        plugin._dropped_events = 3
        plugin._last_drop_reason = "ValueError: nope"
        assert await plugin.get_health() == {
            "dropped_events": 3,
            "last_drop_reason": "ValueError: nope",
        }

    @pytest.mark.asyncio
    async def test_a_healthy_plugin_reports_nothing_wrong(self, plugin):
        assert await plugin.get_health() == {
            "dropped_events": 0,
            "last_drop_reason": None,
        }


# ── The debug forgery RPC is gated ────────────────────────────────────────────

class TestSimulateTagIsGated:
    """It emits the same events as a real scan from caller-supplied data, so
    anything reaching the RPC surface could make the panel believe a paired tag
    was presented. No frontend caller remains."""

    @pytest.mark.asyncio
    async def test_refused_without_the_debug_flag(self, plugin, mock_decky):
        with patch.dict(os.environ, {}, clear=True):
            result = await plugin.simulate_tag(b"\xde\xad\xbe\xef", "steam://rungameid/400")

        assert result is False
        assert "media_detected" not in [c.args[0] for c in mock_decky.emit.call_args_list]

    @pytest.mark.asyncio
    async def test_allowed_with_the_debug_flag(self, plugin, mock_decky):
        with patch.dict(os.environ, {"DECKY_LINKS_DEBUG": "1"}), \
             patch.object(plugin.nfc_source, "_classify_tag", return_value={}):
            result = await plugin.simulate_tag(b"\xde\xad\xbe\xef", "steam://rungameid/400")

        assert result is True
        assert "media_detected" in [c.args[0] for c in mock_decky.emit.call_args_list]


# ── Camera tolerates a dropped frame ──────────────────────────────────────────

class TestCameraDoesNotTearDownOnOneBadFrame:
    """A dropped frame is ordinary for a webcam — USB bandwidth contention, the
    device busy with autoexposure. Tearing down on one cost a full stop/start
    and an ffmpeg spawn to recover from something that fixes itself."""

    def _camera(self):
        from sources.camera_source import CameraSource
        src = CameraSource({"enabled": True, "device": "/dev/video0"}, logger=MagicMock())
        src._active = True
        return src

    @pytest.mark.asyncio
    async def test_one_failed_capture_keeps_the_source_up(self):
        src = self._camera()
        with patch.object(src, "_capture_and_decode", return_value=(False, None)):
            await src.poll()
        assert src.is_active() is True

    @pytest.mark.asyncio
    async def test_repeated_failures_eventually_reconnect(self):
        src = self._camera()
        with patch.object(src, "_capture_and_decode", return_value=(False, None)):
            for _ in range(src.CAPTURE_FAILURE_THRESHOLD):
                await src.poll()
        assert src.is_active() is False

    @pytest.mark.asyncio
    async def test_a_good_frame_resets_the_run(self):
        """Intermittent failures must not accumulate into a teardown across
        minutes of otherwise fine operation."""
        src = self._camera()
        with patch.object(src, "_capture_and_decode", return_value=(False, None)):
            await src.poll()
            await src.poll()
        with patch.object(src, "_capture_and_decode", return_value=(True, None)):
            await src.poll()
        with patch.object(src, "_capture_and_decode", return_value=(False, None)):
            await src.poll()
            await src.poll()
        assert src.is_active() is True


# ── Poll cadence is measured from the poll's start ────────────────────────────

class TestPollCadence:
    """Sleeping the full interval *after* the poll made the real cadence
    `interval + work_time`, so the configured number meant something different
    for every source — and the camera, a 5s capture on a 1s interval, actually
    sampled every 6s."""

    @pytest.mark.asyncio
    async def test_slow_poll_shortens_the_following_sleep(self):
        import sources.manager as mgr

        source = MagicMock()
        source.source_id = "slow:0"
        source.source_type = SourceType.CAMERA
        source.poll_interval = 1.0
        source.is_enabled.return_value = True
        source.is_active.return_value = True

        async def _slow_poll():
            # 0.4s of "work", simulated by advancing the clock rather than
            # actually waiting.
            clock[0] += 0.4
            return None

        source.poll = _slow_poll
        clock = [0.0]
        sleeps = []

        async def _record_sleep(delay):
            sleeps.append(delay)
            raise asyncio.CancelledError

        manager = mgr.SourceManager(asyncio.Queue(), logger=MagicMock())
        with patch.object(mgr.time, "monotonic", lambda: clock[0]), \
             patch.object(mgr.asyncio, "sleep", _record_sleep):
            with pytest.raises(asyncio.CancelledError):
                await manager._run_source(source)

        assert sleeps == [pytest.approx(0.6)], (
            f"expected the remainder of the interval, got {sleeps}"
        )

    @pytest.mark.asyncio
    async def test_a_poll_that_overruns_does_not_sleep_negative(self):
        """asyncio.sleep on a negative number returns immediately, but the
        clamp says so explicitly rather than relying on that — and stops the
        overrun accumulating into debt."""
        import sources.manager as mgr

        source = MagicMock()
        source.source_id = "slower:0"
        source.source_type = SourceType.CAMERA
        source.poll_interval = 1.0
        source.is_enabled.return_value = True
        source.is_active.return_value = True

        clock = [0.0]

        async def _very_slow_poll():
            clock[0] += 5.0
            return None

        source.poll = _very_slow_poll
        sleeps = []

        async def _record_sleep(delay):
            sleeps.append(delay)
            raise asyncio.CancelledError

        manager = mgr.SourceManager(asyncio.Queue(), logger=MagicMock())
        with patch.object(mgr.time, "monotonic", lambda: clock[0]), \
             patch.object(mgr.asyncio, "sleep", _record_sleep):
            with pytest.raises(asyncio.CancelledError):
                await manager._run_source(source)

        assert sleeps == [0.0]


# ── The duplicate status publish ──────────────────────────────────────────────

class TestStatusIsPublishedOncePerEvent:
    """_handle_source_event published, then the loop published again for the
    same event. The second was a no-op diff rather than a message, so it cost
    only a get_source_statuses call — but it was a call across every source,
    twice, for every connect and disconnect."""

    @pytest.mark.asyncio
    async def test_handler_does_not_publish_on_its_own(self, plugin, mock_decky):
        with patch.object(plugin, "_publish_statuses", AsyncMock()) as publish:
            await plugin._handle_source_event(SourceEvent(
                kind=SourceEventKind.CONNECTED,
                source_type=SourceType.NFC,
                source_id="nfc:0",
            ))
        publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_loop_still_publishes_after_dispatching(self, plugin, mock_decky):
        plugin._event_queue = asyncio.Queue()
        await plugin._event_queue.put(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.NFC,
            source_id="nfc:0",
        ))

        with patch.object(plugin, "_publish_statuses", AsyncMock()) as publish:
            task = asyncio.create_task(plugin._event_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert publish.await_count >= 1
