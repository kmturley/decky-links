"""NFC media source — wraps the existing NFC reader and polling logic.

This source is responsible for:
- Reader hardware connection lifecycle (connect / disconnect detection)
- Tag UID polling with debounced removal detection
- NDEF record reading and URI extraction
- NDEF URI writing (for pairing)
- Tag classification (Mifare Classic, NTAG21x, etc.)

It does NOT own:
- State machine transitions (plugin's job)
- Game launching (plugin's job)
- Frontend event emission (plugin's job via queue consumer)
- Audio playback (plugin's job)

Three principles govern the poll loop, each of them the answer to a specific
failure the old one had:

1. **A failed read is not an absent tag.**  Protocol desyncs used to feed the
   removal counter, so three bad frames in a row quit the user's game.  Only a
   reader that successfully reports "nothing there" counts as removal.
2. **Removal is measured in time, not in poll counts.**  A count is silently
   re-scaled by every change to ``polling_interval``; a duration is not.
3. **Recoverable errors are recovered from, not escalated.**  Closing the
   serial port over a checksum mismatch cost seconds of dead reader and a
   "disconnected" banner, for something that resynchronises in 20 ms.
"""

import asyncio
import os
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceEvent,
    SourceEventKind,
    SourceType,
)

from nfc_core import ndef_codec, tag_identity
from nfc_core.tag_handlers import get_handler

try:
    from nfc_core.reader import (
        PN532UARTReader,
        ReaderError,
        ReaderProtocolError,
        ReaderTransportError,
        TargetInfo,
    )
    _READER_IMPORT_ERROR = None
except ImportError as _e:
    # Keep the reason. Discarding it makes a packaging failure (e.g. wheels
    # built for the wrong platform, breaking the adafruit_pn532 -> Blinka ->
    # cffi import chain) indistinguishable from "no reader plugged in", which
    # is exactly what made this class of bug so hard to diagnose.
    PN532UARTReader = None
    _READER_IMPORT_ERROR = _e

    class ReaderError(Exception):
        pass

    class ReaderProtocolError(ReaderError):
        pass

    class ReaderTransportError(ReaderError):
        pass

    TargetInfo = ()


# NTAG21x capability container. Page 3 holds magic / version / size÷8 / access;
# reading it is how the real capacity of a tag is known rather than assumed.
_NTAG_CC_PAGE = 3
_NTAG_NDEF_MAGIC = 0xE1
# NTAG215 user memory: pages 4-129.  The old value of 130 pages ran to page
# 133 — four pages past the end of user memory and into the dynamic lock bytes
# and configuration pages, where a write can permanently alter the tag's
# access settings.  Still the fallback when a tag will not report its own size.
_NTAG_FALLBACK_USER_PAGES = 126
# NTAG216 is the largest of the family at 888 bytes; anything claiming more is
# a misread, not a bigger tag.
_NTAG_MAX_USER_PAGES = 222


# USB-serial bridge chips used by PN532/PN5180 UART modules. Auto-detection is
# restricted to these so it can never land on an unrelated CDC-ACM device.
_KNOWN_USB_SERIAL_VIDS = frozenset({
    0x1A86,  # QinHeng CH340 / CH341
    0x10C4,  # Silicon Labs CP210x
    0x0403,  # FTDI
    0x067B,  # Prolific PL2303
})


def _is_target(obj) -> bool:
    """True when ``obj`` is a real :class:`TargetInfo`.

    Mock readers answer every attribute with a truthy stand-in, so a plain
    ``if target:`` would treat one as a genuine detection.  Capability probing
    has to check the type, not the truthiness.
    """
    return isinstance(obj, TargetInfo) if TargetInfo else False


class NfcSource(MediaSource):
    """NFC reader polling source.

    Wraps the existing PN532/ACR122U/Proxmark/nfcpy reader abstraction
    and produces MediaEvents for tag arrival and removal.
    """

    source_type = SourceType.NFC

    # Retained so existing callers and settings keep working, but removal is
    # now governed by REMOVAL_GRACE_SECONDS as well: a tag must be both
    # missed this many times *and* absent for that long.
    DEBOUNCE_THRESHOLD = 2
    # How long a tag must be continuously absent before removal is reported.
    # Expressed in seconds so that changing polling_interval does not silently
    # change how long it takes to notice a tag being lifted.
    REMOVAL_GRACE_SECONDS = 0.6
    # Consecutive recoverable protocol errors tolerated before the reader is
    # treated as genuinely broken and reconnected.
    MAX_PROTOCOL_ERRORS = 5
    # Attempts made to read a newly arrived tag's content before giving up and
    # reporting it unreadable.  Contactless is a lossy link and one dropped
    # frame used to be cached permanently as "blank tag".
    CONTENT_READ_ATTEMPTS = 3

    def __init__(
        self,
        settings: dict,
        key_manager=None,
        logger=None,
    ):
        self._settings = settings
        self._key_manager = key_manager
        self._logger = logger
        # One reader, one serial port, two callers. Polling runs on the source
        # manager's task and pairing on the plugin's, and now that both do
        # their work in threads they can genuinely overlap — the event loop
        # used to serialise them for free, because neither yielded. Two
        # threads interleaving commands on a PN532 corrupts the exchange, so
        # the reader is held for the whole of a poll or a write.
        self._io_lock = threading.RLock()
        # Set by stop(). The poll body runs in a worker thread via
        # asyncio.to_thread, and a thread cannot be cancelled — cancelling the
        # awaiting task raises CancelledError in the coroutine while the
        # thread carries on with the serial port open. Since
        # ThreadPoolExecutor threads are non-daemon, that also stops the
        # interpreter exiting, which is how a reloaded plugin leaves its
        # previous backend alive holding /dev/ttyUSB0. The flag lets the
        # thread notice it should give up at each step instead.
        self._stopping = False
        self._reader = None
        # Legacy field retained for compatibility with existing reader module
        self._uart = None

        # Polling state
        self._last_uid_hex: Optional[str] = None
        self._missing_count: int = 0
        # Monotonic timestamp of the first poll that failed to see the tag.
        self._absent_since: Optional[float] = None
        # Consecutive recoverable errors; reset by any clean poll.
        self._protocol_errors: int = 0

        # Current tag state (readable by plugin)
        self.current_tag_uid: Optional[str] = None
        self.current_tag_uri: Optional[str] = None
        self.current_tag_meta: Optional[Dict[str, Any]] = None

        # Effective device path (may differ from settings when auto-detected)
        self._effective_path: Optional[str] = None
        # Last path that produced a successful connection — prefer this over
        # auto-detecting a different port after a transient USB disconnect.
        self._last_good_path: Optional[str] = None
        # Deduplicates the repeated logging of an unchanged failure state.
        self._last_log_key: Optional[str] = None

        # Pairing state (set by plugin)
        self.is_pairing: bool = False
        self.pairing_uri: Optional[str] = None

        # Tag classification cache (UID hex -> metadata dict) with LRU eviction
        self._tag_classification_cache: OrderedDict = OrderedDict()
        # uid hex → user page count read from the tag's capability container.
        # Without it the CC is re-read on every classification *and* every NDEF
        # read of the same tag, which is both wasteful and, more to the point,
        # makes the number of reader round-trips depend on the call path.
        self._ntag_pages_cache: Dict[str, int] = {}
        self._tag_cache_max_size: int = 128

    @property
    def source_id(self) -> str:
        device = self._effective_path or self._settings.get("device_path", "unknown")
        return f"nfc:{device}"

    @property
    def poll_interval(self) -> float:
        interval = self._settings.get("polling_interval", 0.5)
        if not isinstance(interval, (int, float)):
            return 0.5
        val = float(interval)
        if not (0.1 <= val <= 10.0):
            return 0.5
        return val

    @property
    def removal_grace(self) -> float:
        """Seconds of continuous absence before a tag counts as removed.

        Never shorter than two poll intervals, so a single dropped poll can
        never quit the user's game however the interval is configured.
        """
        configured = self._settings.get("removal_grace_seconds")
        if isinstance(configured, (int, float)) and 0.1 <= float(configured) <= 10.0:
            grace = float(configured)
        else:
            grace = self.REMOVAL_GRACE_SECONDS
        return max(grace, self.poll_interval * 2)

    @property
    def reader(self):
        """Expose the underlying reader for direct access by plugin methods."""
        return self._reader

    def rearm(self) -> None:
        """Forget the currently-seen tag so the next poll re-reports it as new.

        ``poll()`` only emits a LOAD event when the UID differs from the last
        one seen. Without this, a card already resting on the reader when the
        user starts pairing would never produce an event, and the user would
        have to lift and re-tap. Called by ``Plugin.start_pairing``.
        """
        self._last_uid_hex = None
        self._missing_count = 0
        self._absent_since = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def _find_serial_port(self) -> Optional[str]:
        """Return a serial port that looks like a reader, or None.

        Identified by the USB-serial bridge's vendor ID rather than by globbing
        device names. A Steam Deck exposes an unrelated ``/dev/ttyACM0``; the
        old glob picked it whenever no reader was plugged in and then wrote
        PN532 wake-up frames to it every retry. Auto-detection now guesses only
        when there is positive evidence, and stays silent otherwise.
        """
        try:
            from serial.tools import list_ports
        except ImportError:
            self._log_once("no-list-ports", "warning",
                           "NfcSource: pyserial list_ports unavailable — "
                           "cannot auto-detect a reader port")
            return None

        try:
            ports = list(list_ports.comports())
        except Exception as e:
            self._log_once("list-ports-failed", "warning",
                           f"NfcSource: could not enumerate serial ports: {e}")
            return None

        candidates = sorted(
            (p for p in ports if p.vid in _KNOWN_USB_SERIAL_VIDS),
            key=lambda p: p.device,
        )
        if not candidates:
            seen = ", ".join(sorted(p.device for p in ports)) or "none"
            self._log_once(
                "no-candidate", "info",
                f"NfcSource: no USB-serial reader found "
                f"(configured {self._settings.get('device_path')!r} not present; "
                f"ports seen: {seen})",
            )
            return None

        chosen = candidates[0]
        self._log_once(
            f"detected:{chosen.device}", "info",
            f"NfcSource: auto-detected serial port {chosen.device!r} "
            f"[{chosen.vid:04x}:{chosen.pid:04x} {chosen.product or 'unknown'}] "
            f"(configured {self._settings.get('device_path')!r} not found)",
        )
        return chosen.device

    def _log_once(self, key: str, level: str, message: str) -> None:
        """Log only when the situation changes.

        ``start()`` is retried on a timer forever, so an unplugged reader used
        to write the same two lines to the log every 30 seconds indefinitely.
        """
        if not self._logger or self._last_log_key == key:
            return
        self._last_log_key = key
        getattr(self._logger, level)(message)

    async def start(self) -> bool:
        """Initialise the NFC reader hardware."""
        path = self._settings.get("device_path", "")
        if not os.path.exists(path):
            if self._last_good_path:
                # We had a working connection before. Wait for that exact
                # device to reappear instead of jumping to a different port
                # (which could be a completely different device, e.g. ttyACM0).
                if not os.path.exists(self._last_good_path):
                    return False
                path = self._last_good_path
            else:
                path = self._find_serial_port() or ""
        if not path:
            return False
        self._effective_path = path

        reader = await self._create_reader()
        if not reader:
            self._reader = None
            return False

        connected = await reader.connect()
        if not connected:
            self._log_once(
                f"connect-failed:{path}", "error",
                f"NfcSource: reader init failed on {path}: unable to connect",
            )
            self._reader = None
            return False

        if self._logger:
            self._logger.info(
                f"NfcSource: connected to reader type "
                f"{self._settings.get('reader_type')} on {path}"
            )
        # Let the next failure speak up, however many times we retried to get here.
        self._last_log_key = None
        self._reader = reader
        self._last_good_path = self._effective_path
        self._protocol_errors = 0
        self._stopping = False
        return True

    async def stop(self) -> None:
        """Release reader resources.

        Sets the stop flag before touching the reader so a poll already
        running in a worker thread unwinds at its next checkpoint rather than
        continuing to drive a reader we are closing underneath it.
        """
        self._stopping = True
        if self._reader:
            try:
                self._reader.close()
            except Exception:
                pass
        self._reader = None
        if self._uart:
            try:
                self._uart.close()
            except Exception:
                pass
            self._uart = None
        # Reset tag state so the same card is re-detected after reconnect
        self._last_uid_hex = None
        self._missing_count = 0
        self._absent_since = None
        self._protocol_errors = 0
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.current_tag_meta = None

    def is_active(self) -> bool:
        """Check if reader is connected and usable."""
        if not self._reader:
            return False
        if hasattr(self._reader, "is_connected"):
            return self._reader.is_connected()
        return True

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self):
        """One poll cycle: read UID, detect arrival/removal, return event(s).

        The work happens on a worker thread because all of it blocks: the UID
        read waits on the serial line, classification sleeps between key
        attempts, and an NDEF read is dozens of round trips to the tag. On the
        event loop that is a ~200ms stall on a bare poll and far worse with a
        tag present — and it is shared with every other source and every
        frontend RPC. The proxmark backend shells out to its client binary
        with a 5s timeout, so this is the difference between a responsive
        plugin and a frozen one.

        May return a list when one cycle produces two events, which happens
        when a tag is swapped for another between polls: the outgoing tag's
        UNLOAD must still be reported, or whatever it launched can never be
        quit by lifting it.
        """
        if not self._reader:
            return None

        return await asyncio.to_thread(self._poll_blocking)

    def _poll_blocking(self):
        """The synchronous body of :meth:`poll`. Never call from the loop."""
        with self._io_lock:
            return self._poll_locked()

    def _poll_locked(self):
        if not self._reader or self._stopping:
            return None

        try:
            target = self._detect()
        except ReaderTransportError as e:
            # The link itself is gone. This is the only failure that justifies
            # dropping the reader.
            self._drop_reader(f"link lost: {e}")
            return None
        except ReaderProtocolError as e:
            return self._handle_protocol_error(e)
        except Exception as e:
            # An unclassified failure from a backend that does not use the
            # reader error taxonomy. Treat it as recoverable — escalating to a
            # teardown is what used to make a single bad frame cost seconds.
            return self._handle_protocol_error(e)

        # A clean answer, whatever it said. The link is in step again.
        self._protocol_errors = 0

        if target is not None:
            return self._on_tag_present(target)
        return self._on_tag_absent()

    def _detect(self):
        """Return a :class:`TargetInfo` for the tag present, or ``None``.

        Normalises across backends: a reader that can report ATQA/SAK does so,
        and one that can only report a UID gets wrapped in the same shape so
        the caller never has two code paths.
        """
        reader = self._reader
        read_target = getattr(reader, "read_target", None)
        if read_target is not None:
            result = read_target(timeout=0.2)
            if _is_target(result):
                return result
            if result is None:
                # An honest "nothing there".
                return None
            # Neither a TargetInfo nor None: a stand-in rather than the real
            # method. Fall through to the UID path.

        uid = reader.read_uid(timeout=0.2)
        if not uid:
            return None
        try:
            uid = bytes(uid)
        except (TypeError, ValueError):
            raise ReaderProtocolError("reader returned a UID that is not bytes")
        if not self._uid_is_usable(uid):
            raise ReaderProtocolError(
                f"implausible UID of {len(uid)} bytes — treating as a bad frame"
            )
        return TargetInfo(uid=uid) if TargetInfo else None

    @staticmethod
    def _uid_is_usable(uid: bytes) -> bool:
        """Reject UIDs that anticollision could not have produced.

        A corrupt frame read as identity used to become a brand-new tag,
        producing a spurious LOAD and, when it vanished next poll, a spurious
        removal.  Mock readers hand back objects whose length is zero; those
        are passed through rather than rejected, because "cannot tell" is not
        the same as "wrong".
        """
        try:
            length = len(uid)
        except TypeError:
            return True
        if length == 0:
            return True
        return length in (4, 7, 8, 10)

    def _handle_protocol_error(self, exc: BaseException) -> None:
        """Resynchronise after a recoverable failure.

        Deliberately returns no event.  A desync tells us nothing about
        whether a tag is present, so it must not advance the removal clock —
        letting it do so is what turned bad frames into phantom removals and
        closed games.
        """
        self._protocol_errors += 1
        recover = getattr(self._reader, "recover", None)
        if recover is not None:
            try:
                recover()
            except Exception:
                pass

        if self._protocol_errors >= self.MAX_PROTOCOL_ERRORS:
            self._drop_reader(
                f"{self._protocol_errors} consecutive protocol errors, "
                f"last: {exc}"
            )
        elif self._logger:
            self._logger.debug(
                f"NfcSource: recoverable read error "
                f"({self._protocol_errors}/{self.MAX_PROTOCOL_ERRORS}): {exc}"
            )
        return None

    def _drop_reader(self, reason: str) -> None:
        """Tear the reader down so SourceManager reconnects it."""
        if self._logger:
            self._logger.error(f"NfcSource: dropping reader — {reason}")
            self._logger.debug(traceback.format_exc())
        # Close before dropping the reference — otherwise the serial fd stays
        # open until GC and the next connect() races against it.
        try:
            if self._reader:
                self._reader.close()
        except Exception:
            pass
        self._reader = None
        if self._uart:
            try:
                self._uart.close()
            except Exception:
                pass
            self._uart = None
        # Reset tag state so the same card is re-detected after reconnect.
        self._last_uid_hex = None
        self._missing_count = 0
        self._absent_since = None
        self._protocol_errors = 0

    def _on_tag_present(self, target):
        """Handle a successful detection."""
        uid = target.uid
        uid_hex = uid.hex().upper()

        # Any sighting cancels a removal in progress.
        self._missing_count = 0
        self._absent_since = None

        if uid_hex == self._last_uid_hex:
            # Same tag still present. Metadata is cached, so this is a dict
            # lookup rather than another conversation with the tag.
            try:
                self.current_tag_meta = self._classify_tag(uid, target)
            except Exception:
                pass
            return None

        events: List[PluginEvent] = []

        # A different tag without an intervening absence — the user swapped
        # one for another between polls. The outgoing one still has to be
        # reported gone, or the registry keeps attributing a running game to a
        # tag that is no longer on the reader and lifting the new one does
        # nothing.
        if self._last_uid_hex:
            events.append(MediaEvent(
                kind=MediaEventKind.UNLOAD,
                source_type=SourceType.NFC,
                source_id=self.source_id,
                media_id=self._last_uid_hex,
                uri=self.current_tag_uri,
            ))

        self._last_uid_hex = uid_hex
        self.current_tag_uid = uid_hex

        load = self._build_load_event(target, uid, uid_hex)
        events.append(load)
        return events if len(events) > 1 else load

    def _build_load_event(self, target, uid: bytes, uid_hex: str) -> MediaEvent:
        """Classify the tag, read its content, and build the LOAD event."""
        try:
            meta = self._classify_tag(uid, target)
        except Exception:
            meta = None
        self.current_tag_meta = meta

        payload: Dict[str, Any] = {}
        if meta:
            payload["tag_meta"] = meta

        uri, records, unreadable, error = self._read_media(meta)
        self.current_tag_uri = uri

        # Release the tag now the conversation is over. A target left selected
        # stays in the ISO 14443-3 ACTIVE state and does not answer the next
        # anticollision, which makes a card resting on the reader go
        # intermittently invisible.
        release = getattr(self._reader, "release_target", None)
        if release is not None:
            try:
                release()
            except Exception:
                pass

        payload["ndef_records"] = ndef_codec.records_to_dicts(records)
        if unreadable:
            # "Unreadable" and "blank" look identical downstream unless we say
            # which it was, and only one of them is something the user can act
            # on. A tag we failed to read must never be reported as empty and
            # ready to pair.
            payload["unreadable"] = True
            if error:
                payload["error"] = error

        if meta and meta.get("random_uid"):
            # A randomly generated UID differs on every tap, so pairing by UID
            # can never match. Say so rather than letting the user pair a tag
            # that will never be recognised again.
            #
            # Overrides any earlier message: a tag can be both unreadable and
            # randomly identified — a phone emulating a card is both — and the
            # random ID is the more fundamental obstacle, because it defeats
            # pairing even if the tag were otherwise perfectly readable.
            payload["unstable_id"] = True
            payload["error"] = (
                "This tag reports a new random ID every tap, so it cannot be "
                "paired. Use an NTAG or Mifare Classic tag instead."
            )

        if self._logger:
            self._logger.info(
                f"NfcSource: new tag {uid_hex} "
                f"({(meta or {}).get('label') or (meta or {}).get('type', 'unknown')}), "
                f"uri={uri}"
            )

        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.NFC,
            source_id=self.source_id,
            media_id=uid_hex,
            uri=uri,
            payload=payload,
        )

    def _on_tag_absent(self) -> Optional[PluginEvent]:
        """Handle a clean "no tag present" answer from the reader."""
        if not self._last_uid_hex:
            return None

        now = time.monotonic()
        if self._absent_since is None:
            self._absent_since = now
        self._missing_count += 1

        elapsed = now - self._absent_since
        if self._missing_count < self.DEBOUNCE_THRESHOLD:
            return None
        if elapsed < self.removal_grace:
            return None

        removed_uid = self._last_uid_hex
        if self._logger:
            self._logger.info(
                f"NfcSource: tag removed: {removed_uid} "
                f"(absent {elapsed:.2f}s over {self._missing_count} polls)"
            )
        self._last_uid_hex = None
        self._missing_count = 0
        self._absent_since = None

        removed_uri = self.current_tag_uri
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.current_tag_meta = None

        return MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.NFC,
            source_id=self.source_id,
            media_id=removed_uid,
            uri=removed_uri,
        )

    # ── Reader factory ─────────────────────────────────────────────────

    async def _create_reader(self):
        """Return a reader instance based on configured settings."""
        rtype = self._settings.get("reader_type", "pn532_uart")
        path = self._effective_path or self._settings.get("device_path", "")
        baud = int(self._settings.get("baudrate", 115200))

        if rtype == "pn532_uart":
            if not PN532UARTReader:
                if self._logger:
                    self._logger.error(
                        "PN532UARTReader unavailable — nfc.reader failed to import. "
                        "This is a packaging problem, not missing hardware. "
                        f"Cause: {type(_READER_IMPORT_ERROR).__name__}: "
                        f"{_READER_IMPORT_ERROR}"
                    )
                    self._logger.error(
                        "Check that py_modules/ holds Linux x86_64 wheels — macOS "
                        "builds break the adafruit_pn532 -> Blinka -> cffi chain."
                    )
                return None
            return PN532UARTReader(path, baud, logger=self._logger)
        elif rtype == "acr122u":
            try:
                from nfc_core.acr122u_backend import ACR122UReader
                return ACR122UReader(logger=self._logger)
            except ImportError:
                if self._logger:
                    self._logger.error("ACR122U backend requires pyscard library")
                return None
        elif rtype == "proxmark":
            try:
                from nfc_core.proxmark_backend import ProxmarkReader
                return ProxmarkReader(path, logger=self._logger)
            except ImportError:
                if self._logger:
                    self._logger.error("Proxmark backend not available")
                return None
        elif rtype == "nfcpy":
            try:
                from nfc_core.nfcpy_backend import NfcPyReader
                return NfcPyReader(path, logger=self._logger)
            except ImportError:
                if self._logger:
                    self._logger.error("nfcpy backend requires nfcpy library")
                return None
        else:
            if self._logger:
                self._logger.warning(f"Unknown reader type: {rtype}")
            return None

    # ── Tag classification ─────────────────────────────────────────────

    def _classify_tag(self, uid: bytes, target=None) -> Dict[str, Any]:
        """Return metadata about the presented tag.

        Classification is read from the tag's ISO 14443-3 activation data —
        ATQA and SAK — which every Type A tag announces during anticollision,
        before a single command is sent to it.  It costs nothing and is exact.

        The previous approach was to *try* authenticating as a Mifare Classic
        and infer the family from whether that worked.  A failed
        authentication deselects the tag, so the probe destroyed the very
        session the next probe needed: only the first key in a list could ever
        succeed, an NTAG came out labelled DESFire, and the read path then
        addressed it with the wrong protocol.

        Results are cached per UID.
        """
        uid_hex = uid.hex().upper()

        # Check cache first
        if uid_hex in self._tag_classification_cache:
            return self._tag_classification_cache[uid_hex]

        meta: Dict[str, Any] = {
            "uid": uid_hex,
            "type": tag_identity.TYPE_UNKNOWN,
            "label": "unknown tag",
            "capacity_bytes": 0,
            "protected": False,
            "random_uid": tag_identity.is_random_uid(uid) if isinstance(uid, bytes) else False,
        }

        if target is None:
            target = self._last_target()

        sak = getattr(target, "sak", None) if _is_target(target) else None
        atqa = getattr(target, "atqa", None) if _is_target(target) else None
        protocol = getattr(target, "protocol", None) if _is_target(target) else None

        if sak is not None:
            meta.update(tag_identity.classify_sak(sak, atqa))
            meta["sak"] = f"{sak:02X}"
            if atqa is not None:
                meta["atqa"] = f"{atqa:04X}"
        elif protocol and protocol != tag_identity.PROTO_106A:
            meta.update(self._classify_non_type_a(protocol))
        else:
            meta.update(self._probe_family(uid_hex))

        if protocol:
            meta["protocol"] = protocol

        # Page-addressed families report their exact model and size through
        # GET_VERSION. This is the only non-destructive way to tell an NTAG213
        # from a 215 from a 216, and it is never sent to a tag whose SAK says
        # it is not in this family.
        if meta.get("type") in tag_identity.PAGE_ADDRESSED:
            self._refine_page_addressed(meta, uid_hex)
        elif meta.get("type") in (tag_identity.TYPE_CLASSIC, tag_identity.TYPE_CLASSIC_MINI):
            blocks = self._iter_mifare_data_blocks()
            meta["capacity_bytes"] = len(blocks) * 16

        meta["ndef_capable"] = meta.get("type") in tag_identity.NDEF_CAPABLE

        self._cache_tag_classification(uid_hex, meta)
        return meta

    def _last_target(self):
        """Activation data from the reader's most recent detection, if any."""
        getter = getattr(self._reader, "last_target", None)
        if getter is None:
            return None
        try:
            result = getter()
        except Exception:
            return None
        return result if _is_target(result) else None

    @staticmethod
    def _classify_non_type_a(protocol: str) -> Dict[str, Any]:
        """Family for a tag detected on something other than ISO 14443-3A."""
        if protocol == tag_identity.PROTO_106B:
            return {"type": tag_identity.TYPE_ISO14443B,
                    "label": "ISO 14443-B card", "capacity_bytes": 0}
        if protocol in (tag_identity.PROTO_212F, tag_identity.PROTO_424F):
            return {"type": tag_identity.TYPE_FELICA,
                    "label": "FeliCa card", "capacity_bytes": 0}
        if protocol == tag_identity.PROTO_JEWEL:
            return {"type": tag_identity.TYPE_JEWEL,
                    "label": "Topaz / Jewel tag", "capacity_bytes": 0}
        return {"type": tag_identity.TYPE_UNKNOWN,
                "label": f"{protocol} tag", "capacity_bytes": 0}

    def _probe_family(self, uid_hex: str) -> Dict[str, Any]:
        """Identify a tag on a backend that cannot report SAK.

        Ordered so the non-destructive test comes first: reading the NTAG
        capability container is a plain read, and a Mifare Classic answers it
        with a refusal because page 3 of a Classic is a sector trailer.  Only
        if that says nothing do we fall back to an authentication probe, and
        even then the tag is re-selected between key attempts.
        """
        pages = self._read_ntag_user_pages(uid_hex)
        if pages:
            return {
                "type": tag_identity.TYPE_NTAG,
                "label": "NTAG21x / Ultralight",
                "capacity_bytes": pages * 4,
            }

        if self._probe_mifare_classic(uid_hex):
            return {
                "type": tag_identity.TYPE_CLASSIC,
                "label": "Mifare Classic",
                "capacity_bytes": 0,   # filled in by the caller
            }

        return {
            "type": tag_identity.TYPE_UNKNOWN,
            "label": "unrecognised tag",
            "capacity_bytes": 0,
            "protected": True,
        }

    def _probe_mifare_classic(self, uid_hex: str) -> bool:
        """Try the known Classic keys against sector 1, re-selecting between.

        The old loop broke out on the first exception and never re-selected,
        so the second and third keys were tried against a card that had
        already been deselected by the first failure — which is why
        NDEF-formatted Classic tags, whose data key is the second in the list,
        were never recognised.
        """
        try:
            uid = bytes.fromhex(uid_hex)
        except ValueError:
            return False
        handler = get_handler(
            tag_identity.TYPE_CLASSIC, uid, key_manager=self._key_manager
        )
        if handler is None:
            return False
        try:
            return bool(handler.authenticate_sector(self._reader, 1))
        except Exception:
            return False

    def _refine_page_addressed(self, meta: Dict[str, Any], uid_hex: str) -> None:
        """Pin down the exact NTAG / Ultralight model and usable capacity."""
        version = None
        getter = getattr(self._reader, "get_version", None)
        if getter is not None:
            try:
                version = getter()
            except Exception:
                version = None
        if not isinstance(version, (bytes, bytearray)):
            version = None

        parsed = tag_identity.parse_version(bytes(version)) if version else None
        if parsed:
            meta["type"] = parsed["type"]
            meta["label"] = parsed["label"]
            meta["capacity_bytes"] = parsed["user_bytes"]
            self._ntag_pages_cache[uid_hex] = min(
                parsed["user_pages"], _NTAG_MAX_USER_PAGES
            )
            return

        # No GET_VERSION: fall back to the capability container, which gives
        # the NDEF area size even when the model stays unknown.
        pages = self._read_ntag_user_pages(uid_hex)
        if pages:
            meta["capacity_bytes"] = pages * 4
            if meta["type"] == tag_identity.TYPE_ULTRALIGHT:
                # SAK 0x00 covers both families; a readable NDEF capability
                # container makes NTAG much the likelier of the two.
                meta["type"] = tag_identity.TYPE_NTAG
                meta["label"] = "NTAG21x"
        else:
            meta["capacity_bytes"] = 0
            meta["protected"] = True

    def _cache_tag_classification(self, uid_hex: str, meta: Dict[str, Any]) -> None:
        """Cache tag classification with LRU eviction."""
        self._tag_classification_cache[uid_hex] = meta
        if len(self._tag_classification_cache) > self._tag_cache_max_size:
            oldest_key = next(iter(self._tag_classification_cache))
            del self._tag_classification_cache[oldest_key]

    # ── NDEF Read ──────────────────────────────────────────────────────

    def _read_ntag_user_pages(self, uid_hex: Optional[str] = None) -> Optional[int]:
        """Number of user pages, read from the tag's Capability Container.

        Page 3 of every NTAG21x is a 4-byte CC: magic ``0xE1``, version,
        size/8, access. Byte 2 times 8 is the NDEF data area in bytes, which is
        exactly the user memory — 144 bytes on an NTAG213, 496 on a 215, 872 on
        a 216. Returns None when the CC cannot be read or is not an NDEF tag,
        leaving the caller to fall back.
        """
        if uid_hex and uid_hex in self._ntag_pages_cache:
            return self._ntag_pages_cache[uid_hex]

        try:
            cc = self._reader.ntag2xx_read_block(_NTAG_CC_PAGE)
        except Exception:
            return None
        if not cc or len(cc) < 3 or cc[0] != _NTAG_NDEF_MAGIC:
            return None
        pages = (cc[2] * 8) // 4
        if pages <= 0 or pages > _NTAG_MAX_USER_PAGES:
            return None
        if uid_hex:
            self._ntag_pages_cache[uid_hex] = pages
        return pages

    def _iter_ntag_pages(self, uid_hex: Optional[str] = None):
        """Yield user-writable pages for NTAG21x devices.

        Sized from the tag itself. This used to return pages 4–133
        unconditionally — past the end of NTAG215 user memory and into the
        configuration pages — so a long URI written to a smaller NTAG213 ran
        past the end of the tag, page writes failing silently one by one while
        the write reported success.
        """
        pages = self._read_ntag_user_pages(uid_hex)
        if pages is None:
            # Unreadable CC: keep the NTAG215 assumption rather than refusing
            # to write at all. Wrong only for tags that already could not tell
            # us anything.
            pages = _NTAG_FALLBACK_USER_PAGES
            self._log_once(
                "ntag-cc-unreadable", "warning",
                "NFC: could not read tag capability container; assuming "
                f"{pages} user pages ({pages * 4} bytes)",
            )
        for page in range(4, 4 + pages):
            yield page

    def _iter_mifare_data_blocks(self):
        """Return list of writable data blocks (skip trailer blocks)."""
        blocks = []
        for block in range(4, 63):  # FIRST_DATA_BLOCK to MAX_BLOCK
            if block % 4 == 3:
                continue
            blocks.append(block)
        return blocks

    def _select(self) -> Optional[bytes]:
        """Re-activate the tag and return its UID, or ``None``.

        Data exchange addresses the target that activation selected, so a read
        must always start here — particularly after classification, which on
        some paths leaves the tag deselected.
        """
        selector = getattr(self._reader, "select_target", None)
        if selector is not None:
            try:
                target = selector(timeout=0.2)
            except ReaderError:
                raise
            except Exception:
                target = None
            if _is_target(target):
                return target.uid
        uid = self._reader.read_uid(timeout=0.1)
        return bytes(uid) if uid else None

    def _read_media(self, meta: Optional[Dict[str, Any]]):
        """Read the tag's content, retrying a lossy link before giving up.

        Returns ``(uri, records, unreadable, error)``.  ``unreadable`` is the
        important distinction: a tag we failed to read is not a blank tag, and
        reporting one as the other sent users off to pair a card that already
        had a URI on it.  The old code had no retry at all — a single dropped
        frame was cached as "blank" until the tag was physically lifted and
        re-presented.
        """
        family = (meta or {}).get("type", tag_identity.TYPE_UNKNOWN)

        if meta and not meta.get("ndef_capable", True):
            label = meta.get("label", family)
            return None, [], True, (
                f"{label} is not a tag this plugin can store a link on. "
                f"Use an NTAG21x or Mifare Classic tag."
            )

        last_error = None
        for attempt in range(self.CONTENT_READ_ATTEMPTS):
            if self._stopping:
                return None, [], True, "reader stopped"
            try:
                records, raw = self._read_ndef_records(return_raw=True)
            except ReaderTransportError:
                raise
            except ReaderProtocolError as e:
                last_error = str(e)
                recover = getattr(self._reader, "recover", None)
                if recover is not None:
                    try:
                        recover()
                    except Exception:
                        pass
                continue
            except Exception as e:
                last_error = str(e)
                continue

            uri = ndef_codec.first_uri(records)
            if uri:
                return uri, records, False, None
            if records:
                # Readable, but carries something other than a URI.
                return None, records, False, None
            if raw:
                # We got bytes off the tag; it is genuinely blank or holds
                # something we cannot decode. Retrying will not change that.
                return None, [], False, None

            last_error = last_error or "no data returned from tag"

        if self._logger:
            self._logger.warning(
                f"NfcSource: could not read tag content after "
                f"{self.CONTENT_READ_ATTEMPTS} attempts: {last_error}"
            )
        return None, [], True, last_error

    def _read_ndef_records(self, return_raw: bool = False):
        """Read and return all NDEF records present on the current tag."""
        import ndef

        uid = self._select()
        if not uid:
            return ([], b"") if return_raw else []

        tag_meta = self._classify_tag(uid)
        family = tag_meta.get("type", tag_identity.TYPE_UNKNOWN)
        if family == tag_identity.TYPE_UNKNOWN:
            # Nothing said what this is. The page-addressed protocol is the
            # safer guess: its read command is also valid on a Classic, while
            # the reverse returns four overlapping pages per call and
            # scrambles the data.
            family = tag_identity.TYPE_NTAG

        if family in tag_identity.PAGE_ADDRESSED:
            handler = get_handler(
                family, uid, key_manager=self._key_manager,
                pages=list(self._iter_ntag_pages(uid.hex().upper())),
            )
        elif family in (tag_identity.TYPE_CLASSIC, tag_identity.TYPE_CLASSIC_MINI):
            handler = get_handler(
                family, uid, key_manager=self._key_manager,
                blocks=self._iter_mifare_data_blocks(),
            )
        else:
            handler = get_handler(family, uid, key_manager=self._key_manager)
        if handler is None:
            return ([], b"") if return_raw else []

        data = handler.read_ndef(self._reader)
        if not data:
            return ([], b"") if return_raw else []

        records = self._decode_records(bytes(data), ndef)
        return (records, bytes(data)) if return_raw else records

    @staticmethod
    def _decode_records(data: bytes, ndef) -> List[Any]:
        """Turn a raw tag data area into NDEF records.

        Walks the TLV structure properly rather than assuming the message
        starts at byte 0, which is false for any tag carrying a lock-control
        or memory-control TLV ahead of it, and for messages over 254 bytes
        that use the long length form.
        """
        records: List[Any] = []
        message = ndef_codec.find_ndef_message(data)

        if message:
            try:
                for rec in ndef.message_decoder(message):
                    records.append(rec)
            except Exception:
                records = []

        if not records:
            # Either the TLV would not parse or the message would not decode.
            # A URI visible in the raw bytes is still worth launching.
            uri = ndef_codec.extract_uri_fallback(message or data)
            if uri:
                try:
                    records.append(ndef.UriRecord(uri))
                except Exception:
                    pass

        return records

    def _read_ndef_uri(self) -> Optional[str]:
        """Return the first URI record's value, or None."""
        return ndef_codec.first_uri(self._read_ndef_records())

    # ── NDEF Write (for pairing) ───────────────────────────────────────

    def can_write(self) -> bool:
        return True

    async def write_uri(
        self, media_id: str, uri: str, title: str = ""
    ) -> Tuple[bool, Optional[str]]:
        """Source-generic pairing entry point.

        ``media_id`` is the tag UID as hex, the form carried by MediaEvents.

        ``title`` is accepted and ignored: an NDEF URI record holds a URI and
        nothing else, and adding a text record to carry the game name would
        spend scarce tag memory storing what the app id already resolves to.
        Storage media, whose payload is a JSON file with room to spare, do
        record it.

        Offloaded for the same reason as :meth:`poll`: writing a tag is a
        page-at-a-time conversation with sleeps between key attempts, and it
        is awaited from the event loop during pairing. Blocking there froze
        the panel at exactly the moment it is showing the user a spinner.
        """
        try:
            uid = bytes.fromhex(media_id)
        except (ValueError, TypeError):
            return False, f"invalid tag UID {media_id!r}"
        return await asyncio.to_thread(self.write_ndef_uri, uid, uri)

    def write_ndef_uri(self, uid: bytes, uri: str) -> Tuple[bool, Optional[str]]:
        """Write a URI as an NDEF URI record to the tag.

        Holds the reader for the whole write, so a poll running concurrently
        on the source manager's thread cannot interleave commands with it.

        Returns ``(True, None)`` on success, ``(False, error_message)`` on failure.
        """
        with self._io_lock:
            return self._write_ndef_uri_locked(uid, uri)

    def _write_ndef_uri_locked(self, uid: bytes, uri: str) -> Tuple[bool, Optional[str]]:
        import ndef

        try:
            record = ndef.UriRecord(uri)
            message = b"".join(ndef.message_encoder([record]))
            tlv = ndef_codec.encode_tlv(message)
        except Exception as e:
            return False, f"Failed to create NDEF record: {e}"

        try:
            uid_hex = uid.hex().upper()
        except AttributeError:
            uid_hex = str(uid)

        meta = self._tag_classification_cache.get(uid_hex)
        family = (meta or {}).get("type")

        if family in (tag_identity.TYPE_CLASSIC, tag_identity.TYPE_CLASSIC_MINI):
            authenticated = True
        elif family in tag_identity.PAGE_ADDRESSED:
            authenticated = False
        else:
            # No classification to go on (a direct call, or a backend that
            # cannot report SAK). Probe, non-destructively first.
            if self._read_ntag_user_pages(uid_hex) is not None:
                authenticated = False
            else:
                authenticated = self._probe_mifare_classic(uid_hex)
                if not authenticated:
                    # Neither a Classic we can open nor an NDEF-formatted
                    # NTAG. Writing anyway is what produced "Write failed at
                    # page 4" on a keyring fob: every page write is rejected,
                    # one at a time, and the first refusal is reported as if
                    # it were a transient error.
                    return False, (
                        "Unsupported tag — not a Mifare Classic or an NDEF-"
                        "formatted NTAG. It may need formatting as NDEF first."
                    )

        if authenticated:
            handler = get_handler(
                tag_identity.TYPE_CLASSIC,
                uid,
                key_manager=self._key_manager,
                blocks=self._iter_mifare_data_blocks(),
            )
            handler_type = tag_identity.TYPE_CLASSIC
        else:
            handler = get_handler(
                tag_identity.TYPE_NTAG,
                uid,
                key_manager=self._key_manager,
                pages=list(self._iter_ntag_pages(uid_hex)),
            )
            handler_type = tag_identity.TYPE_NTAG
        if handler is None:
            return False, f"No handler available for tag type {handler_type}"

        # Size the write from the URI, not from the encoded message: the
        # check must hold even if the encoder produced something unexpected.
        required = max(len(tlv), ndef_codec.uri_storage_size(uri))
        max_payload = handler.get_capacity()
        if required > max_payload:
            # Checked before the first page write, so a tag that cannot hold
            # the URI is left exactly as it was rather than half-written.
            return False, (
                f"Tag too small: needs {required} bytes, holds {max_payload}"
            )

        try:
            return handler.write_ndef(self._reader, bytes(tlv))
        except Exception as e:
            return False, str(e)
