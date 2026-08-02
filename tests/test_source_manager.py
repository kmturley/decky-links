"""
test_source_manager.py — the per-source poll loop.

Focused on the enablement path: a source the user has switched off must idle,
not retry on a backoff timer forever. Camera, MQTT, serial and file-watch all
ship disabled, so before this the plugin spent its whole life reconnecting to
hardware nobody had asked it to use.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sources.base import SourceType

from sources.base import MediaSource, SourceEventKind, SourceType


class FakeSource(MediaSource):
    """Minimal source whose enabled/active state the test drives directly."""

    source_type = SourceType.MQTT

    def __init__(self, enabled=True, start_ok=True):
        self._settings = {"enabled": enabled}
        self._active = False
        self._start_ok = start_ok
        self.start_calls = 0
        self.stop_calls = 0
        self.poll_calls = 0

    @property
    def source_id(self):
        return "fake:1"

    @property
    def poll_interval(self):
        return 0.001

    async def start(self):
        self.start_calls += 1
        self._active = self._start_ok
        return self._start_ok

    async def stop(self):
        self.stop_calls += 1
        self._active = False

    def is_active(self):
        return self._active

    async def poll(self):
        self.poll_calls += 1
        return None


@pytest.fixture
def manager():
    from sources.manager import SourceManager
    return SourceManager(asyncio.Queue(), logger=MagicMock())


async def _run_briefly(manager, source, seconds=0.05):
    task = asyncio.create_task(manager._run_source(source))
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestDisabledSources:

    @pytest.mark.asyncio
    async def test_disabled_source_is_never_started(self, manager):
        source = FakeSource(enabled=False)
        await _run_briefly(manager, source)
        assert source.start_calls == 0
        assert source.poll_calls == 0

    @pytest.mark.asyncio
    async def test_enabled_source_is_started_and_polled(self, manager):
        source = FakeSource(enabled=True)
        await _run_briefly(manager, source)
        assert source.start_calls == 1
        assert source.poll_calls > 0

    @pytest.mark.asyncio
    async def test_disabled_source_does_not_retry_on_a_timer(self, manager, monkeypatch):
        """The point of the whole change: no backoff loop against hardware the
        user has switched off."""
        import sources.manager as mgr
        monkeypatch.setattr(mgr, "DISABLED_POLL_SECONDS", 0.001)
        source = FakeSource(enabled=False)
        await _run_briefly(manager, source, seconds=0.05)
        assert source.start_calls == 0

    @pytest.mark.asyncio
    async def test_switching_a_source_on_starts_it_without_a_restart(self, manager, monkeypatch):
        import sources.manager as mgr
        monkeypatch.setattr(mgr, "DISABLED_POLL_SECONDS", 0.001)
        source = FakeSource(enabled=False)

        task = asyncio.create_task(manager._run_source(source))
        await asyncio.sleep(0.02)
        assert source.start_calls == 0

        source._settings["enabled"] = True     # user flips the toggle
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert source.start_calls == 1

    @pytest.mark.asyncio
    async def test_switching_a_running_source_off_stops_it(self, manager, monkeypatch):
        import sources.manager as mgr
        monkeypatch.setattr(mgr, "DISABLED_POLL_SECONDS", 0.001)
        source = FakeSource(enabled=True)

        task = asyncio.create_task(manager._run_source(source))
        await asyncio.sleep(0.02)
        assert source.is_active()

        source._settings["enabled"] = False
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not source.is_active()
        assert source.stop_calls >= 1

    @pytest.mark.asyncio
    async def test_switching_off_emits_disconnected(self, manager, monkeypatch):
        import sources.manager as mgr
        monkeypatch.setattr(mgr, "DISABLED_POLL_SECONDS", 0.001)
        source = FakeSource(enabled=True)

        task = asyncio.create_task(manager._run_source(source))
        await asyncio.sleep(0.02)
        source._settings["enabled"] = False
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        kinds = []
        while not manager._queue.empty():
            kinds.append(manager._queue.get_nowait().kind)
        assert SourceEventKind.CONNECTED in kinds
        assert SourceEventKind.DISCONNECTED in kinds


# ── Backoff on the poll-exception path ────────────────────────────────────────

class TestPollExceptionBacksOff:
    """A source raising on every poll used to fall through to the poll_interval
    sleep — 0.1s for MQTT and serial — and hot-loop at 10Hz writing a full
    traceback each time."""

    @pytest.mark.asyncio
    async def test_repeated_poll_errors_back_off_exponentially(self):
        import sources.manager as mgr

        source = MagicMock()
        source.source_id = "flaky:0"
        source.source_type = SourceType.NFC
        source.poll_interval = 0.1
        source.is_enabled.return_value = True
        source.is_active.return_value = True
        source.poll = AsyncMock(side_effect=RuntimeError("hardware fell over"))

        sleeps = []

        async def _record_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 4:
                raise asyncio.CancelledError

        queue = asyncio.Queue()
        manager = mgr.SourceManager(queue, logger=MagicMock())

        with patch.object(mgr.asyncio, "sleep", _record_sleep):
            with pytest.raises(asyncio.CancelledError):
                await manager._run_source(source)

        # Doubling from RECONNECT_MIN, not a flat poll_interval.
        assert sleeps[:3] == [
            mgr.RECONNECT_MIN,
            mgr.RECONNECT_MIN * 2,
            mgr.RECONNECT_MIN * 4,
        ], f"expected exponential backoff, got {sleeps}"
        assert source.poll_interval not in sleeps[:3]

    @pytest.mark.asyncio
    async def test_backoff_is_capped(self):
        import sources.manager as mgr

        source = MagicMock()
        source.source_id = "flaky:0"
        source.source_type = SourceType.NFC
        source.poll_interval = 0.1
        source.is_enabled.return_value = True
        source.is_active.return_value = True
        source.poll = AsyncMock(side_effect=RuntimeError("still broken"))

        sleeps = []

        async def _record_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 30:
                raise asyncio.CancelledError

        manager = mgr.SourceManager(asyncio.Queue(), logger=MagicMock())
        with patch.object(mgr.asyncio, "sleep", _record_sleep):
            with pytest.raises(asyncio.CancelledError):
                await manager._run_source(source)

        assert max(sleeps) == mgr.RECONNECT_MAX
