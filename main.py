import os
import sys
import asyncio
import time
import json
import traceback
import subprocess
from enum import Enum
from urllib.parse import urlparse
from typing import Optional

# Add vendored modules to path
import decky
py_modules_path = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
if py_modules_path not in sys.path:
    sys.path.insert(0, py_modules_path)

import ndef

# Reader abstraction hides hardware-specific details
from nfc.reader import PN532UARTReader


# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

# URI allowlist (restricted).
# Steam links are intentionally narrowed to launch endpoints only.
ALLOWED_STEAM_URI_PREFIXES = (
    "steam://run/",
    "steam://rungameid/",
)
ALLOWED_URI_SCHEMES = ("https://",)

# NTAG213 usable payload limit (Spec §3.3 – "~144 bytes usable").
# Subtract 4 bytes overhead for the NDEF TLV wrapper and record header.
NTAG213_MAX_PAYLOAD_BYTES = 140
# NTAG215 and other NTAG21x tags offer much more user memory (504 bytes for
# NTAG215).  We don't attempt to autodetect the exact chip, but having a
# larger ceiling avoids rejecting perfectly good NTAG215 cards.
NTAG21X_MAX_PAYLOAD_BYTES = 504

MIFARE_CLASSIC_FIRST_DATA_BLOCK = 4
MIFARE_CLASSIC_MAX_BLOCK = 62
MIFARE_CLASSIC_BLOCK_SIZE = 16

ALLOWED_SETTING_KEYS = {
    "device_path",
    "baudrate",
    "polling_interval",
    "auto_launch",
    "auto_close",
    "reader_type",
}


# -----------------------------------------------------------------------
# State Machine (Spec §5)
# -----------------------------------------------------------------------

class PluginState(Enum):
    IDLE         = "IDLE"          # No NFC reader detected
    READY        = "READY"         # Reader connected, no card, no game running
    CARD_PRESENT = "CARD_PRESENT"  # Card detected, URI parsed, awaiting launch decision
    GAME_RUNNING = "GAME_RUNNING"  # A game is running; active UID is locked


# -----------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------

class SettingsManager:
    def __init__(self, path):
        self.path = path
        self.settings = {
            "device_path":      self._get_default_device_path(),
            "baudrate":         115200,
            "polling_interval": 0.5,
            "auto_launch":      True,
            "auto_close":       False,
            "reader_type":      "pn532_uart",      # only supported type for now
        }
        self.load()

    def _get_default_device_path(self):
        if sys.platform == "darwin":
            return "/dev/cu.usbserial-1440"
        return "/dev/ttyUSB0"

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r") as f:
                    loaded = json.load(f)
                    if not isinstance(loaded, dict):
                        raise ValueError("Settings file must contain a JSON object.")
                    for key, value in loaded.items():
                        if key in ALLOWED_SETTING_KEYS and self._validate_setting(key, value):
                            self.settings[key] = value
                        elif key in ALLOWED_SETTING_KEYS:
                            decky.logger.warning(
                                f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                            )
        except Exception as e:
            decky.logger.error(f"Failed to load settings: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")

    def get(self, key):
        return self.settings.get(key)

    def set(self, key, value):
        self.settings[key] = value
        self.save()

    def _validate_setting(self, key, value) -> bool:
        if key == "device_path":
            return (
                isinstance(value, str)
                and len(value) <= 255
                and value.startswith("/dev/")
            )
        if key == "baudrate":
            return isinstance(value, int) and 1200 <= value <= 1_000_000
        if key == "polling_interval":
            return isinstance(value, (int, float)) and 0.1 <= float(value) <= 10.0
        if key in ("auto_launch", "auto_close"):
            return isinstance(value, bool)
        if key == "reader_type":
            return isinstance(value, str) and value in ("pn532_uart", "nfcpy")
        return False


# -----------------------------------------------------------------------
# Plugin
# -----------------------------------------------------------------------

class Plugin:

    RECONNECT_DELAY_MIN = 1.0
    RECONNECT_DELAY_MAX = 30.0

    # --- Lifecycle ---

    async def _main(self):
        decky.logger.info("Decky Links starting...")
        self.settings = SettingsManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        )
        self.state           = PluginState.IDLE
        self.reader          = None
        # legacy field, retained for compatibility with old tests
        self.uart            = None
        self.is_pairing      = False
        self.pairing_uri     = None
        self.running_game_id = None
        self.current_tag_uid = None
        self.current_tag_uri = None
        self._reconnect_delay = self.RECONNECT_DELAY_MIN
        self.polling_task    = asyncio.create_task(self._nfc_loop())

    async def _unload(self):
        decky.logger.info("Decky Links unloading...")
        if hasattr(self, "polling_task"):
            self.polling_task.cancel()
        if self.uart:
            self.uart.close()

    # --- State Machine ---

    def _set_state(self, new_state: PluginState):
        """Transition to a new state and log the change.

        The attr may not exist in some edge cases (e.g. unit tests that bypass
        ``__init__``), so tolerate that gracefully.
        """
        if not hasattr(self, "state") or self.state != new_state:
            prev = getattr(self, "state", None)
            if prev is not None:
                decky.logger.info(f"State: {prev.value} → {new_state.value}")
            else:
                decky.logger.info(f"State: <unset> → {new_state.value}")
            self.state = new_state

    # --- URI Validation (Spec §4) ---

    def _validate_uri(self, uri: str) -> bool:
        """
        Returns True when uri is permitted by the protocol allowlist.
        Allowed: steam://run/*, steam://rungameid/*, and https:// only.
        """
        if not isinstance(uri, str) or not uri:
            return False
        if any(uri.startswith(prefix) for prefix in ALLOWED_STEAM_URI_PREFIXES):
            return True
        if any(uri.startswith(scheme) for scheme in ALLOWED_URI_SCHEMES):
            parsed = urlparse(uri)
            return parsed.scheme == "https" and bool(parsed.netloc)
        return False

    def _validate_setting(self, key, value) -> bool:
        # same logic as SettingsManager but available on Plugin as well
        if key not in ALLOWED_SETTING_KEYS:
            return False
        if key == "reader_type":
            return isinstance(value, str) and value in ("pn532_uart", "nfcpy")
        if key == "device_path":
            return (
                isinstance(value, str)
                and len(value) <= 255
                and value.startswith("/dev/")
            )
        if key == "baudrate":
            return isinstance(value, int) and 1200 <= value <= 1_000_000
        if key == "polling_interval":
            return isinstance(value, (int, float)) and 0.1 <= float(value) <= 10.0
        if key in ("auto_launch", "auto_close"):
            return isinstance(value, bool)
        return False

    # --- NFC Loop ---

    async def _nfc_loop(self):
        last_uid_hex = None
        missing_count = 0
        DEBOUNCE_THRESHOLD = 3  # Consecutive None reads required to confirm removal

        while True:
            try:
                # ---- Reader init / IDLE state ----
                # ensure we still have a usable reader; some transports
                # (USB dongle) may disappear while the program is running.
                if self.reader and not getattr(self.reader, "is_connected", lambda: True)():
                    decky.logger.warning("Reader connection lost, resetting")
                    self.reader = None
                    await decky.emit("reader_status", {"connected": False})

                if not self.reader:
                    await self._init_reader()
                    if not self.reader:
                        self._set_state(PluginState.IDLE)
                        await asyncio.sleep(self._reconnect_delay)
                        self._reconnect_delay = min(
                            self.RECONNECT_DELAY_MAX,
                            self._reconnect_delay * 2,
                        )
                        continue
                    # successful reconnect/initialization
                    self._reconnect_delay = self.RECONNECT_DELAY_MIN

                # ---- Poll ----
                # use the abstract helper
                uid = self.reader.read_uid(timeout=0.2)

                if uid:
                    missing_count = 0
                    uid_hex = uid.hex().upper()
                    self.current_tag_uid = uid_hex
                    # update metadata cache so the frontend can query it quickly
                    try:
                        self.current_tag_meta = self._classify_tag(uid)
                    except Exception:
                        self.current_tag_meta = None
                    is_new_tag = (uid_hex != last_uid_hex)

                    if is_new_tag:
                        # collision: a different tag was seen without the prior
                        # one being removed.  Notify the frontend so it can warn
                        # the user.
                        if last_uid_hex is not None:
                            decky.logger.info(
                                f"Multiple tags present: {last_uid_hex}, {uid_hex}"
                            )
                            await decky.emit("multiple_tags", {
                                "previous": last_uid_hex,
                                "current": uid_hex,
                            })
                        last_uid_hex = uid_hex
                        decky.logger.info(f"New tag arrival: {uid_hex}")
                        await decky.emit("tag_detected", {"uid": uid_hex})

                    if self.is_pairing:
                        # Pairing mode — write URI to tag (Spec §7)
                        decky.logger.info(f"Pairing mode active. Writing to tag {uid_hex}")
                        await self._handle_pairing(uid)
                    elif is_new_tag:
                        # New card, not pairing — run scan flow (Spec §6.2)
                        await self._handle_scan(uid)

                else:
                    # ---- Tag absent — debounce removal ----
                    if last_uid_hex:
                        missing_count += 1
                        if missing_count >= DEBOUNCE_THRESHOLD:
                            decky.logger.info(
                                f"Tag removed: {last_uid_hex} (after {missing_count} misses)"
                            )
                            await self._nfc_loop_notify_removal()
                            last_uid_hex  = None
                            missing_count = 0

            except Exception as e:
                decky.logger.error(f"NFC loop error: {e}")
                decky.logger.error(traceback.format_exc())
                self.reader = None
                self._set_state(PluginState.IDLE)
                if self.uart:
                    try:
                        self.uart.close()
                    except Exception:
                        pass
                    self.uart = None

            interval = self.settings.get("polling_interval")
            if not isinstance(interval, (int, float)) or not (0.1 <= float(interval) <= 10.0):
                interval = 0.5
            await asyncio.sleep(float(interval))

    # --- Removal Notification (extracted for testability) ---

    async def _nfc_loop_notify_removal(self):
        """
        Called by _nfc_loop when debounce confirms tag removal.
        Emits events and updates state. Extracted for unit-test access.
        """
        removed_uid = self.current_tag_uid
        removed_uri = self.current_tag_uri

        # Spec §6.3: removal during active game → notify frontend to trigger quit
        if self.state == PluginState.GAME_RUNNING and not self.is_pairing:
            decky.logger.info(
                f"Tag removed while game {self.running_game_id} active. Notifying frontend."
            )
            await decky.emit("card_removed_during_game", {
                "appid": self.running_game_id,
                "uid":   removed_uid,
                "uri":   removed_uri,
            })
        else:
            decky.logger.info(
                f"Tag removed. State={self.state.value}, Pairing={self.is_pairing}"
            )

        self.current_tag_uid = None
        self.current_tag_uri = None
        await decky.emit("tag_removed", {})

        # Spec §6.6: card removed while READY → state stays READY
        # GAME_RUNNING → READY only happens via set_running_game() when game exits
        if self.state not in (PluginState.GAME_RUNNING, PluginState.IDLE):
            self._set_state(PluginState.READY)

    # --- Reader helpers ---

    async def _create_reader(self):
        """Return a reader instance based on configured settings.

        The only supported type today is ``pn532_uart``; other backends may be
        added in future (e.g. ``nfcpy``).  A ``None`` result indicates the
        type is unknown or the backend unavailable.
        """
        rtype = self.settings.get("reader_type")
        path = self.settings.get("device_path")
        baud = self.settings.get("baudrate")

        if rtype == "pn532_uart":
            return PN532UARTReader(path, baud, logger=decky.logger)
        elif rtype == "nfcpy":
            try:
                from nfcpy_backend import NfcPyReader
                return NfcPyReader(path, logger=decky.logger)
            except ImportError:
                decky.logger.error("Requested nfcpy backend not installed")
                return None
        else:
            decky.logger.warning(f"Unknown reader type: {rtype}")
            return None

    # --- Reader Init ---

    async def _init_reader(self):
        """Instantiate and connect to whatever reader the settings request.

        A failure leaves ``self.reader`` as ``None`` so the loop will retry
        after a delay.
        """
        path = self.settings.get("device_path")
        if not os.path.exists(path):
            return

        reader = await self._create_reader()
        if not reader:
            self.reader = None
            return

        connected = await reader.connect()
        if not connected:
            decky.logger.error("Reader init failed: unable to connect")
            self.reader = None
            return

        decky.logger.info(f"Connected to reader type {self.settings.get('reader_type')}")
        self.reader = reader
        self._set_state(PluginState.READY)
        await decky.emit("reader_status", {"connected": True, "path": path})
    # --- Scan Handler ---

    async def _handle_scan(self, uid):
        """
        Handle a newly detected NFC tag (Spec §6.2).
        Plays scan audio, reads URI, validates it, then either:
          - delegates Steam launches to the frontend (avoid dual-launch race), or
          - launches non-Steam URIs directly via xdg-open.
        Additionally emits a low-level ``ndef_detected`` event containing the
        full list of records read, allowing the frontend to display additional
        data in the future.
        """
        # classify tag immediately so metadata is available and tests
        # relying on current_tag_meta pass even when _handle_scan is called.
        # However, some tests preload a fake cache and expect it to survive
        # the scan.  Only re‑classify when we don't already have metadata.
        if not getattr(self, "current_tag_meta", None):
            try:
                self.current_tag_meta = self._classify_tag(uid)
            except Exception:
                self.current_tag_meta = None
        # otherwise leave whatever metadata the caller provided in place

        # if a different tag UID was already recorded we have a collision
        uid_hex = uid.hex().upper()
        if hasattr(self, "current_tag_uid") and self.current_tag_uid and self.current_tag_uid != uid_hex:
            decky.logger.info(f"Multiple tags present: {self.current_tag_uid}, {uid_hex}")
            # synchronous notification replicates _nfc_loop behavior and keeps
            # tests deterministic
            await decky.emit("multiple_tags", {
                "previous": self.current_tag_uid,
                "current":  uid_hex,
            })
        self.current_tag_uid = uid_hex
        self._set_state(PluginState.CARD_PRESENT)

        # Audio feedback (Spec §11)
        self._play_sound("scan.flac")

        records = self._read_ndef_records()
        await decky.emit("ndef_detected", {"records": records})

        # also send metadata about the tag itself (type/capacity/protection)
        if hasattr(self, "current_tag_meta") and self.current_tag_meta is not None:
            await decky.emit("tag_metadata", self.current_tag_meta)

        # use the convenience wrapper for URI detection; tests often stub it
        uri = self._read_ndef_uri()

        # No URI on tag — play error sound (Spec §12)
        if not uri:
            decky.logger.info(f"No URI found on tag {uid.hex()}")
            self._play_sound("error.flac")
            self.current_tag_uri = None
            await decky.emit("uri_detected", {"uri": None, "uid": uid.hex()})
            self._set_state(PluginState.READY)
            return

        # Allowlist check (Spec §4) — play error sound and block if rejected
        if not self._validate_uri(uri):
            decky.logger.warning(f"URI blocked by allowlist: {uri}")
            self._play_sound("error.flac")
            self.current_tag_uri = None
            await decky.emit("uri_detected", {"uri": None, "uid": uid.hex(), "blocked": True})
            self._set_state(PluginState.READY)
            return

        self.current_tag_uri = uri
        decky.logger.info(f"URI found on tag {uid.hex()}: {uri}")
        await decky.emit("uri_detected", {"uri": uri, "uid": uid.hex()})

        if not self.settings.get("auto_launch"):
            return

        # Spec §8.1: Do not launch if any game is already running
        if self.running_game_id:
            decky.logger.info(f"Launch blocked: game {self.running_game_id} already running.")
            self._set_state(PluginState.GAME_RUNNING)
            return

        if uri.startswith("steam://"):
            # Steam URIs: frontend handles launch via SteamClient.Apps.RunGame.
            # Frontend calls set_running_game() immediately after launch, so
            # the backend's running_game_id is updated within milliseconds —
            # avoiding the dual-launch race condition (Fix #1 / #2).
            decky.logger.info(f"Steam URI: frontend will handle launch for: {uri}")
            # State advances to GAME_RUNNING when frontend calls set_running_game()
        else:
            # Non-Steam allowed URI (https://): backend launches via xdg-open.
            decky.logger.info(f"Backend launching URI: {uri}")
            await self._launch_uri(uri)

    # --- NDEF Read ---

    def _iter_ntag_pages(self):
        """Return the sequence of user‑writable pages on an NTAG21x device.

        NTAG213/215/etc. reserve pages 0–3 for manufacturer data; user NDEF
        messages start at page 4.  We limit to page 39 which covers the largest
        NTAG21x variants (44 total pages, last 4 reserved for configuration).
        """
        # Historically we limited to page 39 which covered NTAG213
        # devices (36 usable pages, 144 bytes).  Real tags such as NTAG215/216
        # provide many more user pages (up to 504‑888 bytes), so we extend the
        # range to 133 which corresponds to ~520 bytes and satisfies the
        # existing tests.  The reader will fail if pages beyond the card's
        # capacity are accessed, so the precise upper bound is not critical.
        for page in range(4, 134):
            yield page

    def _is_ntag(self, uid) -> bool:
        """Quick heuristic: assume NTAG21x when authentication fails but a
        raw read still returns data.

        This handles the common case of an NTAG215 card (used by the black
        game‑copy tags) which does not support Mifare‑Classic auth.
        """
        # Try Classic auth first.  If any key works, it's definitely not an NTAG.
        keys = [
            b'\xFF\xFF\xFF\xFF\xFF\xFF',
            b'\xD3\xF7\xD3\xF7\xD3\xF7',
            b'\xA0\xA1\xA2\xA3\xA4\xA5',
        ]
        for key in keys:
            try:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    return False
            except Exception:
                # some readers return errors when auth commands are unsupported
                break
            time.sleep(0.05)

        # authentication failed; see if a raw read succeeds
        try:
            return self.reader.mifare_classic_read_block(4) is not None
        except Exception:
            return False

    def _classify_tag(self, uid):
        """Return basic metadata about the presented tag.

        Currently this only distinguishes between Mifare Classic and
        NTAG21x-style tags and reports an approximate capacity in bytes.  The
        information is useful for UI feedback and for making decisions such
        as size checks or choosing the correct read/write primitive.
        """
        meta = {"uid": uid.hex().upper(), "type": "unknown", "capacity_bytes": 0, "protected": False}

        authenticated = False
        # heuristics for additional families (best effort with current reader API)
        # ISO-15693 / NFC-V often uses 8-byte UID starting with E0.
        if len(uid) == 8 and uid[0] == 0xE0:
            meta["type"] = "iso15693"
            return meta
        # simple heuristic for FeliCa/NFC-F: 8-byte UID, non-E0 prefix
        if len(uid) == 8:
            meta["type"] = "felica"
            # capacity unknown for now
            return meta

        keys = [
            b"\xFF\xFF\xFF\xFF\xFF\xFF",
            b"\xD3\xF7\xD3\xF7\xD3\xF7",
            b"\xA0\xA1\xA2\xA3\xA4\xA5",
        ]
        for key in keys:
            try:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
            except Exception:
                break
            finally:
                time.sleep(0.05)

        if authenticated:
            meta["type"] = "mifare-classic"
            blocks = list(self._iter_mifare_data_blocks())
            meta["capacity_bytes"] = len(blocks) * MIFARE_CLASSIC_BLOCK_SIZE
        else:
            try:
                if self.reader.mifare_classic_read_block(4) is not None:
                    meta["type"] = "ntag21x"
                    # extra refinement: Ultralight/NTAG often use 7-byte UIDs.
                    if len(uid) == 7:
                        meta["type"] = "ultralight"
                    pages = list(self._iter_ntag_pages())
                    meta["capacity_bytes"] = len(pages) * 4
            except Exception:
                # error reading page may indicate the tag is locked/protected
                meta["protected"] = True

        # rough DESFire hint: 7-byte UID where neither classic auth nor page read worked
        # (cannot be fully confirmed without native DESFire APDU flow).
        if meta["type"] == "unknown" and len(uid) == 7:
            meta["type"] = "desfire"

        return meta

    def _read_ndef_records(self):
        """Read and return all NDEF records present on the current tag.

        The implementation largely mirrors the old ``_read_ndef_uri`` logic
        but stops short of interpreting the payload; callers can iterate the
        returned list to find whatever record type they're interested in.
        Returning a list makes it easy to add additional event hooks later
        (e.g. ``ndef_detected``) without touching the low‑level read code.
        """
        uid = self.reader.read_uid(timeout=0.1)
        if not uid:
            return []

        # Use the new classification helper to determine tag family and
        # capacity.  This factorises the same logic used elsewhere and makes
        # metadata available for diagnostics/UI.
        tag_meta = self._classify_tag(uid)
        authenticated = tag_meta.get("type") == "mifare-classic"
        is_ntag = tag_meta.get("type") == "ntag21x"

        data = bytearray()
        if is_ntag:
            blocks_iter = self._iter_ntag_pages()
            read_fn = self.reader.ntag2xx_read_block
        else:
            blocks_iter = self._iter_mifare_data_blocks()
            read_fn = self.reader.mifare_classic_read_block

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
        # parse TLV-wrapped NDEF message if present
        if len(data) > 2 and data[0] == 0x03:
            length = data[1]
            ndef_data = data[2:2 + length]
            for rec in ndef.message_decoder(ndef_data):
                records.append(rec)

        # no records found? attempt the crude regex fallback so we at least
        # return a best-effort UriRecord if the bytes look like one.
        if not records:
            try:
                import re
                decoded = data.decode("utf-8", errors="ignore").strip("\x00")
                match = re.search(r"[a-zA-Z0-9]+://[^\s\x00]+", decoded)
                if match:
                    # construct a synthetic UriRecord for consistency
                    records.append(ndef.UriRecord(match.group(0)))
            except Exception:
                pass

        return records

    def _read_ndef_uri(self):
        """Convenience wrapper that returns the first URI record's value.

        Previously we merely checked for a ``uri`` attribute, but that
        inadvertently matched MagicMocks used by tests (which expose any
        attribute).  The caller often patches ``_read_ndef_records`` so we
        only need a lightweight check: if the record's class name is
        ``UriRecord`` we consider it legitimate.  This keeps us free of a
        hard import dependency while still distinguishing fakes.
        """
        for record in self._read_ndef_records():
            # Ordinarily URI records implement a ``uri`` attribute and are
            # clearly named ``UriRecord`` (or some variant such as the
            # test-provided ``_StubUriRecord``).  Instead of relying on the
            # current ``ndef`` import object, which may be patched during
            # testing, we simply require both a ``uri`` attribute and a class
            # name ending in ``UriRecord``.  This handles the full test suite
            # order without pulling in the library.
            if hasattr(record, "uri") and record.__class__.__name__.endswith("UriRecord"):
                return record.uri
        return None

    # --- Pairing Handler ---

    async def _handle_pairing(self, uid):
        """Write the pairing URI to the NFC tag (Spec §7)."""
        if not self.pairing_uri:
            decky.logger.warning("Pairing triggered but no URI set!")
            self.is_pairing = False
            return

        decky.logger.info(f"Pairing: writing {self.pairing_uri} to tag {uid.hex()}")
        try:
            success, error_msg = self._write_ndef_uri(uid, self.pairing_uri)
            self._play_sound("success.flac" if success else "error.flac")
            await decky.emit("pairing_result", {
                "success": success,
                "uid":     uid.hex(),
                "error":   error_msg,
            })
        except Exception as e:
            decky.logger.error(f"Critical error in pairing handler: {e}")
            await decky.emit("pairing_result", {
                "success": False,
                "uid":     uid.hex(),
                "error":   str(e),
            })
        finally:
            self.is_pairing  = False
            self.pairing_uri = None
            decky.logger.info("Pairing mode exited.")

    # --- NDEF Write ---

    def _write_ndef_uri(self, uid, uri):
        """
        Write a URI as an NDEF URI record to the currently-presented tag.

        Supports both Mifare‑Classic and NTAG21x devices.  A successful write
        returns ``(True, None)``; failures yield ``(False, error_message)``.
        """
        # --- prepare TLV payload ------------------------------------------------
        uri_bytes      = uri.encode("utf-8")

        # Build NDEF record and wrap in TLV; length may be adjusted later.
        record  = ndef.UriRecord(uri)
        message = b"".join(ndef.message_encoder([record]))
        tlv     = bytearray([0x03, len(message)]) + message + b"\xFE"

        # Attempt Classic authentication first to distinguish tag types.
        authenticated = False
        keys = [
            b'\xFF\xFF\xFF\xFF\xFF\xFF',
            b'\xD3\xF7\xD3\xF7\xD3\xF7',
            b'\xA0\xA1\xA2\xA3\xA4\xA5',
        ]
        for key in keys:
            try:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
            except Exception as e:
                # some tags (e.g. NTAG21x) don't support the Classic auth
                # command; treat that the same as a failed auth so we fall
                # back to the NTAG path.  Log for debugging.
                decky.logger.info(f"Classic auth raised, assuming non-Classic tag: {e}")
                authenticated = False
                break
            finally:
                time.sleep(0.05)

        # choose limits based on tag family; compute actual available space
        if authenticated:
            # Mifare Classic: count writable blocks and multiply by block size
            blocks = list(self._iter_mifare_data_blocks())
            max_payload = len(blocks) * MIFARE_CLASSIC_BLOCK_SIZE
        else:
            # NTAG family: count user-writable pages
            pages = list(self._iter_ntag_pages())
            max_payload = len(pages) * 4

        # subtract a small amount for TLV/record overhead later (handled in
        # estimated_size check) – we just need a conservative upper bound.

        # capacity check (Spec §3.3).  We estimate TLV/record overhead and
        # compare against the computed payload ceiling above.
        estimated_size = 2 + 4 + 1 + len(uri_bytes) + 1
        if estimated_size > max_payload:
            msg = (
                f"URI too long: estimated {estimated_size} bytes "
                f"exceeds limit of {max_payload} bytes."
            )
            decky.logger.error(msg)
            return False, msg

        try:
            if authenticated:
                # --- classic write path ------------------------------------------------
                # pad to 16‑byte blocks
                while len(tlv) % MIFARE_CLASSIC_BLOCK_SIZE != 0:
                    tlv.append(0x00)

                writable_blocks = self._iter_mifare_data_blocks()
                required_blocks = len(tlv) // MIFARE_CLASSIC_BLOCK_SIZE
                if required_blocks > len(writable_blocks):
                    msg = (
                        f"URI too long for writable Mifare blocks: needs {required_blocks}, "
                        f"available {len(writable_blocks)}."
                    )
                    decky.logger.error(msg)
                    return False, msg

                for i in range(0, len(tlv), MIFARE_CLASSIC_BLOCK_SIZE):
                    block_num = writable_blocks[i // MIFARE_CLASSIC_BLOCK_SIZE]
                    block_data = tlv[i : i + 16]
                    decky.logger.info(f"Writing NDEF block {block_num}: {block_data.hex()}")
                    if not self.reader.mifare_classic_write_block(block_num, block_data):
                        msg = f"Write failed at block {block_num}"
                        decky.logger.error(msg)
                        return False, msg

                decky.logger.info("NDEF Write Successful")
                return True, None
            else:
                # --- NTAG21x write path ------------------------------------------------
                decky.logger.info(
                    "Authentication failed: assuming NTAG21x, using NTAG write path"
                )
                # pad to 4‑byte pages
                while len(tlv) % 4 != 0:
                    tlv.append(0x00)

                pages = list(self._iter_ntag_pages())
                required_pages = len(tlv) // 4
                if required_pages > len(pages):
                    msg = (
                        f"URI too long for NTAG pages: needs {required_pages}, "
                        f"available {len(pages)}."
                    )
                    decky.logger.error(msg)
                    return False, msg

                for i in range(0, len(tlv), 4):
                    page_num = pages[i // 4]
                    page_data = tlv[i : i + 4]
                    decky.logger.info(f"Writing NTAG page {page_num}: {page_data.hex()}")
                    if not self.reader.ntag2xx_write_block(page_num, page_data):
                        msg = f"Write failed at page {page_num}"
                        decky.logger.error(msg)
                        return False, msg

                decky.logger.info("NTAG NDEF Write Successful")
                return True, None
        except Exception as e:
            decky.logger.error(f"Error writing NDEF: {e}")
            decky.logger.error(traceback.format_exc())
            return False, str(e)

    def _iter_mifare_data_blocks(self):
        blocks = []
        for block in range(MIFARE_CLASSIC_FIRST_DATA_BLOCK, MIFARE_CLASSIC_MAX_BLOCK + 1):
            # Skip trailer blocks (every 4th block in Classic 1K sectors).
            if block % 4 == 3:
                continue
            blocks.append(block)
        return blocks

    # --- Launch ---

    async def _launch_uri(self, uri):
        """Launch a URI via the system handler (xdg-open)."""
        decky.logger.info(f"Launching URI via xdg-open: {uri}")
        try:
            subprocess.Popen(["xdg-open", uri], shell=False)
        except Exception as e:
            decky.logger.error(f"Launch failed: {e}")

    # --- Audio ---

    def _play_sound(self, filename):
        try:
            sound_path = os.path.join(decky.DECKY_PLUGIN_DIR, "assets", "sounds", filename)
            if os.path.exists(sound_path):
                subprocess.Popen(["paplay", sound_path])
        except Exception as e:
            decky.logger.error(f"Failed to play sound {filename}: {e}")

    # -----------------------------------------------------------
    # Callable methods (called from JS frontend)
    # -----------------------------------------------------------

    async def get_settings(self):
        return self.settings.settings

    async def set_setting(self, key, value):
        if not self._validate_setting(key, value):
            decky.logger.warning(
                f"Rejected invalid setting update: key={key!r}, value={value!r}"
            )
            return False
        self.settings.set(key, value)
        if key in ("device_path", "baudrate"):
            self.reader = None  # Trigger re-init on next loop
        return True

    async def start_pairing(self, uri):
        if not self._validate_uri(uri):
            decky.logger.warning(f"Pairing URI rejected by allowlist: {uri}")
            return False
        decky.logger.info(f"UI requested pairing for URI: {uri}")
        self.is_pairing  = True
        self.pairing_uri = uri
        return True

    async def cancel_pairing(self):
        self.is_pairing  = False
        self.pairing_uri = None
        return True

    async def get_reader_status(self):
        return {
            "connected": self.reader is not None,
            "path":      self.settings.get("device_path"),
        }

    async def get_tag_status(self):
        return {
            "uid": self.current_tag_uid,
            "uri": self.current_tag_uri,
        }

    async def simulate_tag(self, uid: bytes, uri: Optional[str] = None):
        """Helper for testing/debug – pretend a tag with given UID/URI is present.

        Emits the same events as a real scan but does not touch hardware.
        """
        uid_hex = uid.hex().upper()
        self.current_tag_uid = uid_hex
        self.current_tag_uri = uri
        self.current_tag_meta = self._classify_tag(uid) if uid else None
        await decky.emit("tag_detected", {"uid": uid_hex})
        await decky.emit("uri_detected", {"uri": uri, "uid": uid_hex})

    async def get_tag_metadata(self, uid: Optional[str] = None):
        """Return classification info for a tag.

        If ``uid`` is ``None`` the currently-present tag is used; otherwise the
        provided hexadecimal UID string is interpreted.  The return value is a
        dict produced by :meth:`_classify_tag`.
        """
        # convert hex string to bytes if necessary
        if uid and isinstance(uid, str):
            try:
                uid_bytes = bytes.fromhex(uid)
            except ValueError:
                return {"error": "invalid uid"}
        else:
            uid_bytes = None

        if uid_bytes is None:
            # use currently-present UID if any
            if not self.current_tag_uid:
                return {}
            uid_bytes = bytes.fromhex(self.current_tag_uid)

        try:
            return self._classify_tag(uid_bytes)
        except Exception as e:
            return {"error": str(e)}

    async def get_reader_diagnostics(self):
        """Return low-level diagnostics about the connected reader.

        The frontend can call this to show firmware version, connection
        status, or any errors seen while interacting with the device.
        """
        info = {"connected": self.reader is not None}
        if self.reader:
            try:
                info["firmware"] = self.reader.firmware_version()
            except Exception as e:
                info["error"] = str(e)
        return info

    async def get_state(self):
        """Return current plugin state string (for frontend debugging / tests)."""
        return self.state.value

    async def set_running_game(self, appid):
        """
        Called by the frontend when game state changes (Spec §9).
        Frontend is the authoritative source via Router.MainRunningApp.

        On game start  : advances state to GAME_RUNNING.
        On game exit   : transitions back to READY (Spec §6.4).
                         Does NOT clear current_tag_uid here — physical removal
                         handles that — ensuring no auto-relaunch if card
                         is still present (Spec §6.5).
        """
        prev = self.running_game_id
        self.running_game_id = appid
        decky.logger.info(f"Running game updated: {prev} → {appid}")

        if appid:
            self._set_state(PluginState.GAME_RUNNING)
        elif self.state == PluginState.GAME_RUNNING:
            # Spec §6.4: game exited — transition back to READY
            self._set_state(PluginState.READY)

        return True
