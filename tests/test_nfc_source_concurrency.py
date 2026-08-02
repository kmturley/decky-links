"""NfcSource thread-safety around the reader.

Polling and pairing are driven from two different asyncio tasks — the source
manager's per-source loop and the plugin's pairing handler. Both now do their
blocking work on worker threads, so they can genuinely run at the same time;
before that they were serialised for free, because neither yielded to the
event loop. Two threads interleaving commands on one PN532 over one serial
port corrupts the exchange, so the reader is held for the whole of either
operation.
"""

import asyncio
import threading
import time

import pytest

from sources.nfc_source import NfcSource


class _OverlapDetectingReader:
    """A reader that records whether two callers were ever inside it at once."""

    def __init__(self):
        self.inside = 0
        self.max_inside = 0
        self._guard = threading.Lock()

    def _enter(self):
        with self._guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)

    def _leave(self):
        with self._guard:
            self.inside -= 1

    def read_uid(self, timeout=0.2):
        self._enter()
        try:
            time.sleep(0.02)  # long enough for a racing writer to get in
            return None
        finally:
            self._leave()


@pytest.mark.asyncio
async def test_poll_and_write_never_touch_the_reader_at_once():
    src = NfcSource({}, logger=None)
    reader = _OverlapDetectingReader()
    src._reader = reader

    # Stand in for the real write, which is a long page-at-a-time exchange.
    def _fake_write(uid, uri):
        reader._enter()
        try:
            time.sleep(0.02)
            return True, None
        finally:
            reader._leave()

    src._write_ndef_uri_locked = _fake_write

    # Poll and pair concurrently, repeatedly, to give the race every chance.
    await asyncio.gather(*[
        coro
        for _ in range(5)
        for coro in (src.poll(), src.write_uri("DEADBEEF", "https://example.com"))
    ])

    assert reader.max_inside == 1, (
        f"reader was entered by {reader.max_inside} threads at once — polling "
        f"and pairing must not overlap on one serial port"
    )


@pytest.mark.asyncio
async def test_poll_does_not_block_the_event_loop():
    """The whole point of the offload: the loop stays responsive while a poll
    is in progress."""
    src = NfcSource({}, logger=None)

    class _SlowReader:
        def read_uid(self, timeout=0.2):
            time.sleep(0.2)
            return None

    src._reader = _SlowReader()

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        await src.poll()
    finally:
        ticker.cancel()

    # A blocking poll would have starved the ticker completely.
    assert ticks > 1, (
        f"event loop advanced only {ticks} times during a 0.2s poll — the "
        f"blocking read is still running on the loop"
    )
