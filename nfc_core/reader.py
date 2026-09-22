"""Reader abstraction for NFC hardware.

This module defines a generic :class:`Reader` interface used by the
Decky Links plugin plus a concrete implementation for the PN532 over
UART.  The goal is to isolate transport- and chip-specific details
from the main plugin logic so additional backends can be added later.

The interface is intentionally thin: the plugin still relies on the
PN532-style method names (e.g. ``mifare_classic_read_block``) so the
wrapper merely forwards calls.  Higher-level refactors can introduce a
more uniform API once multiple readers exist.

Detection, however, is *not* left to the stock driver.  See
:class:`PN532UARTReader` for why.
"""

import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from nfc_core import tag_identity

# The serial and adafruit_pn532 imports are lazy to allow tests to run
# without hardware.  Mocks supply substitute modules via sys.modules.


# ── Error taxonomy ──────────────────────────────────────────────────────
# The single most damaging thing the old code did was treat every exception
# the same: close the serial port and force a full reconnect.  A desynced
# frame — which is recoverable in milliseconds by aborting and resending —
# cost several seconds of dead reader and a "disconnected" banner in the UI.


class ReaderError(Exception):
    """Base class for reader failures."""


class ReaderProtocolError(ReaderError):
    """A recoverable framing or command-sequencing failure.

    The link is fine; the conversation is out of step.  The fix is to abort
    the command in flight, drain the buffer and try again — not to tear down
    the connection.
    """


class ReaderTransportError(ReaderError):
    """The underlying link is gone: unplugged, closed, or in error.

    This is the only condition that justifies dropping the reader and
    reconnecting.
    """


def classify_exception(exc: BaseException) -> ReaderError:
    """Sort a driver exception into transport-fatal or protocol-recoverable.

    ``OSError`` (which ``serial.SerialException`` subclasses) means the file
    descriptor is unusable.  Everything else — checksum mismatches, missing
    ACKs, unexpected command responses, ``BusyError``, and the ``TypeError``
    the Adafruit driver raises when it subscripts a ``None`` response after a
    timeout — is the conversation being out of step, and is retryable.
    """
    if isinstance(exc, OSError):
        return ReaderTransportError(str(exc) or exc.__class__.__name__)
    return ReaderProtocolError(f"{exc.__class__.__name__}: {exc}")


# ── Detection result ────────────────────────────────────────────────────


class TargetInfo:
    """What a single detection told us about the tag on the reader.

    ``uid`` is the only field every backend can supply.  ``atqa`` and ``sak``
    come from ISO 14443-3 anticollision and identify the tag family exactly,
    for free, before a single command is sent to it — which is why this
    plugin no longer guesses by trying to authenticate.
    """

    __slots__ = ("uid", "atqa", "sak", "ats", "protocol", "target_number")

    def __init__(
        self,
        uid: bytes,
        atqa: Optional[int] = None,
        sak: Optional[int] = None,
        ats: Optional[bytes] = None,
        protocol: str = tag_identity.PROTO_106A,
        target_number: int = 1,
    ):
        self.uid = uid
        self.atqa = atqa
        self.sak = sak
        self.ats = ats
        self.protocol = protocol
        self.target_number = target_number

    @property
    def uid_hex(self) -> str:
        return self.uid.hex().upper()

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"uid": self.uid_hex, "protocol": self.protocol}
        if self.atqa is not None:
            out["atqa"] = f"{self.atqa:04X}"
        if self.sak is not None:
            out["sak"] = f"{self.sak:02X}"
        return out

    def __repr__(self) -> str:
        sak = "--" if self.sak is None else f"{self.sak:02X}"
        atqa = "----" if self.atqa is None else f"{self.atqa:04X}"
        return f"<TargetInfo {self.uid_hex} atqa={atqa} sak={sak} {self.protocol}>"


class Reader(ABC):
    """Abstract base class for NFC readers.

    Subclasses should implement ``connect``/``close`` plus a minimal set of
    convenience helpers.  The :meth:`__getattr__` hook in
    :class:`PN532UARTReader` permits delegating additional methods directly
    to the underlying driver so the existing plugin code can continue to
    call familiar names without change.

    Interface Contract:
    - Implementations must support both synchronous and asynchronous operations
    - All methods should be thread-safe or clearly document thread constraints
    - Connection state must be queryable via is_connected()
    - Firmware version should be available after successful connection
    - UID reading should support configurable timeout
    - Additional hardware-specific methods can be delegated via __getattr__

    Supported Methods (minimum):
    - connect(): Establish connection to hardware
    - close(): Release hardware resources
    - is_connected(): Query connection status
    - firmware_version(): Get firmware/version tuple
    - read_uid(timeout): Read passive target UID
    - read_uid_iso14443b(timeout): Read ISO-14443B UID

    Optional, with a default derived from ``read_uid``:
    - read_target(timeout): Full activation data (UID + ATQA + SAK)
    - select_target(timeout): Re-activate the tag before a data exchange
    - release_target(): Release the tag so the field can re-activate it
    - get_version(): NTAG/Ultralight GET_VERSION response

    Additional Methods (delegated):
    - mifare_classic_authenticate_block(uid, block, key_type, key)
    - mifare_classic_read_block(block)
    - mifare_classic_write_block(block, data)
    - ntag2xx_read_block(page)
    - ntag2xx_write_block(page, data)
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Attempt to initialise the reader hardware.

        Returns ``True`` on success (ready for use) or ``False`` on
        failure.  The plugin will treat a failure as if no reader were
        present and will retry periodically.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any resources held by the reader."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return ``True`` if the reader believes it is currently usable."""

    @abstractmethod
    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """Return the firmware/version tuple, or ``None`` if unavailable."""

    @abstractmethod
    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Low‑level helper for reading a passive target UID.

        The semantics mirror ``PN532_UART.read_passive_target``; other
        backends should provide a compatible behaviour.
        """

    @abstractmethod
    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID from ISO-14443B tag.

        Returns UID bytes or None if no tag present.
        """

    # ── Optional capabilities, with safe defaults ───────────────────────

    def read_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Return full activation data for the tag present, or ``None``.

        The default implementation reports only the UID, which is all a
        backend that cannot expose ATQA/SAK is able to say.  Callers must
        cope with ``atqa`` and ``sak`` being ``None``.
        """
        uid = self.read_uid(timeout=timeout)
        if not uid:
            return None
        return TargetInfo(uid=bytes(uid))

    def select_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Re-activate the tag in the field before a data exchange.

        Needed after any operation that leaves the tag deselected — a failed
        MIFARE authentication being the common one.  Defaults to a plain
        detection, which for most backends is the same thing.
        """
        return self.read_target(timeout=timeout)

    def release_target(self) -> None:
        """Release the currently selected tag.  Optional; default is a no-op."""

    def get_version(self) -> Optional[bytes]:
        """NTAG/Ultralight GET_VERSION response, or ``None`` if unsupported."""
        return None

    def recover(self) -> None:
        """Resynchronise after a protocol error.  Optional; default no-op."""

    @property
    def supports_target_info(self) -> bool:
        """True when :meth:`read_target` can report ATQA/SAK."""
        return False


# ── PN532 command set (NXP UM0701-02) ───────────────────────────────────
_CMD_RFCONFIGURATION = 0x32
_CMD_INDATAEXCHANGE = 0x40
_CMD_INLISTPASSIVETARGET = 0x4A
_CMD_INRELEASE = 0x52
_CMD_INAUTOPOLL = 0x60

# RFConfiguration CfgItem 0x05 = MaxRetries.
_CFG_MAX_RETRIES = 0x05
# MxRtyATR, MxRtyPSL, MxRtyPassiveActivation.  The last one is the important
# value: its power-on default is 0xFF, meaning "retry until a card appears",
# which leaves InListPassiveTarget running on the chip indefinitely after the
# host has already given up waiting.  Every subsequent command then collides
# with a response to a command the host has forgotten about, which is the
# root cause of the reader appearing to drop in and out.
_MAX_RETRY_ATR = 0xFF
_MAX_RETRY_PSL = 0x01
_MAX_RETRY_PASSIVE_ACTIVATION = 0x02

# InAutoPoll target types we ask for.  Ordered cheapest-first; the PN532 tries
# each in turn within one poll period.
_POLL_TYPE_GENERIC_106A = 0x00   # ISO 14443-4A, MIFARE, NTAG, DEP
_POLL_TYPE_ISO14443B = 0x03      # ISO 14443-3B / 14443-4B
_POLL_TYPE_JEWEL = 0x04          # Innovision Topaz / Jewel
_POLL_TYPE_FELICA_212 = 0x11
_POLL_TYPE_FELICA_424 = 0x12

# Mapping from the Type byte in an InAutoPoll response to a protocol name.
_POLL_TYPE_PROTOCOL = {
    0x00: tag_identity.PROTO_106A,
    0x10: tag_identity.PROTO_106A,
    0x20: tag_identity.PROTO_106A,
    0x01: tag_identity.PROTO_212F,
    0x11: tag_identity.PROTO_212F,
    0x02: tag_identity.PROTO_424F,
    0x12: tag_identity.PROTO_424F,
    0x03: tag_identity.PROTO_106B,
    0x23: tag_identity.PROTO_106B,
    0x04: tag_identity.PROTO_JEWEL,
}

# InListPassiveTarget baud rates.
_BAUD_106A = 0x00
_BAUD_106B = 0x03

# PollNr = 1 single pass, Period = 1 × 150 ms.  Bounded by construction, so
# the command always completes and always leaves the chip idle afterwards.
_AUTOPOLL_POLL_NR = 0x01
_AUTOPOLL_PERIOD = 0x01
# How long to allow for one InAutoPoll sweep.
#
# This is a ceiling, not a delay: with a tag on the reader the chip answers on
# the first target type in a few tens of milliseconds, and the poll costs
# nothing near this. It only binds when *no* tag is present, because then the
# chip works through every requested type — Type A, Type B, two FeliCa rates
# and Jewel — before it can honestly report nothing, and that sweep plus the
# polling period comfortably exceeds a third of a second.
#
# Set too tight, the symptom is diabolical: every no-tag poll fails while
# every tag-present poll succeeds, so the reader reports "no response to
# InAutoPoll" continuously, trips the consecutive-error limit, and reconnects
# in a loop — yet tags still read correctly in the gaps, which makes it look
# like failing hardware rather than a timeout.
_AUTOPOLL_MIN_TIMEOUT = 1.0
# Consecutive InAutoPoll failures before concluding this module cannot do it
# and dropping to InListPassiveTarget for the rest of the connection.
_AUTOPOLL_FALLBACK_AFTER = 3

# Host-to-PN532 ACK frame.  Sending this aborts whatever command the chip is
# currently executing — the documented way out of a desync.
_ACK_FRAME = b"\x00\x00\xff\x00\xff\x00"

# NTAG / Ultralight GET_VERSION.
_TAG_CMD_GET_VERSION = 0x60


class PN532UARTReader(Reader):
    """PN532 reader connected over a UART serial port.

    Wraps :class:`adafruit_pn532.uart.PN532_UART` and exposes the same
    methods transparently, so a caller can treat the instance as if it *were*
    the underlying PN532 object thanks to ``__getattr__``.

    Detection deliberately does **not** use the stock
    ``read_passive_target``.  That method sends ``InListPassiveTarget`` and
    abandons it host-side on timeout, while the chip — whose passive
    activation retry count defaults to "forever" — keeps scanning.  The next
    poll then writes a command the busy chip will not acknowledge, and when a
    card finally arrives its response is read as the ACK for a different
    command.  The result is a ``RuntimeError`` mid-poll, which the old code
    answered by closing the serial port.

    Two changes remove the whole failure mode:

    * ``MaxRetries`` is configured at connect time so passive activation is
      bounded and the command always returns.
    * Detection uses ``InAutoPoll``, which is self-terminating by design and
      additionally sweeps Type B, FeliCa and Jewel in the same pass — so the
      range of tags the plugin sees widens as a side effect of making it
      stable.
    """

    def __init__(self, device_path: str, baudrate: int, logger=None):
        # _reader must exist before anything else can fail, because
        # __getattr__ consults it and would otherwise recurse forever.
        self._reader = None
        self.device_path = device_path
        self.baudrate = baudrate
        self.logger = logger
        self.uart = None
        # Set once an InAutoPoll attempt fails outright, so a PN532 clone with
        # firmware that predates the command degrades to the older path
        # instead of failing every poll.
        self._autopoll_unsupported = False
        # Consecutive InAutoPoll failures on this connection.
        self._autopoll_failures = 0
        self._last_target: Optional[TargetInfo] = None
        # Why the last connect() failed, as {"code", "message"}, for the source
        # to hand to the panel. connect() returns a bare bool, and "the port is
        # held by another copy of this plugin" needs a different response from
        # the user than "no reader is plugged in" — so the reason has to
        # survive the call rather than only reaching the log.
        self.last_error: Optional[dict] = None

    # ── Connection lifecycle ────────────────────────────────────────────

    async def connect(self) -> bool:
        # ensure the path is present before attempting to open serial
        if not os.path.exists(self.device_path):
            return False
        import asyncio
        return await asyncio.to_thread(self._connect_blocking)

    def _connect_blocking(self) -> bool:
        """Synchronous connect with a hard 5-second deadline via threading.Timer.

        asyncio.wait_for cannot cancel a running thread on Python 3.9, so we
        use threading.Timer to force-close the serial port when the deadline
        expires.  Closing the port causes the adafruit_pn532 I/O loops to
        raise within one poll cycle (~10 ms) rather than blocking for 30 s.

        The timer must start BEFORE PN532_UART() is constructed because the
        constructor itself calls _wakeup() → SAM_configuration() and reads
        firmware_version — all of which can block on the wrong device.
        """
        import serial
        import threading
        from adafruit_pn532.uart import PN532_UART

        timed_out = [False]

        def _on_timeout():
            timed_out[0] = True
            self.close()  # closing the port causes in_waiting/write to raise

        timer = threading.Timer(5.0, _on_timeout)
        # Cleared per attempt, so a reason recorded by a previous failure can
        # never be reported for this one.
        self.last_error = None
        try:
            self.uart = serial.Serial(
                self.device_path,
                baudrate=self.baudrate,
                timeout=0.1,
                write_timeout=0.5,
                # Deliberately *not* exclusive=True. That sets TIOCEXCL and
                # additionally takes pyserial's own flock — which meant
                # pyserial raised first and _lock_port() below never ran, so a
                # port held by a stale backend surfaced as the constructor's
                # "Could not exclusively lock port" with no indication of who
                # was holding it or what to do about it. TIOCEXCL exempts
                # root, and this plugin runs as root, so it was never the
                # thing protecting the port anyway: the flock is. Taking it
                # ourselves is what lets the failure be named.
            )
            if not self._lock_port():
                return False
            # Start timer BEFORE PN532_UART.__init__ — the constructor calls
            # _wakeup() which runs SAM_configuration + firmware_version and
            # can block for 30 s on a wrong/unresponsive device without this.
            timer.start()
            self._reader = PN532_UART(self.uart, debug=False)
            if timed_out[0]:
                # The port opened but nothing answered the PN532 handshake.
                # Almost always the wrong port — a USB-serial adapter that is
                # not a reader enumerates identically.
                self.last_error = {
                    "code": "no_response",
                    "message": (
                        f"No NFC reader answered on {self.device_path}. Check "
                        f"the reader is plugged in and set to UART mode."
                    ),
                }
                return False

            timer.cancel()
            # Brief settle — some PN532 modules glitch the serial line
            # immediately after SAM configuration before accepting polls.
            time.sleep(0.5)

            self._autopoll_unsupported = False
            self._autopoll_failures = 0
            self._last_target = None
            self._configure_retries()
            return True

        except Exception as e:
            if self.logger:
                self.logger.error(f"PN532UARTReader.connect failed: {e}")
            # Only if _lock_port() has not already said something more
            # specific — "the port is busy" outranks the generic open failure
            # it would otherwise be reported as.
            if self.last_error is None:
                self.last_error = {
                    "code": "open_failed",
                    "message": f"Could not open {self.device_path}: {e}",
                }
            self.close()
            return False
        finally:
            timer.cancel()

    def _lock_port(self) -> bool:
        """Take an exclusive advisory lock on the open serial port.

        Two backend processes of this plugin sharing one PN532 is not a
        theoretical hazard — it happens routinely, because reloading the
        plugin does not reliably kill the previous process.  ``poll()`` runs
        its work in a thread via ``asyncio.to_thread``, and a thread cannot be
        cancelled: unload cancels the awaiting task, the worker thread keeps
        running with the port open, and because ThreadPoolExecutor threads are
        non-daemon the interpreter cannot exit either.  The old process lives
        on holding ``/dev/ttyUSB0`` while a new one opens it alongside.

        The result is indistinguishable from broken hardware.  Both processes
        write PN532 command frames and race to read the replies, so each one
        sees missing ACKs, frames that start mid-stream, checksum failures and
        responses to commands it never sent — while ``dmesg`` shows a
        perfectly stable USB link, and the tag on the reader reads correctly
        one second and not at all the next.

        ``flock`` is the right instrument because both contenders are us: it
        is advisory, which is enough for cooperating processes, and the kernel
        drops it when the fd closes or the process dies, so a genuinely dead
        instance never leaves the port locked.  ``TIOCEXCL`` cannot do this
        job alone — it exempts root, and this plugin runs as root.
        """
        try:
            import fcntl
        except ImportError:
            return True   # not POSIX; nothing to do

        try:
            fd = self.uart.fileno()
        except Exception:
            fd = None
        if not isinstance(fd, int) or fd < 0:
            # A port object with no real file descriptor — a stub, or a
            # transport that is not a tty. Nothing to lock, and refusing to
            # connect over that would be worse than not locking.
            return True

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.last_error = {
                "code": "port_busy",
                "message": (
                    f"{self.device_path} is already in use by another copy of "
                    f"this plugin. Restart the plugin loader from Decky's "
                    f"settings to clear it."
                ),
            }
            if self.logger:
                self.logger.error(
                    f"{self.device_path} is already in use by another instance "
                    f"of this plugin. Refusing to open it a second time — two "
                    f"readers on one serial port corrupt each other's frames "
                    f"and look exactly like failing hardware. Check with "
                    f"`sudo lsof {self.device_path}`; if a stale backend is "
                    f"listed, restart the plugin loader."
                )
            self.close()
            return False

    def _configure_retries(self) -> None:
        """Bound passive activation so detection commands always return.

        Without this the chip retries activation forever and is still running
        the previous poll's command when the next one arrives.  Failure here
        is logged but not fatal: the reader still works, just less reliably,
        and refusing to connect over it would be worse.
        """
        try:
            self._reader.call_function(
                _CMD_RFCONFIGURATION,
                params=[
                    _CFG_MAX_RETRIES,
                    _MAX_RETRY_ATR,
                    _MAX_RETRY_PSL,
                    _MAX_RETRY_PASSIVE_ACTIVATION,
                ],
                response_length=0,
                timeout=0.5,
            )
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"PN532: could not set MaxRetries ({e}); detection may be "
                    f"less reliable on this module"
                )

    def close(self) -> None:
        if self.uart:
            try:
                self.uart.close()
            except Exception:
                pass
        self.uart = None
        self._reader = None
        self._last_target = None

    def is_connected(self) -> bool:
        return self._reader is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        if self._reader:
            return self._reader.firmware_version
        return None

    # ── Resynchronisation ───────────────────────────────────────────────

    def recover(self) -> None:
        """Abort any command in flight and drain the link.

        Sending a host ACK frame tells the PN532 to stop what it is doing.
        Draining afterwards discards the response to whatever it had already
        half-produced, so the next command starts from a known state.  This
        is the cheap alternative to closing the port, which is what the old
        error path did for every failure however trivial.
        """
        uart = self.uart
        if uart is None:
            return
        try:
            uart.write(_ACK_FRAME)
        except Exception:
            return
        try:
            uart.flush()
        except Exception:
            pass
        # Give the chip a moment to stop talking before discarding its output.
        time.sleep(0.02)
        for method in ("reset_input_buffer", "reset_output_buffer"):
            try:
                getattr(uart, method)()
            except Exception:
                pass

    @property
    def _has_low_level_api(self) -> bool:
        """True when the underlying driver exposes ``call_function``.

        Everything this class adds — bounded retries, InAutoPoll, ATQA/SAK
        capture — is built on the PN532 command primitive.  A substituted or
        stubbed driver that only implements the high-level helpers still
        works, just without the extras, so capability is checked rather than
        assumed.
        """
        return hasattr(self._reader, "call_function")

    def _call(self, command: int, response_length: int = 0, params=b"", timeout: float = 0.5):
        """Issue a PN532 command, translating failures into the error taxonomy."""
        if not self._reader:
            raise ReaderTransportError("reader is closed")
        try:
            return self._reader.call_function(
                command, response_length=response_length, params=params, timeout=timeout
            )
        except Exception as e:
            raise classify_exception(e) from e

    # ── Detection ───────────────────────────────────────────────────────

    @property
    def supports_target_info(self) -> bool:
        return self._reader is not None and self._has_low_level_api

    def last_target(self) -> Optional[TargetInfo]:
        """The most recent successful detection, or ``None``."""
        return self._last_target

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Return the UID of the tag present, or ``None``.

        Kept for compatibility with every existing caller.  Prefer
        :meth:`read_target`, which additionally reports ATQA and SAK.
        """
        target = self.read_target(timeout=timeout)
        return target.uid if target else None

    def read_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Detect a tag of any supported family.

        Tries ``InAutoPoll`` first — bounded, self-terminating, and covering
        Type A, Type B, FeliCa and Jewel in one pass.  Falls back to
        ``InListPassiveTarget`` on firmware that does not implement it.
        """
        if not self._reader:
            return None

        if not self._has_low_level_api:
            return self._read_target_via_helper(timeout)

        if not self._autopoll_unsupported:
            try:
                target = self._autopoll(timeout)
                self._autopoll_failures = 0
                self._last_target = target
                return target
            except ReaderProtocolError:
                # Could be a desync, or a module whose firmware does not
                # implement the command. Resynchronise either way.
                self.recover()
                self._autopoll_failures += 1
                if self._autopoll_failures < _AUTOPOLL_FALLBACK_AFTER:
                    raise
                # Repeated failure means this is not a passing desync. Demote
                # to the older detection path rather than raising forever —
                # raising was a loop with no exit, because the caller's own
                # error budget drops the reader, reconnect resets this flag,
                # and the same command fails again on the next poll.
                self._autopoll_unsupported = True
                if self.logger:
                    self.logger.warning(
                        f"PN532: InAutoPoll failed {self._autopoll_failures} times "
                        f"in a row; falling back to InListPassiveTarget for this "
                        f"connection. Type B, FeliCa and Jewel tags will no longer "
                        f"be detected."
                    )

        target = self._list_passive_target(_BAUD_106A, timeout)
        self._last_target = target
        return target

    def _read_target_via_helper(self, timeout: float) -> Optional[TargetInfo]:
        """Detection for a driver that offers only ``read_passive_target``.

        No ATQA or SAK is available on this path, so classification falls
        back to probing.  Kept working rather than rejected, because it is
        what a stubbed or third-party driver provides.
        """
        try:
            uid = self._reader.read_passive_target(timeout=timeout)
        except Exception as e:
            raise classify_exception(e) from e
        if not uid:
            self._last_target = None
            return None
        target = TargetInfo(uid=bytes(uid))
        self._last_target = target
        return target

    def _autopoll(self, timeout: float) -> Optional[TargetInfo]:
        """One bounded InAutoPoll pass across every supported tag family."""
        params = bytearray(
            [
                _AUTOPOLL_POLL_NR,
                _AUTOPOLL_PERIOD,
                _POLL_TYPE_GENERIC_106A,
                _POLL_TYPE_ISO14443B,
                _POLL_TYPE_FELICA_212,
                _POLL_TYPE_FELICA_424,
                _POLL_TYPE_JEWEL,
            ]
        )
        response = self._call(
            _CMD_INAUTOPOLL,
            response_length=64,
            params=params,
            timeout=max(timeout, _AUTOPOLL_MIN_TIMEOUT),
        )
        if response is None:
            # The chip did not answer in time.  With MaxRetries bounded this
            # should not happen, so treat it as a desync rather than as "no
            # tag" — reporting absence wrongly is what causes phantom
            # removals and closed games.
            raise ReaderProtocolError("no response to InAutoPoll")
        if len(response) < 1:
            raise ReaderProtocolError("truncated InAutoPoll response")

        count = response[0]
        if count == 0:
            return None
        if len(response) < 3:
            raise ReaderProtocolError("InAutoPoll reported a target but sent no data")

        target_type = response[1]
        length = response[2]
        data = response[3:3 + length]
        return _parse_autopoll_target(target_type, data)

    def _list_passive_target(self, baud: int, timeout: float) -> Optional[TargetInfo]:
        """Legacy detection path: InListPassiveTarget for one baud rate.

        Unlike the stock driver this parses the *whole* target descriptor
        rather than discarding everything but the UID, so ATQA and SAK
        survive and classification never has to guess.
        """
        response = self._call(
            _CMD_INLISTPASSIVETARGET,
            response_length=64,
            params=[0x01, baud],
            timeout=timeout,
        )
        if response is None:
            raise ReaderProtocolError("no response to InListPassiveTarget")
        if len(response) < 1:
            raise ReaderProtocolError("truncated InListPassiveTarget response")
        if response[0] == 0:
            return None
        if baud == _BAUD_106B:
            return _parse_106b(response[1:])
        return _parse_106a(response[1:])

    def select_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Re-activate a Type A tag before exchanging data with it.

        Content reads always go through this rather than through
        :meth:`read_target`, because ``InDataExchange`` addresses the target
        that ``InListPassiveTarget`` selected.
        """
        if not self._reader:
            return None
        if not self._has_low_level_api:
            return self._read_target_via_helper(timeout)
        target = self._list_passive_target(_BAUD_106A, timeout)
        if target:
            self._last_target = target
        return target

    def release_target(self) -> None:
        """Release the selected target so the field can re-activate it.

        Leaving a tag selected between polls is what makes a card resting on
        the reader intermittently invisible: it stays in the ACTIVE state and
        does not answer the next anticollision.
        """
        if not self._reader:
            return
        try:
            self._call(_CMD_INRELEASE, response_length=1, params=[0x00], timeout=0.3)
        except ReaderError:
            pass

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read the PUPI of an ISO 14443-B tag, or ``None``.

        The previous implementation passed ``baud_rate=3`` to a driver method
        whose parameter is named ``card_baud``, so every call raised
        ``TypeError`` into a bare ``except`` and Type B never worked at all.
        Routine detection now covers Type B via InAutoPoll; this remains for
        callers that want to probe it explicitly.
        """
        if not self._reader or not self._has_low_level_api:
            return None
        try:
            target = self._list_passive_target(_BAUD_106B, timeout)
        except ReaderError:
            return None
        return target.uid if target else None

    # ── Tag commands ────────────────────────────────────────────────────

    def get_version(self) -> Optional[bytes]:
        """Send GET_VERSION to an NTAG / Ultralight EV1 and return 8 bytes.

        This is the only non-destructive way to tell an NTAG213 from a 215
        from a 216, and it replaces assuming every tag has NTAG215 geometry.
        The original MIFARE Ultralight predates the command and answers with
        a NAK, which is itself useful: ``None`` means "not an EV1-era part".

        Only ever called for tags whose SAK says they are in this family, so
        it is never sent to a MIFARE Classic.
        """
        if not self._reader or not self._has_low_level_api:
            return None
        try:
            response = self._call(
                _CMD_INDATAEXCHANGE,
                response_length=9,
                params=[0x01, _TAG_CMD_GET_VERSION],
                timeout=0.3,
            )
        except ReaderError:
            return None
        if not response or len(response) < 9 or response[0] != 0x00:
            return None
        return bytes(response[1:9])

    # allow callers to transparently access any other PN532_UART methods
    def __getattr__(self, name):
        # Guard against recursion: __getattr__ is consulted for _reader itself
        # if anything touches it before __init__ has set it.
        if name.startswith("_"):
            raise AttributeError(name)
        reader = self.__dict__.get("_reader")
        if reader is not None and hasattr(reader, name):
            return getattr(reader, name)
        raise AttributeError(f"{self.__class__.__name__!r} object has no attribute {name!r}")


# ── Target descriptor parsing ───────────────────────────────────────────


def _parse_106a(data) -> Optional[TargetInfo]:
    """Parse an ISO 14443-3A target descriptor.

    Layout: Tg, SENS_RES(2), SEL_RES(1), NFCIDLength(1), NFCID1(n),
    optionally ATSLength + ATS.
    """
    if len(data) < 5:
        raise ReaderProtocolError("truncated 106A target descriptor")
    tg = data[0]
    atqa = (data[1] << 8) | data[2]
    sak = data[3]
    uid_len = data[4]
    if uid_len == 0 or uid_len > 10 or len(data) < 5 + uid_len:
        raise ReaderProtocolError(f"implausible UID length {uid_len}")
    uid = bytes(data[5:5 + uid_len])

    ats = None
    rest = data[5 + uid_len:]
    if len(rest) >= 1:
        ats_len = rest[0]
        # ATS length includes the length byte itself.
        if 1 <= ats_len <= len(rest):
            ats = bytes(rest[1:ats_len])

    return TargetInfo(
        uid=uid, atqa=atqa, sak=sak, ats=ats,
        protocol=tag_identity.PROTO_106A, target_number=tg,
    )


def _parse_106b(data) -> Optional[TargetInfo]:
    """Parse an ISO 14443-3B target descriptor.

    Layout: Tg, ATQB(12), ATTRIB_RES_Length, ATTRIB_RES.  The 4-byte PUPI
    inside ATQB is the closest thing Type B has to a UID.
    """
    if len(data) < 13:
        raise ReaderProtocolError("truncated 106B target descriptor")
    tg = data[0]
    pupi = bytes(data[2:6])
    return TargetInfo(
        uid=pupi, protocol=tag_identity.PROTO_106B, target_number=tg,
    )


def _parse_felica(data, protocol: str) -> Optional[TargetInfo]:
    """Parse a FeliCa target descriptor.

    Layout: Tg, POL_RES length, response code, NFCID2t(8), Pad(8),
    optionally SystemCode(2).  NFCID2t (the IDm) is the identifier.
    """
    if len(data) < 11:
        raise ReaderProtocolError("truncated FeliCa target descriptor")
    tg = data[0]
    idm = bytes(data[3:11])
    return TargetInfo(uid=idm, protocol=protocol, target_number=tg)


def _parse_jewel(data) -> Optional[TargetInfo]:
    """Parse an Innovision Jewel / Topaz descriptor: Tg, SENS_RES(2), JewelID(4)."""
    if len(data) < 7:
        raise ReaderProtocolError("truncated Jewel target descriptor")
    tg = data[0]
    atqa = (data[1] << 8) | data[2]
    return TargetInfo(
        uid=bytes(data[3:7]), atqa=atqa,
        protocol=tag_identity.PROTO_JEWEL, target_number=tg,
    )


def _parse_autopoll_target(target_type: int, data) -> Optional[TargetInfo]:
    """Dispatch an InAutoPoll target descriptor on its type byte."""
    protocol = _POLL_TYPE_PROTOCOL.get(target_type)
    if protocol == tag_identity.PROTO_106A:
        return _parse_106a(data)
    if protocol == tag_identity.PROTO_106B:
        return _parse_106b(data)
    if protocol in (tag_identity.PROTO_212F, tag_identity.PROTO_424F):
        return _parse_felica(data, protocol)
    if protocol == tag_identity.PROTO_JEWEL:
        return _parse_jewel(data)
    raise ReaderProtocolError(f"unsupported InAutoPoll target type 0x{target_type:02X}")
