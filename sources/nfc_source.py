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
"""

import os
import sys
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

try:
    from nfc.reader import PN532UARTReader
    _READER_IMPORT_ERROR = None
except ImportError as _e:
    # Keep the reason. Discarding it makes a packaging failure (e.g. wheels
    # built for the wrong platform, breaking the adafruit_pn532 -> Blinka ->
    # cffi import chain) indistinguishable from "no reader plugged in", which
    # is exactly what made this class of bug so hard to diagnose.
    PN532UARTReader = None
    _READER_IMPORT_ERROR = _e


# USB-serial bridge chips used by PN532/PN5180 UART modules. Auto-detection is
# restricted to these so it can never land on an unrelated CDC-ACM device.
_KNOWN_USB_SERIAL_VIDS = frozenset({
    0x1A86,  # QinHeng CH340 / CH341
    0x10C4,  # Silicon Labs CP210x
    0x0403,  # FTDI
    0x067B,  # Prolific PL2303
})


class NfcSource(MediaSource):
    """NFC reader polling source.

    Wraps the existing PN532/ACR122U/Proxmark/nfcpy reader abstraction
    and produces MediaEvents for tag arrival and removal.
    """

    source_type = SourceType.NFC

    DEBOUNCE_THRESHOLD = 3   # consecutive None reads to confirm removal

    def __init__(
        self,
        settings: dict,
        key_manager=None,
        signature_manager=None,
        logger=None,
    ):
        self._settings = settings
        self._key_manager = key_manager
        self._signature_manager = signature_manager
        self._logger = logger
        self._reader = None
        # Legacy field retained for compatibility with existing reader module
        self._uart = None

        # Polling state
        self._last_uid_hex: Optional[str] = None
        self._missing_count: int = 0

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
        return True

    async def stop(self) -> None:
        """Release reader resources."""
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

    async def poll(self) -> Optional[PluginEvent]:
        """One poll cycle: read UID, detect arrival/removal, return event."""
        if not self._reader:
            return None

        try:
            uid = self._reader.read_uid(timeout=0.2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"NfcSource: poll error: {e}")
                self._logger.error(traceback.format_exc())
            # Mark reader as dead so SourceManager will attempt reconnect.
            # Reset last_uid so the same card is re-detected after reconnect.
            # Close before dropping the reference — otherwise the serial fd
            # stays open until GC and the next connect() races against it.
            self._last_uid_hex = None
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
            return None

        if uid:
            self._missing_count = 0
            uid_hex = uid.hex().upper()
            is_new_tag = (uid_hex != self._last_uid_hex)

            if is_new_tag:
                self._last_uid_hex = uid_hex
                self.current_tag_uid = uid_hex

                # Classify tag (needed to choose ntag vs mifare read path)
                try:
                    self.current_tag_meta = self._classify_tag(uid)
                except Exception:
                    self.current_tag_meta = None

                # Read NDEF records once and derive both URI and record list.
                # _read_ndef_uri() would call _read_ndef_records() internally,
                # then we'd call it again — two full tag reads for the same data.
                try:
                    records = self._read_ndef_records()
                except Exception:
                    records = []

                uri = None
                for record in records:
                    if hasattr(record, "uri") and record.__class__.__name__.endswith("UriRecord"):
                        uri = record.uri
                        break
                self.current_tag_uri = uri

                # Build payload with NFC-specific data
                payload: Dict[str, Any] = {}
                if self.current_tag_meta:
                    payload["tag_meta"] = self.current_tag_meta

                serializable_records = []
                for record in records:
                    rec_dict = {}
                    for attr in ['type', 'name', 'uri', 'text', 'language', 'encoding']:
                        if hasattr(record, attr):
                            rec_dict[attr] = getattr(record, attr)
                    serializable_records.append(rec_dict)
                payload["ndef_records"] = serializable_records

                if self._logger:
                    self._logger.info(f"NfcSource: new tag {uid_hex}, uri={uri}")

                return MediaEvent(
                    kind=MediaEventKind.LOAD,
                    source_type=SourceType.NFC,
                    source_id=self.source_id,
                    media_id=uid_hex,
                    uri=uri,
                    payload=payload,
                )
            else:
                # Same tag still present — update metadata cache
                try:
                    self.current_tag_meta = self._classify_tag(uid)
                except Exception:
                    pass
        else:
            # Tag absent — debounce removal
            if self._last_uid_hex:
                self._missing_count += 1
                if self._missing_count >= self.DEBOUNCE_THRESHOLD:
                    removed_uid = self._last_uid_hex
                    if self._logger:
                        self._logger.info(
                            f"NfcSource: tag removed: {removed_uid} "
                            f"(after {self._missing_count} misses)"
                        )
                    self._last_uid_hex = None
                    self._missing_count = 0

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

        return None

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
                from nfc.acr122u_backend import ACR122UReader
                return ACR122UReader(logger=self._logger)
            except ImportError:
                if self._logger:
                    self._logger.error("ACR122U backend requires pyscard library")
                return None
        elif rtype == "proxmark":
            try:
                from nfc.proxmark_backend import ProxmarkReader
                return ProxmarkReader(path, logger=self._logger)
            except ImportError:
                if self._logger:
                    self._logger.error("Proxmark backend not available")
                return None
        elif rtype == "nfcpy":
            try:
                from nfc.nfcpy_backend import NfcPyReader
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

    def _classify_tag(self, uid: bytes) -> Dict[str, Any]:
        """Return basic metadata about the presented tag.

        Distinguishes between Mifare Classic, NTAG21x, Ultralight, and
        other tag families.  Results are cached per UID.
        """
        uid_hex = uid.hex().upper()

        # Check cache first
        if uid_hex in self._tag_classification_cache:
            return self._tag_classification_cache[uid_hex]

        meta: Dict[str, Any] = {
            "uid": uid_hex,
            "type": "unknown",
            "capacity_bytes": 0,
            "protected": False,
        }

        authenticated = False

        # Heuristics for additional families
        if len(uid) == 4 and hasattr(self._reader, 'read_uid_iso14443b'):
            try:
                test_uid = self._reader.read_uid_iso14443b(timeout=0.1)
                if test_uid and test_uid == uid:
                    meta["type"] = "iso14443b"
                    self._cache_tag_classification(uid_hex, meta)
                    return meta
            except Exception:
                pass

        if len(uid) == 8 and uid[0] == 0xE0:
            meta["type"] = "iso15693"
            self._cache_tag_classification(uid_hex, meta)
            return meta

        if len(uid) == 8:
            meta["type"] = "felica"
            self._cache_tag_classification(uid_hex, meta)
            return meta

        # Try Mifare Classic authentication
        keys = [
            b"\xFF\xFF\xFF\xFF\xFF\xFF",
            b"\xD3\xF7\xD3\xF7\xD3\xF7",
            b"\xA0\xA1\xA2\xA3\xA4\xA5",
        ]
        for key in keys:
            try:
                if self._reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
            except Exception:
                break
            finally:
                time.sleep(0.05)

        if authenticated:
            meta["type"] = "mifare-classic"
            blocks = list(self._iter_mifare_data_blocks())
            meta["capacity_bytes"] = len(blocks) * 16  # MIFARE_CLASSIC_BLOCK_SIZE
        else:
            try:
                if self._reader.mifare_classic_read_block(4) is not None:
                    meta["type"] = "ntag21x"
                    if len(uid) == 7:
                        meta["type"] = "ultralight"
                    pages = list(self._iter_ntag_pages())
                    meta["capacity_bytes"] = len(pages) * 4
            except Exception:
                meta["protected"] = True

        if meta["type"] == "unknown" and len(uid) == 7:
            meta["type"] = "desfire"

        self._cache_tag_classification(uid_hex, meta)
        return meta

    def _cache_tag_classification(self, uid_hex: str, meta: Dict[str, Any]) -> None:
        """Cache tag classification with LRU eviction."""
        self._tag_classification_cache[uid_hex] = meta
        if len(self._tag_classification_cache) > self._tag_cache_max_size:
            oldest_key = next(iter(self._tag_classification_cache))
            del self._tag_classification_cache[oldest_key]

    # ── NDEF Read ──────────────────────────────────────────────────────

    def _iter_ntag_pages(self):
        """Yield user-writable pages for NTAG21x devices."""
        for page in range(4, 134):
            yield page

    def _iter_mifare_data_blocks(self):
        """Return list of writable data blocks (skip trailer blocks)."""
        blocks = []
        for block in range(4, 63):  # FIRST_DATA_BLOCK to MAX_BLOCK
            if block % 4 == 3:
                continue
            blocks.append(block)
        return blocks

    def _read_ndef_records(self) -> List[Any]:
        """Read and return all NDEF records present on the current tag."""
        import ndef

        uid = self._reader.read_uid(timeout=0.1)
        if not uid:
            return []

        tag_meta = self._classify_tag(uid)
        is_ntag = tag_meta.get("type") in ("ntag21x", "ultralight")

        data = bytearray()
        if is_ntag:
            blocks_iter = self._iter_ntag_pages()
            read_fn = self._reader.ntag2xx_read_block
        else:
            blocks_iter = self._iter_mifare_data_blocks()
            read_fn = self._reader.mifare_classic_read_block

        for i in blocks_iter:
            block = read_fn(i)
            if block:
                data.extend(block)
                if 0xFE in block:
                    break
            else:
                break

        if not data:
            return []

        records = []
        if len(data) > 2 and data[0] == 0x03:
            length = data[1]
            ndef_data = data[2:2 + length]
            try:
                for rec in ndef.message_decoder(ndef_data):
                    records.append(rec)
            except Exception:
                # Try fallback URI extraction
                if len(ndef_data) > 3:
                    for i in range(len(ndef_data) - 2):
                        if ndef_data[i] == 0x55:
                            uri_data = ndef_data[i + 2:]
                            if uri_data:
                                try:
                                    uri_str = uri_data.decode("utf-8", errors="ignore").strip("\x00\xfe")
                                    if uri_str:
                                        records.append(ndef.UriRecord(uri_str))
                                        break
                                except Exception:
                                    pass

        if not records:
            try:
                import re
                decoded = data.decode("utf-8", errors="ignore").strip("\x00")
                match = re.search(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\x00\xfe]{1,2048})", decoded)
                if match:
                    uri = match.group(1).strip()
                    try:
                        records.append(ndef.UriRecord(uri))
                    except Exception:
                        pass
            except Exception:
                pass

        return records

    def _read_ndef_uri(self) -> Optional[str]:
        """Return the first URI record's value, or None."""
        for record in self._read_ndef_records():
            if hasattr(record, "uri") and record.__class__.__name__.endswith("UriRecord"):
                return record.uri
        return None

    # ── NDEF Write (for pairing) ───────────────────────────────────────

    def can_write(self) -> bool:
        return True

    def write_uri(self, media_id: str, uri: str) -> Tuple[bool, Optional[str]]:
        """Source-generic pairing entry point.

        ``media_id`` is the tag UID as hex, the form carried by MediaEvents.
        """
        try:
            uid = bytes.fromhex(media_id)
        except (ValueError, TypeError):
            return False, f"invalid tag UID {media_id!r}"
        return self.write_ndef_uri(uid, uri)

    def write_ndef_uri(self, uid: bytes, uri: str) -> Tuple[bool, Optional[str]]:
        """Write a URI as an NDEF URI record to the tag.

        Returns ``(True, None)`` on success, ``(False, error_message)`` on failure.
        """
        import ndef

        uri_bytes = uri.encode("utf-8")

        try:
            record = ndef.UriRecord(uri)
            message = b"".join(ndef.message_encoder([record]))
            tlv = bytearray([0x03, len(message)]) + message + b"\xFE"
        except Exception as e:
            return False, f"Failed to create NDEF record: {e}"

        # Determine tag type by attempting Classic auth
        authenticated = False
        keys = [
            b'\xFF\xFF\xFF\xFF\xFF\xFF',
            b'\xD3\xF7\xD3\xF7\xD3\xF7',
            b'\xA0\xA1\xA2\xA3\xA4\xA5',
        ]
        for key in keys:
            try:
                if self._reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
            except Exception:
                authenticated = False
                break
            finally:
                time.sleep(0.05)

        # Compute capacity
        if authenticated:
            blocks = list(self._iter_mifare_data_blocks())
            max_payload = len(blocks) * 16
        else:
            pages = list(self._iter_ntag_pages())
            max_payload = len(pages) * 4

        estimated_size = 2 + 4 + 1 + len(uri_bytes) + 1
        if estimated_size > max_payload:
            msg = (
                f"URI too long: estimated {estimated_size} bytes "
                f"exceeds limit of {max_payload} bytes."
            )
            return False, msg

        try:
            if authenticated:
                # Mifare Classic write path
                while len(tlv) % 16 != 0:
                    tlv.append(0x00)

                writable_blocks = self._iter_mifare_data_blocks()
                required_blocks = len(tlv) // 16
                if required_blocks > len(writable_blocks):
                    return False, (
                        f"URI too long for writable Mifare blocks: needs {required_blocks}, "
                        f"available {len(writable_blocks)}."
                    )

                for i in range(0, len(tlv), 16):
                    block_num = writable_blocks[i // 16]
                    block_data = tlv[i : i + 16]
                    if not self._reader.mifare_classic_write_block(block_num, block_data):
                        return False, f"Write failed at block {block_num}"

                return True, None
            else:
                # NTAG write path
                while len(tlv) % 4 != 0:
                    tlv.append(0x00)

                pages = list(self._iter_ntag_pages())
                required_pages = len(tlv) // 4
                if required_pages > len(pages):
                    return False, (
                        f"URI too long for NTAG pages: needs {required_pages}, "
                        f"available {len(pages)}."
                    )

                for i in range(0, len(tlv), 4):
                    page_num = pages[i // 4]
                    page_data = tlv[i : i + 4]
                    if not self._reader.ntag2xx_write_block(page_num, page_data):
                        return False, f"Write failed at page {page_num}"

                return True, None
        except Exception as e:
            return False, str(e)
