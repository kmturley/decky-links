"""Source manager — orchestrates all media sources.

The manager owns one ``asyncio.Task`` per registered source.  Each task
polls its source independently and pushes events into a shared
``asyncio.Queue`` that the plugin's main loop consumes.
"""

import asyncio
import traceback
from typing import List, Optional

from sources.base import (
    MediaSource,
    PluginEvent,
    SourceEvent,
    SourceEventKind,
)


# Backoff for a source whose hardware is not responding.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
# How often a switched-off source re-checks whether the user switched it back
# on. Deliberately slow: this is the idle path, and nothing is waiting on it.
DISABLED_POLL_SECONDS = 5.0


class SourceManager:
    """Orchestrates all media sources, each in its own asyncio.Task.

    Usage::

        queue: asyncio.Queue[PluginEvent] = asyncio.Queue()
        manager = SourceManager(queue, logger=decky.logger)
        manager.register(nfc_source)
        manager.register(storage_source)
        await manager.start_all()
        # ... consume events from queue ...
        await manager.stop_all()
    """

    def __init__(
        self,
        event_queue: "asyncio.Queue[PluginEvent]",
        logger=None,
    ):
        self._queue = event_queue
        self._logger = logger
        self._sources: List[MediaSource] = []
        self._tasks: List[asyncio.Task] = []

    # ── Registration ───────────────────────────────────────────────────

    def register(self, source: MediaSource) -> None:
        """Add a source to be managed.  Must be called before ``start_all``."""
        self._sources.append(source)
        if self._logger:
            self._logger.info(
                f"SourceManager: registered {source.source_type.value} "
                f"source ({source.source_id})"
            )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """Start each registered source in its own asyncio.Task."""
        for source in self._sources:
            task = asyncio.create_task(self._run_source(source))
            self._tasks.append(task)
            if self._logger:
                self._logger.info(
                    f"SourceManager: started task for {source.source_id}"
                )

    async def stop_all(self) -> None:
        """Cancel all running source tasks and call stop() on each source."""
        for task in self._tasks:
            task.cancel()

        # Wait for all tasks to finish cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        for source in self._sources:
            try:
                await source.stop()
            except Exception as e:
                if self._logger:
                    self._logger.error(
                        f"SourceManager: error stopping {source.source_id}: {e}"
                    )

    # ── Per-source poll loop ───────────────────────────────────────────

    async def _run_source(self, source: MediaSource) -> None:
        """Poll loop for a single source.

        Handles initialisation retries with exponential backoff, and emits
        ``SourceEvent`` CONNECTED/DISCONNECTED events as the source comes
        online or drops out.
        """
        reconnect_delay = RECONNECT_MIN
        was_connected = False
        was_disabled = False

        while True:
            try:
                # ── Respect the user's on/off switch ───────────────────
                # Checked every cycle rather than once at startup, so toggling
                # a source in the panel takes effect without a plugin restart.
                if not source.is_enabled():
                    if not was_disabled:
                        if self._logger:
                            self._logger.info(
                                f"SourceManager: {source.source_id} is disabled — idling"
                            )
                        if source.is_active():
                            try:
                                await source.stop()
                            except Exception as e:
                                if self._logger:
                                    self._logger.warning(
                                        f"SourceManager: error stopping disabled "
                                        f"{source.source_id}: {e}"
                                    )
                        if was_connected:
                            await self._queue.put(SourceEvent(
                                kind=SourceEventKind.DISCONNECTED,
                                source_type=source.source_type,
                                source_id=source.source_id,
                            ))
                            was_connected = False
                        was_disabled = True
                    await asyncio.sleep(DISABLED_POLL_SECONDS)
                    continue

                if was_disabled:
                    if self._logger:
                        self._logger.info(
                            f"SourceManager: {source.source_id} re-enabled"
                        )
                    was_disabled = False
                    reconnect_delay = RECONNECT_MIN

                # ── Initialise if needed ───────────────────────────────
                if not source.is_active():
                    if was_connected:
                        # Source was previously active — emit disconnect
                        await self._queue.put(SourceEvent(
                            kind=SourceEventKind.DISCONNECTED,
                            source_type=source.source_type,
                            source_id=source.source_id,
                        ))
                        was_connected = False

                    # Always release the previous attempt's resources before
                    # re-initialising. Without this, a source that dropped out
                    # (e.g. NFC reader after a poll error) leaks its open serial
                    # handle and start() opens a second fd on the same device,
                    # making reconnects timing-dependent.
                    try:
                        await source.stop()
                    except Exception as e:
                        if self._logger:
                            self._logger.warning(
                                f"SourceManager: error stopping {source.source_id} "
                                f"before restart: {e}"
                            )

                    ok = await source.start()
                    if not ok:
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(RECONNECT_MAX, reconnect_delay * 2)
                        continue

                    # Successfully (re)connected
                    reconnect_delay = RECONNECT_MIN
                    was_connected = True
                    await self._queue.put(SourceEvent(
                        kind=SourceEventKind.CONNECTED,
                        source_type=source.source_type,
                        source_id=source.source_id,
                    ))

                # ── Poll ───────────────────────────────────────────────
                event = await source.poll()
                if event is not None:
                    await self._queue.put(event)

            except asyncio.CancelledError:
                # Task is being stopped — exit cleanly
                if was_connected:
                    await self._queue.put(SourceEvent(
                        kind=SourceEventKind.DISCONNECTED,
                        source_type=source.source_type,
                        source_id=source.source_id,
                    ))
                raise
            except Exception as e:
                if self._logger:
                    self._logger.error(
                        f"SourceManager: error in {source.source_id}: {e}"
                    )
                    self._logger.error(traceback.format_exc())
                # Mark source as needing reconnect on next iteration
                was_connected = False

                # Back off here too, not just on a failed start(). A source
                # raising on every poll used to fall straight through to the
                # poll_interval sleep below — 0.1s for MQTT and serial — and
                # hot-loop at 10Hz writing a full traceback each time, which
                # buries every other log line and burns battery doing it.
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(RECONNECT_MAX, reconnect_delay * 2)
                continue

            await asyncio.sleep(source.poll_interval)

    # ── Introspection ──────────────────────────────────────────────────

    @property
    def sources(self) -> List[MediaSource]:
        """Return the list of registered sources (read-only view)."""
        return list(self._sources)

    def get_source(self, source_id: str) -> Optional[MediaSource]:
        """Look up a source by its unique ID."""
        for source in self._sources:
            if source.source_id == source_id:
                return source
        return None
