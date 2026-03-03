import os
import sys
import asyncio
import time
import json
import traceback
import subprocess
from enum import Enum
from urllib.parse import urlparse

# Add vendored modules to path
import decky
py_modules_path = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
if py_modules_path not in sys.path:
    sys.path.insert(0, py_modules_path)

import serial
from adafruit_pn532.uart import PN532_UART
import ndef

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
MIFARE_CLASSIC_FIRST_DATA_BLOCK = 4
MIFARE_CLASSIC_MAX_BLOCK = 62
MIFARE_CLASSIC_BLOCK_SIZE = 16

ALLOWED_SETTING_KEYS = {
    "device_path",
    "baudrate",
    "polling_interval",
    "auto_launch",
    "auto_close",
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
        return False


# -----------------------------------------------------------------------
# Plugin
# -----------------------------------------------------------------------

class Plugin:

    # --- Lifecycle ---

    async def _main(self):
        decky.logger.info("Decky Links starting...")
        self.settings = SettingsManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        )
        self.state           = PluginState.IDLE
        self.reader          = None
        self.uart            = None
        self.is_pairing      = False
        self.pairing_uri     = None
        self.running_game_id = None
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.polling_task    = asyncio.create_task(self._nfc_loop())

    async def _unload(self):
        decky.logger.info("Decky Links unloading...")
        if hasattr(self, "polling_task"):
            self.polling_task.cancel()
        if self.uart:
            self.uart.close()

    # --- State Machine ---

    def _set_state(self, new_state: PluginState):
        """Transition to a new state and log the change."""
        if self.state != new_state:
            decky.logger.info(f"State: {self.state.value} → {new_state.value}")
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
        if key not in ALLOWED_SETTING_KEYS:
            return False

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
                if not self.reader:
                    await self._init_reader()
                    if not self.reader:
                        self._set_state(PluginState.IDLE)
                        await asyncio.sleep(5)
                        continue

                # ---- Poll ----
                uid = self.reader.read_passive_target(timeout=0.2)

                if uid:
                    missing_count = 0
                    uid_hex = uid.hex().upper()
                    self.current_tag_uid = uid_hex
                    is_new_tag = (uid_hex != last_uid_hex)

                    if is_new_tag:
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

    # --- Reader Init ---

    async def _init_reader(self):
        path = self.settings.get("device_path")
        baud = self.settings.get("baudrate")

        if not os.path.exists(path):
            return

        try:
            decky.logger.info(f"Attempting to connect to PN532 on {path} at {baud}")
            self.uart   = serial.Serial(path, baudrate=baud, timeout=0.1)
            self.reader = PN532_UART(self.uart, debug=False)

            version = self.reader.firmware_version
            if version:
                decky.logger.info(f"Connected to PN532: {version}")
                self.reader.SAM_configuration()
                self._set_state(PluginState.READY)
                await decky.emit("reader_status", {"connected": True, "path": path})
            else:
                decky.logger.error("Failed to get PN532 firmware version")
                self.uart.close()
                self.reader = None
                self.uart   = None
        except Exception as e:
            decky.logger.error(f"Init reader failed: {e}")
            self.reader = None

    # --- Scan Handler ---

    async def _handle_scan(self, uid):
        """
        Handle a newly detected NFC tag (Spec §6.2).
        Plays scan audio, reads URI, validates it, then either:
          - delegates Steam launches to the frontend (avoid dual-launch race), or
          - launches non-Steam URIs directly via xdg-open.
        """
        self._set_state(PluginState.CARD_PRESENT)

        # Audio feedback (Spec §11)
        self._play_sound("scan.flac")

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

    def _read_ndef_uri(self):
        """
        Read an NDEF URI record from the currently-presented tag (Spec §3.1).
        Returns the URI string on success, None on failure.
        """
        try:
            uid = self.reader.read_passive_target(timeout=0.1)
            if not uid:
                return None

            # Try to authenticate Mifare Classic Sector 1 (Block 4)
            authenticated = False
            keys = [
                b'\xFF\xFF\xFF\xFF\xFF\xFF',
                b'\xD3\xF7\xD3\xF7\xD3\xF7',
                b'\xA0\xA1\xA2\xA3\xA4\xA5',
            ]
            for key in keys:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
                time.sleep(0.05)

            if not authenticated:
                decky.logger.info("Auth failed for block 4 – attempting raw read fallback")

            data = bytearray()
            for i in self._iter_mifare_data_blocks():
                block = self.reader.mifare_classic_read_block(i)
                if block:
                    data.extend(block)
                    if 0xFE in block:
                        break
                else:
                    break

            if not data:
                return None

            # NDEF TLV (Type 0x03 = NDEF Message) — spec §3.1 uses NDEF URI records
            if len(data) > 2 and data[0] == 0x03:
                length    = data[1]
                ndef_data = data[2:2 + length]
                for record in ndef.message_decoder(ndef_data):
                    if isinstance(record, ndef.UriRecord):
                        return record.uri

            # Fallback: raw URI string encoded directly on tag
            try:
                import re
                decoded = data.decode("utf-8", errors="ignore").strip("\x00")
                match   = re.search(r"[a-zA-Z0-9]+://[^\s\x00]+", decoded)
                if match:
                    return match.group(0)
            except Exception:
                pass

        except Exception as e:
            decky.logger.error(f"Error reading NDEF: {e}")

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
        Write a URI as an NDEF URI record to a Mifare Classic tag (Spec §3.1).
        Enforces NTAG213 capacity limit (Spec §3.3).
        Returns (True, None) on success or (False, error_message) on failure.
        """
        # Capacity check (Spec §3.3)
        # Estimate: TLV header (2 bytes) + NDEF record header (~4 bytes) +
        #           NDEF URI prefix byte (1) + URI bytes + TLV terminator (1 byte)
        uri_bytes      = uri.encode("utf-8")
        estimated_size = 2 + 4 + 1 + len(uri_bytes) + 1
        if estimated_size > NTAG213_MAX_PAYLOAD_BYTES:
            msg = (
                f"URI too long: estimated {estimated_size} bytes "
                f"exceeds NTAG213 limit of {NTAG213_MAX_PAYLOAD_BYTES} bytes."
            )
            decky.logger.error(msg)
            return False, msg

        try:
            authenticated = False
            keys = [
                b'\xFF\xFF\xFF\xFF\xFF\xFF',
                b'\xD3\xF7\xD3\xF7\xD3\xF7',
                b'\xA0\xA1\xA2\xA3\xA4\xA5',
            ]
            for key in keys:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
                time.sleep(0.05)

            if not authenticated:
                msg = "Authentication failed: Default keys rejected."
                decky.logger.error(f"{msg} (UID: {uid.hex()})")
                return False, msg

            # Build NDEF URI record (Spec §3.1)
            record  = ndef.UriRecord(uri)
            message = b"".join(ndef.message_encoder([record]))

            # Wrap in NDEF TLV: [0x03][Length][Message][0xFE]
            tlv = bytearray([0x03, len(message)]) + message + b"\xFE"

            # Pad to Mifare Classic block size (16 bytes)
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
                block_data = tlv[i:i + 16]
                decky.logger.info(f"Writing NDEF block {block_num}: {block_data.hex()}")
                if not self.reader.mifare_classic_write_block(block_num, block_data):
                    msg = f"Write failed at block {block_num}"
                    decky.logger.error(msg)
                    return False, msg

            decky.logger.info("NDEF Write Successful")
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
