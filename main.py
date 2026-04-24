import os
import sys
import asyncio
import time
import json
import traceback
import subprocess
import threading
import re
from enum import Enum
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List, Tuple
from collections import OrderedDict

from sources import (
    SourceType,
    SourceEventKind,
    MediaEventKind,
    SourceEvent,
    MediaEvent,
    PluginEvent,
    SourceManager,
)
from sources.nfc_source import NfcSource

# Add vendored modules to path
import decky
py_modules_path = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
if py_modules_path not in sys.path:
    sys.path.insert(0, py_modules_path)

# Add plugin directory to path so nfc module can be imported
plugin_dir = decky.DECKY_PLUGIN_DIR
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

import ndef

# Reader abstraction hides hardware-specific details
from nfc.reader import PN532UARTReader
from nfc.key_manager import KeyManager
from nfc.signature_manager import SignatureManager


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

# Regex for validating Steam app IDs (1-10 digits, max ~4 billion)
STEAM_APPID_PATTERN = re.compile(r'^[0-9]{1,10}$')

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

TOP_LEVEL_SETTING_KEYS = {
    "auto_launch",
    "auto_close",
}

NFC_SETTING_KEYS = {
    "device_path",
    "baudrate",
    "polling_interval",
    "reader_type",
}

ALLOWED_SETTING_KEYS = TOP_LEVEL_SETTING_KEYS | NFC_SETTING_KEYS


# -----------------------------------------------------------------------
# State Machine (Spec §5)
# -----------------------------------------------------------------------

class PluginState(Enum):
    """Plugin state machine (Spec §5).
    
    State transitions:
    - IDLE → READY: Reader connected and initialized
    - READY → CARD_PRESENT: New tag detected
    - CARD_PRESENT → READY: Tag removed (no game running)
    - CARD_PRESENT → GAME_RUNNING: Game launched (auto_launch enabled)
    - GAME_RUNNING → READY: Game exited (via set_running_game)
    - GAME_RUNNING → READY: Tag removed during game (after card_removed_during_game event)
    - Any state → IDLE: Reader disconnected or error
    
    Key invariants:
    - Only one tag is active at a time (single active card model)
    - No auto-relaunch: requires physical card reinsertion
    - Game state is authoritative from frontend (Router.MainRunningApp)
    """
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
            "auto_launch": True,
            "auto_close": False,
            "sources": {
                "nfc": {
                    "device_path": self._get_default_device_path(),
                    "baudrate": 115200,
                    "polling_interval": 0.5,
                    "reader_type": "pn532_uart",
                }
            },
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
                    self._merge_loaded_settings(loaded)
        except Exception as e:
            decky.logger.error(f"Failed to load settings: {e}")

    def save(self):
        try:
            dir_path = os.path.dirname(self.path)
            os.makedirs(dir_path, exist_ok=True)
            # Check write permissions before attempting to write
            if not os.access(dir_path, os.W_OK):
                decky.logger.error(f"No write permission for settings directory: {dir_path}")
                return
            with open(self.path, "w") as f:
                json.dump(self.settings, f, indent=4)
        except IOError as e:
            decky.logger.error(f"Failed to write settings file {self.path}: {e}")
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")

    def get(self, key):
        if key in TOP_LEVEL_SETTING_KEYS:
            return self.settings.get(key)
        if key in NFC_SETTING_KEYS:
            return self.settings["sources"]["nfc"].get(key)
        return self.settings.get(key)

    def set(self, key, value):
        if key in TOP_LEVEL_SETTING_KEYS:
            self.settings[key] = value
        elif key in NFC_SETTING_KEYS:
            self.settings["sources"]["nfc"][key] = value
        elif key == "sources.nfc" and isinstance(value, dict):
            self.settings["sources"]["nfc"].update(value)
        else:
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
            return isinstance(value, str) and value in ("pn532_uart", "acr122u", "proxmark", "nfcpy")
        return False

    def get_source_settings(self, source_type: str) -> Dict[str, Any]:
        sources = self.settings.setdefault("sources", {})
        source_settings = sources.setdefault(source_type, {})
        return source_settings

    def _merge_loaded_settings(self, loaded: Dict[str, Any]) -> None:
        for key in TOP_LEVEL_SETTING_KEYS:
            if key in loaded:
                value = loaded[key]
                if self._validate_setting(key, value):
                    self.settings[key] = value
                else:
                    decky.logger.warning(
                        f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                    )

        loaded_sources = loaded.get("sources")
        if isinstance(loaded_sources, dict):
            loaded_nfc = loaded_sources.get("nfc", {})
            if isinstance(loaded_nfc, dict):
                for key, value in loaded_nfc.items():
                    if key in NFC_SETTING_KEYS and self._validate_setting(key, value):
                        self.settings["sources"]["nfc"][key] = value
                    elif key in NFC_SETTING_KEYS:
                        decky.logger.warning(
                            f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                        )

        for key in NFC_SETTING_KEYS:
            if key in loaded:
                value = loaded[key]
                if self._validate_setting(key, value):
                    self.settings["sources"]["nfc"][key] = value
                else:
                    decky.logger.warning(
                        f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                    )


# -----------------------------------------------------------------------
# Plugin
# -----------------------------------------------------------------------

class Plugin:

    RECONNECT_DELAY_MIN = 1.0
    RECONNECT_DELAY_MAX = 30.0
    DEBOUNCE_THRESHOLD = 3  # Consecutive None reads required to confirm tag removal

    def __init__(self):
        self.settings = None
        self.key_manager = None
        self.signature_manager = None
        self.nfc_source = None
        self.source_manager = None
        self._legacy_reader = None
        self.uart = None
        self.state = "IDLE"
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.running_game_id = None
        self.is_pairing = False

    @property
    def reader(self):
        if hasattr(self, "nfc_source") and self.nfc_source:
            return self.nfc_source.reader
        return self._legacy_reader

    @reader.setter
    def reader(self, value):
        if self.nfc_source:
            self.nfc_source._reader = value
        self._legacy_reader = value

    # --- Lifecycle ---

    async def _main(self):
        decky.logger.info("Decky Links starting...")
        self.settings = SettingsManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        )
        self.key_manager = KeyManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "keys.json"),
            logger=decky.logger
        )
        self.signature_manager = SignatureManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "signing_keys.json"),
            logger=decky.logger
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
        # RPC call caching to reduce load with thread-safe lock
        self._tag_status_lock = threading.RLock()
        self._last_tag_status_query = 0
        self._tag_status_cache = None
        # Tag classification cache (UID hex -> metadata dict) with LRU eviction
        self._tag_classification_cache = OrderedDict()
        self._tag_cache_max_size = 128

        # --- Source-based architecture ---
        self._event_queue: asyncio.Queue[PluginEvent] = asyncio.Queue()
        self.nfc_source = NfcSource(
            settings=self.settings.get_source_settings("nfc"),
            key_manager=self.key_manager,
            signature_manager=self.signature_manager,
            logger=decky.logger,
        )
        self.source_manager = SourceManager(
            event_queue=self._event_queue,
            logger=decky.logger,
        )
        self.source_manager.register(self.nfc_source)

        await self.source_manager.start_all()
        self.polling_task = asyncio.create_task(self._event_loop())

    async def _unload(self):
        decky.logger.info("Decky Links unloading...")
        if hasattr(self, "polling_task"):
            self.polling_task.cancel()
        if hasattr(self, "source_manager"):
            await self.source_manager.stop_all()
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

    # --- Event Loop (replaces old _nfc_loop) ---

    async def _event_loop(self):
        """Consume events from the shared queue and dispatch to handlers.

        This is the main loop that replaced the old ``_nfc_loop``.
        SourceManager feeds events from all registered sources into
        ``self._event_queue``; this loop processes them sequentially.
        """
        while True:
            try:
                event = await self._event_queue.get()
                if isinstance(event, SourceEvent):
                    await self._handle_source_event(event)
                elif isinstance(event, MediaEvent):
                    await self._handle_media_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"Event loop error: {e}")
                decky.logger.error(traceback.format_exc())

    async def _handle_source_event(self, event: SourceEvent):
        """Handle hardware lifecycle events (connect/disconnect)."""
        if event.kind == SourceEventKind.CONNECTED:
            decky.logger.info(
                f"Source connected: {event.source_type.value} ({event.source_id})"
            )
            # Sync plugin-level reader reference for backward compatibility
            if event.source_type == SourceType.NFC:
                self.reader = self.nfc_source.reader
            self._set_state(PluginState.READY)
            await decky.emit("reader_status", {
                "connected": True,
                "path": self.settings.get("device_path"),
                "source_type": event.source_type.value,
            })

        elif event.kind == SourceEventKind.DISCONNECTED:
            decky.logger.info(
                f"Source disconnected: {event.source_type.value} ({event.source_id})"
            )
            if event.source_type == SourceType.NFC:
                self.reader = None
            self._set_state(PluginState.IDLE)
            await decky.emit("reader_status", {
                "connected": False,
                "source_type": event.source_type.value,
            })

    async def _handle_media_event(self, event: MediaEvent):
        """Handle media interaction events (tag tap, floppy insert, etc.)."""
        if event.kind == MediaEventKind.LOAD:
            await self._handle_media_load(event)
        elif event.kind == MediaEventKind.UNLOAD:
            await self._handle_media_unload(event)

    async def _handle_media_load(self, event: MediaEvent):
        """Handle a new media presentation (tag detected, disk inserted, etc.).

        For NFC sources, this replaces the old _handle_scan logic.
        """
        uid_hex = event.media_id
        uri = event.uri

        # Collision check (Spec §6.2)
        if hasattr(self, "current_tag_uid") and self.current_tag_uid and self.current_tag_uid != uid_hex:
            decky.logger.info(f"Multiple media detected: {self.current_tag_uid}, {uid_hex}")
            await decky.emit("multiple_tags", {
                "previous": self.current_tag_uid,
                "current":  uid_hex,
            })

        # Sync plugin-level state for backward compatibility
        self.current_tag_uid = uid_hex
        self.current_tag_uri = uri

        # Sync NFC-specific metadata
        if event.source_type == SourceType.NFC:
            self.current_tag_meta = event.payload.get("tag_meta")
            self.reader = self.nfc_source.reader

        self._set_state(PluginState.CARD_PRESENT)

        # Audio feedback (Spec §11)
        self._play_sound("scan.flac")

        # Emit tag_detected event
        await decky.emit("tag_detected", {
            "uid": uid_hex,
            "source_type": event.source_type.value,
        })

        # Emit NDEF records if available (NFC-specific)
        if "ndef_records" in event.payload:
            await decky.emit("ndef_detected", {"records": event.payload["ndef_records"]})

        # Emit tag metadata if available
        if event.source_type == SourceType.NFC and self.current_tag_meta:
            await decky.emit("tag_metadata", self.current_tag_meta)

        # Emit URI detected
        await decky.emit("uri_detected", {
            "uri": uri,
            "uid": uid_hex,
            "source_type": event.source_type.value,
        })

        # No URI — play error sound (Spec §12)
        if not uri:
            decky.logger.info(f"No URI found on media {uid_hex}")
            self._play_sound("error.flac")
            self.current_tag_uri = None
            self._set_state(PluginState.READY)
            return

        # Allowlist check (Spec §4)
        if not self._validate_uri(uri):
            decky.logger.warning(f"URI blocked by allowlist: {uri}")
            self._play_sound("error.flac")
            self.current_tag_uri = None
            await decky.emit("uri_detected", {
                "uri": None,
                "uid": uid_hex,
                "blocked": True,
                "source_type": event.source_type.value,
            })
            self._set_state(PluginState.READY)
            return

        decky.logger.info(f"URI found on media {uid_hex}: {uri}")

        # Handle pairing mode
        if self.is_pairing and event.source_type == SourceType.NFC:
            uid_bytes = bytes.fromhex(uid_hex)
            await self._handle_pairing(uid_bytes)
            return

        if not self.settings.get("auto_launch"):
            return

        # Spec §8.1: Do not launch if any game is already running
        if self.running_game_id:
            decky.logger.info(f"Launch blocked: game {self.running_game_id} already running.")
            self._set_state(PluginState.GAME_RUNNING)
            return

        if uri.startswith("steam://"):
            decky.logger.info(f"Steam URI: frontend will handle launch for: {uri}")
        else:
            decky.logger.info(f"Backend launching URI: {uri}")
            await self._launch_uri(uri)

    async def _handle_media_unload(self, event: MediaEvent):
        """Handle media removal (tag removed, disk ejected, etc.).

        Replaces the old _nfc_loop_notify_removal logic.
        """
        removed_uid = event.media_id
        removed_uri = event.uri

        # Spec §6.3: removal during active game → notify frontend
        if self.state == PluginState.GAME_RUNNING and not self.is_pairing:
            decky.logger.info(
                f"Media removed while game {self.running_game_id} active. "
                f"Notifying frontend."
            )
            await decky.emit("card_removed_during_game", {
                "appid": self.running_game_id,
                "uid":   removed_uid,
                "uri":   removed_uri,
                "source_type": event.source_type.value,
            })
        else:
            decky.logger.info(
                f"Media removed. State={self.state.value}, Pairing={self.is_pairing}"
            )

        self.current_tag_uid = None
        self.current_tag_uri = None
        if event.source_type == SourceType.NFC:
            self.current_tag_meta = None
        await decky.emit("tag_removed", {
            "source_type": event.source_type.value,
        })

        # Spec §6.6: card removed while READY → state stays READY
        if self.state not in (PluginState.GAME_RUNNING, PluginState.IDLE):
            self._set_state(PluginState.READY)

    # --- URI Validation (Spec §4) ---

    def _validate_uri(self, uri: str) -> bool:
        """
        Returns True when uri is permitted by the protocol allowlist.
        Allowed: steam://run/*, steam://rungameid/*, and https:// only.
        
        Validates format strictly to prevent injection attacks.
        """
        if not isinstance(uri, str) or not uri or len(uri) > 2048:
            return False
        
        # Validate Steam URIs
        for prefix in ALLOWED_STEAM_URI_PREFIXES:
            if uri.startswith(prefix):
                # Extract and validate app ID
                remainder = uri[len(prefix):]
                app_id = remainder.split('/')[0]  # Get first path component
                
                if not app_id or not STEAM_APPID_PATTERN.match(app_id):
                    decky.logger.warning(f"Invalid Steam app ID: {app_id}")
                    return False
                
                # Ensure no path traversal after app ID
                if '/' in remainder and not remainder.startswith(app_id + '/'):
                    decky.logger.warning(f"Suspicious Steam URI format: {uri}")
                    return False
                
                return True
        
        # Validate HTTPS URIs
        if uri.startswith("https://"):
            try:
                parsed = urlparse(uri)
                # Validate domain format (basic check)
                if not parsed.netloc or '.' not in parsed.netloc:
                    return False
                # Reject localhost/private IPs
                if parsed.netloc in ('localhost', '127.0.0.1', '::1'):
                    return False
                return True
            except Exception:
                return False
        
        return False

    def _validate_setting(self, key, value) -> bool:
        # same logic as SettingsManager but available on Plugin as well
        if key not in ALLOWED_SETTING_KEYS:
            return False
        if key == "reader_type":
            return isinstance(value, str) and value in ("pn532_uart", "acr122u", "proxmark", "nfcpy")
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



    # --- Removal Notification (extracted for testability) ---

    async def _nfc_loop_notify_removal(self):
        """Wrapper for backward compatibility / tests."""
        event = MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.NFC,
            source_id="nfc_internal",
            media_id=self.current_tag_uid,
            uri=self.current_tag_uri
        )
        await self._handle_media_unload(event)

    async def _init_reader(self):
        """Wrapper for tests calling this directly."""
        if not self.nfc_source:
            # Ensure we pass a real dict to NfcSource even if self.settings is mocked
            settings_dict = {}
            if self.settings:
                get_source_settings = getattr(self.settings, "get_source_settings", None)
                if callable(get_source_settings):
                    candidate = get_source_settings("nfc")
                    if isinstance(candidate, dict):
                        settings_dict = candidate
                elif hasattr(self.settings, "settings") and isinstance(self.settings.settings, dict):
                    settings_dict = self.settings.settings.get("sources", {}).get("nfc", {})

                if not settings_dict:
                    for k in NFC_SETTING_KEYS:
                        settings_dict[k] = self.settings.get(k)

            self.nfc_source = NfcSource(
                settings=settings_dict,
                key_manager=self.key_manager,
                signature_manager=self.signature_manager,
                logger=decky.logger
            )
        reader = await self._create_reader()
        if not reader:
            self.reader = None
            return False

        # Some tests expect reader.connect() to be called
        if hasattr(reader, "connect"):
            connected = await reader.connect()
            if connected:
                self.reader = reader
            else:
                self.reader = None
            return connected

        self.reader = reader
        return True

    async def _nfc_loop(self):
        """Wrapper for tests that drive the polling loop manually."""
        while True:
            if not self.nfc_source or not self.nfc_source.reader:
                await self._init_reader()
            else:
                try:
                    if not self.nfc_source.reader.is_connected():
                        await self.nfc_source.stop()
                except Exception:
                    await self.nfc_source.stop()
            await asyncio.sleep(0.01)

    async def _create_reader(self):
        """Wrapper for tests calling this directly."""
        if not self.nfc_source:
            await self._init_reader()
        else:
            self._sync_nfc_source_settings()
            try:
                import sources.nfc_source as nfc_source_module
                nfc_source_module.PN532UARTReader = PN532UARTReader
            except Exception:
                pass
        return await self.nfc_source._create_reader()

    def _sync_nfc_source_settings(self):
        if not self.nfc_source or not self.settings:
            return
        for key in NFC_SETTING_KEYS:
            value = self.settings.get(key)
            if value is not None:
                self.nfc_source._settings[key] = value

    async def _handle_scan(self, uid):
        """Wrapper for backward compatibility / tests."""
        uid_hex = uid.hex().upper()
        # Note: this might trigger hardware reads if called outside normal flow
        uri = self._read_ndef_uri()
        
        # Use existing metadata if pre-loaded by tests, else classify
        meta = getattr(self, "current_tag_meta", None)
        if not meta:
            meta = self._classify_tag(uid)
        
        # Convert records to serializable format for the event payload
        records = self._read_ndef_records()
        serializable_records = []
        for record in records:
            rec_dict = {}
            for attr in ['type', 'name', 'uri', 'text', 'language', 'encoding']:
                if hasattr(record, attr):
                    rec_dict[attr] = getattr(record, attr)
            serializable_records.append(rec_dict)

        event = MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.NFC,
            source_id="nfc_internal",
            media_id=uid_hex,
            uri=uri,
            payload={
                "ndef_records": serializable_records,
                "tag_meta": meta
            }
        )
        await self._handle_media_load(event)
    # --- Scan Handler ---



    # --- NDEF Read ---

    def _iter_ntag_pages(self):
        return self.nfc_source._iter_ntag_pages()

    def _is_ntag(self, uid) -> bool:
        meta = self.nfc_source._classify_tag(uid)
        return meta.get("type") in ("ntag21x", "ultralight")

    def _classify_tag(self, uid: bytes) -> Dict[str, Any]:
        return self.nfc_source._classify_tag(uid)

    def _cache_tag_classification(self, uid_hex: str, meta: Dict[str, Any]) -> None:
        """Cache tag classification metadata with LRU eviction.
        
        Prevents unbounded memory growth by evicting oldest entries when
        cache exceeds max size.
        """
        self._tag_classification_cache[uid_hex] = meta
        
        # Simple LRU: if cache exceeds max size, remove oldest entry
        if len(self._tag_classification_cache) > self._tag_cache_max_size:
            # Remove first (oldest) entry
            oldest_key = next(iter(self._tag_classification_cache))
            del self._tag_classification_cache[oldest_key]
            decky.logger.debug(f"Tag classification cache evicted {oldest_key}")

    def _read_ndef_records(self) -> List[Any]:
        """Read and return all NDEF records present on the current tag.
        
        Kept in main.py for backward compatibility with tests patching 'main.ndef'.
        """
        uid = self.nfc_source.reader.read_uid(timeout=0.1)
        if not uid:
            return []

        is_ntag = self._is_ntag(uid)
        data = bytearray()

        # Decide whether to use page-based (NTAG) or block-based (Mifare) reads
        if is_ntag:
            blocks_iter = self._iter_ntag_pages()
            read_fn = self.nfc_source.reader.ntag2xx_read_block
        else:
            blocks_iter = self.nfc_source._iter_mifare_data_blocks()
            read_fn = self.nfc_source.reader.mifare_classic_read_block

        for i in blocks_iter:
            block = read_fn(i)
            if block:
                data.extend(block)
                if 0xFE in block: # NDEF terminator
                    break
            else:
                break

        if not data:
            return []

        records = []
        # Basic TLV parsing: [Type=0x03 (NDEF)][Length][Payload...][Terminator=0xFE]
        if len(data) > 2 and data[0] == 0x03:
            length = data[1]
            ndef_data = data[2:2+length]
            try:
                for rec in ndef.message_decoder(ndef_data):
                    records.append(rec)
            except Exception:
                # Fallback: simple URI extraction if decoder fails
                if len(ndef_data) > 3:
                    for i in range(len(ndef_data)-2):
                        if ndef_data[i] == 0x55: # URI record type byte
                            uri_data = ndef_data[i+2:]
                            if uri_data:
                                try:
                                    uri_str = uri_data.decode('utf-8', errors='ignore').strip('\x00\xfe')
                                    if uri_str:
                                        records.append(ndef.UriRecord(uri_str))
                                        break
                                except Exception:
                                    pass

        # Ultimate fallback: regex search for anything resembling a URI in raw data
        if not records:
            try:
                decoded = data.decode('utf-8', errors='ignore').strip('\x00')
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
            # Check class name to allow for stubbed UriRecord classes in tests
            if hasattr(record, "uri") and record.__class__.__name__.endswith("UriRecord"):
                return record.uri
        return None

    # --- Pairing Handler ---

    async def _handle_pairing(self, uid):
        """Write the pairing URI to the NFC tag (Spec §7)."""
        if not self.pairing_uri:
            decky.logger.warning("Pairing triggered but no URI set!")
            self.is_pairing = False
            self.pairing_uri = None
            return

        # Atomic state update: exit pairing mode immediately to prevent
        # new tags from interfering with the write operation
        pairing_uri = self.pairing_uri
        self.is_pairing = False
        self.pairing_uri = None

        decky.logger.info(f"Pairing: writing {pairing_uri} to tag {uid.hex()}")
        try:
            success, error_msg = self._write_ndef_uri(uid, pairing_uri)
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

    # --- NDEF Write ---

    def _write_ndef_uri(self, uid: bytes, uri: str) -> Tuple[bool, Optional[str]]:
        """
        Write a URI as an NDEF URI record to the currently-presented tag.

        Supports both Mifare‑Classic and NTAG21x devices.  A successful write
        returns ``(True, None)``; failures yield ``(False, error_message)``.
        """
        # --- prepare TLV payload ------------------------------------------------
        uri_bytes      = uri.encode("utf-8")

        # Build NDEF record and wrap in TLV; length may be adjusted later.
        try:
            record  = ndef.UriRecord(uri)
            message = b"".join(ndef.message_encoder([record]))
            tlv     = bytearray([0x03, len(message)]) + message + b"\xFE"
        except Exception as e:
            return (False, f"Failed to create NDEF record: {e}")

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
        # Defensive validation: ensure URI is safe before passing to subprocess
        if not self._validate_uri(uri):
            decky.logger.error(f"Attempted to launch invalid URI: {uri}")
            return
        
        decky.logger.info(f"Launching URI via xdg-open: {uri}")
        try:
            subprocess.Popen(["xdg-open", uri], shell=False)
        except Exception as e:
            decky.logger.error(f"Launch failed: {e}")

    # --- Audio ---

    def _play_sound(self, filename):
        """Play a sound file from the assets/sounds directory.
        
        Only whitelisted sound files are allowed to prevent path traversal attacks.
        """
        # Whitelist allowed sounds
        ALLOWED_SOUNDS = {"scan.flac", "success.flac", "error.flac"}
        
        if filename not in ALLOWED_SOUNDS:
            decky.logger.warning(f"Attempted to play unauthorized sound: {filename}")
            return
        
        try:
            sound_path = os.path.join(decky.DECKY_PLUGIN_DIR, "assets", "sounds", filename)
            
            # Verify file exists before attempting to play
            if not os.path.exists(sound_path):
                decky.logger.error(f"Sound file not found: {sound_path}")
                return
            
            # Verify it's a regular file (not a directory or symlink to sensitive location)
            if not os.path.isfile(sound_path):
                decky.logger.error(f"Sound path is not a regular file: {sound_path}")
                return
            
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
        if key in ("device_path", "baudrate", "reader_type"):
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
            "connected": self.nfc_source.reader is not None,
            "path":      self.settings.get("device_path"),
            "source_type": SourceType.NFC.value,
        }

    async def get_tag_status(self):
        """Get current tag status with thread-safe caching to reduce load.
        
        Results are cached for 100ms to avoid excessive polling.
        """
        now = time.time()
        
        with self._tag_status_lock:
            # Return cached result if still fresh (100ms cache)
            if (now - self._last_tag_status_query) < 0.1 and self._tag_status_cache is not None:
                return self._tag_status_cache
            
            # Update cache atomically
            self._last_tag_status_query = now
            self._tag_status_cache = {
                "uid": self.current_tag_uid,
                "uri": self.current_tag_uri,
            }
            return self._tag_status_cache

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

    async def set_tag_key(self, uid: str, key_a: str, key_b: str):
        """Store custom Mifare Classic authentication keys for a tag UID.

        Args:
            uid: Tag UID as hex string (e.g. "04A1B2C3D4E5F6")
            key_a: Key A as 12-char hex string (6 bytes)
            key_b: Key B as 12-char hex string (6 bytes)

        Returns:
            True if keys were stored successfully, False otherwise.
        """
        # Validate UID format
        if not isinstance(uid, str) or not uid:
            decky.logger.warning("Invalid UID: must be non-empty string")
            return False
        
        try:
            bytes.fromhex(uid)  # Validate hex format
        except ValueError:
            decky.logger.warning(f"Invalid UID format (not hex): {uid}")
            return False
        
        try:
            self.key_manager.set_key(uid.upper(), key_a, key_b)
            decky.logger.info(f"Stored custom keys for tag {uid.upper()}")
            return True
        except ValueError as e:
            decky.logger.warning(f"Invalid key format: {e}")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to store keys: {e}")
            return False

    async def get_tag_key(self, uid: str):
        """Retrieve stored Mifare Classic authentication keys for a tag UID.

        Args:
            uid: Tag UID as hex string

        Returns:
            Dict with 'key_a' and 'key_b' if found, empty dict otherwise.
        """
        try:
            keys = self.key_manager.get_keys(uid)
            if keys:
                return {"key_a": keys[0], "key_b": keys[1]}
            return {}
        except Exception as e:
            decky.logger.error(f"Failed to retrieve keys: {e}")
            return {}

    async def list_tag_keys(self):
        """List all stored tag UIDs with custom keys.

        Returns:
            List of tag UIDs that have custom keys stored.
        """
        try:
            return self.key_manager.list_keys()
        except Exception as e:
            decky.logger.error(f"Failed to list keys: {e}")
            return []

    async def get_sector_info(self, uid: Optional[str] = None):
        """Get sector lock status for current or specified tag.
        
        Args:
            uid: Optional tag UID hex string. If None, uses current tag.
            
        Returns:
            List of sector info dicts, or empty list on error.
        """
        try:
            # Use current tag if no UID specified
            if uid:
                uid_bytes = bytes.fromhex(uid)
            elif self.current_tag_uid:
                uid_bytes = bytes.fromhex(self.current_tag_uid)
            else:
                decky.logger.warning("No tag present for sector info")
                return []
            
            # Get tag metadata to determine type
            meta = self._classify_tag(uid_bytes)
            if meta.get("type") != "mifare-classic":
                decky.logger.warning(f"Sector info only supported for Mifare Classic, got {meta.get('type')}")
                return []
            
            # Create handler and get sector info
            from nfc.tag_handlers import MifareClassicHandler
            handler = MifareClassicHandler(uid_bytes, self.key_manager)
            
            if not self.reader:
                decky.logger.error("No reader available for sector info")
                return []
            
            return handler.get_sector_info(self.reader)
        except Exception as e:
            decky.logger.error(f"Failed to get sector info: {e}")
            return []

    async def lock_sector(self, uid: str, sector: int, key_a: str, key_b: str):
        """Lock a sector on a Mifare Classic tag.
        
        Args:
            uid: Tag UID hex string
            sector: Sector number (0-15 for 1K, 0-39 for 4K)
            key_a: Key A hex string (12 chars = 6 bytes)
            key_b: Key B hex string (12 chars = 6 bytes)
            
        Returns:
            True if successful, False otherwise.
        """
        try:
            # Validate inputs
            if not uid or not isinstance(uid, str):
                decky.logger.warning("Invalid UID for sector lock")
                return False
            
            if len(key_a) != 12 or len(key_b) != 12:
                decky.logger.warning("Keys must be 12 hex characters")
                return False
            
            # Convert hex strings to bytes
            try:
                uid_bytes = bytes.fromhex(uid)
                key_a_bytes = bytes.fromhex(key_a)
                key_b_bytes = bytes.fromhex(key_b)
            except ValueError as e:
                decky.logger.warning(f"Invalid hex format: {e}")
                return False
            
            # Verify tag type and get capacity
            meta = self._classify_tag(uid_bytes)
            if meta.get("type") != "mifare-classic":
                decky.logger.warning(f"Sector locking only supported for Mifare Classic")
                return False
            
            # Determine max sectors based on capacity
            # Mifare Classic 1K: 16 sectors (64 bytes per sector)
            # Mifare Classic 4K: 40 sectors (32 bytes for sectors 0-31, 64 bytes for sectors 32-39)
            capacity = meta.get("capacity_bytes", 0)
            max_sectors = 40 if capacity > 2048 else 16
            
            if sector < 0 or sector >= max_sectors:
                decky.logger.warning(f"Invalid sector {sector} for {capacity}-byte tag (max {max_sectors - 1})")
                return False
            
            if not self.reader:
                decky.logger.error("No reader available for sector lock")
                return False
            
            # Create handler and lock sector
            from nfc.tag_handlers import MifareClassicHandler
            handler = MifareClassicHandler(uid_bytes, self.key_manager)
            
            success, error = handler.lock_sector(self.reader, sector, key_a_bytes, key_b_bytes)
            
            if not success:
                decky.logger.error(f"Failed to lock sector {sector}: {error}")
            else:
                decky.logger.info(f"Successfully locked sector {sector} on tag {uid}")
            
            return success
        except Exception as e:
            decky.logger.error(f"Failed to lock sector: {e}")
            return False

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

    async def generate_signing_key(self, key_id: str):
        """Generate new signing key pair.
        
        Args:
            key_id: Identifier for the key pair
            
        Returns:
            Dict with public_key or error
        """
        try:
            public_key, _ = self.signature_manager.generate_key_pair(key_id)
            decky.logger.info(f"Generated signing key: {key_id}")
            return {"success": True, "public_key": public_key}
        except Exception as e:
            decky.logger.error(f"Failed to generate key: {e}")
            return {"success": False, "error": str(e)}

    async def import_signing_key(self, key_id: str, public_key: str, private_key: Optional[str] = None):
        """Import existing signing key pair.
        
        Args:
            key_id: Identifier for the key pair
            public_key: Public key PEM
            private_key: Optional private key PEM
            
        Returns:
            Success boolean
        """
        try:
            self.signature_manager.import_key_pair(key_id, public_key, private_key)
            decky.logger.info(f"Imported signing key: {key_id}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to import key: {e}")
            return False

    async def delete_signing_key(self, key_id: str):
        """Delete a signing key pair.
        
        Args:
            key_id: Identifier for the key pair
            
        Returns:
            Success boolean
        """
        try:
            self.signature_manager.delete_key(key_id)
            decky.logger.info(f"Deleted signing key: {key_id}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to delete key: {e}")
            return False

    async def list_signing_keys(self):
        """List all signing key IDs.
        
        Returns:
            List of key IDs
        """
        try:
            return self.signature_manager.list_keys()
        except Exception as e:
            decky.logger.error(f"Failed to list keys: {e}")
            return []

    async def get_public_key(self, key_id: str):
        """Get public key for a key ID.
        
        Args:
            key_id: Identifier for the key pair
            
        Returns:
            Public key PEM or None
        """
        try:
            return self.signature_manager.get_public_key(key_id)
        except Exception as e:
            decky.logger.error(f"Failed to get public key: {e}")
            return None

    async def sign_uri(self, uri: str, key_id: str):
        """Sign a URI and return signed NDEF message.
        
        Args:
            uri: URI to sign
            key_id: Signing key ID
            
        Returns:
            Dict with signed_message (hex) or error
        """
        try:
            import ndef
            from nfc.signature_record import SignatureRecord, create_signed_ndef_message
            
            # Create URI record
            uri_record = ndef.UriRecord(uri)
            uri_bytes = b"".join(ndef.message_encoder([uri_record]))
            
            # Sign the URI bytes
            signature = self.signature_manager.sign_data(key_id, uri_bytes)
            
            # Create signature record
            sig_record = SignatureRecord(signature, key_id)
            sig_bytes = sig_record.to_ndef_record()
            
            # Combine into signed message
            signed_message = create_signed_ndef_message(uri_bytes, sig_bytes)
            
            decky.logger.info(f"Signed URI with key {key_id}")
            return {"success": True, "signed_message": signed_message.hex()}
        except Exception as e:
            decky.logger.error(f"Failed to sign URI: {e}")
            return {"success": False, "error": str(e)}

    async def verify_signature(self, signed_message_hex: str):
        """Verify signature in signed NDEF message.
        
        Args:
            signed_message_hex: Signed NDEF message as hex string
            
        Returns:
            Dict with valid boolean and details
        """
        try:
            from nfc.signature_record import SignatureRecord, extract_uri_from_signed_message
            
            signed_message = bytes.fromhex(signed_message_hex)
            uri_bytes, sig_bytes = extract_uri_from_signed_message(signed_message)
            
            if not uri_bytes or not sig_bytes:
                return {"valid": False, "error": "Invalid message format"}
            
            # Parse signature record
            sig_record = SignatureRecord.from_ndef_payload(sig_bytes[3:])  # Skip header
            if not sig_record:
                return {"valid": False, "error": "Invalid signature record"}
            
            # Verify signature
            valid = self.signature_manager.verify_signature(
                sig_record.key_id,
                uri_bytes,
                sig_record.signature
            )
            
            decky.logger.info(f"Signature verification: {valid}")
            return {
                "valid": valid,
                "key_id": sig_record.key_id,
                "algorithm": sig_record.algorithm
            }
        except Exception as e:
            decky.logger.error(f"Failed to verify signature: {e}")
            return {"valid": False, "error": str(e)}
