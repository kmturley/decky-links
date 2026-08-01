import os
import sys

# Bootstrap sys.path before any local package imports.
#
# The decky loader appends only <plugin>/py_modules to sys.path, not the plugin
# directory itself, so local packages (sources/, nfc/) need the plugin dir added
# explicitly. It goes first so the checked-out tree always wins over anything
# that may be left in py_modules/ — py_modules is for pip dependencies only.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_py_modules = os.path.join(_plugin_dir, "py_modules")
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
if _py_modules not in sys.path:
    sys.path.append(_py_modules)

import asyncio
import time
import json
import traceback
import subprocess
import threading
import re
from enum import Enum
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List

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
from sources.storage_source import StorageSource, DEFAULT_DRIVE_KINDS
from sources.camera_source import CameraSource
from sources.mqtt_source import MqttSource
from sources.serial_source import SerialSource
from sources.file_watch_source import FileWatchSource

import decky

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
    - IDLE → READY: any source became available
    - READY → CARD_PRESENT: media presented on any source
    - CARD_PRESENT → READY: last medium removed (no game running)
    - CARD_PRESENT → GAME_RUNNING: Game launched (auto_launch enabled)
    - GAME_RUNNING → READY: Game exited (via set_running_game)
    - GAME_RUNNING → READY: launching medium removed (after card_removed_during_game)
    - Any state → IDLE: every source became unavailable

    Key invariants:
    - Media is tracked per source (Plugin._active_media), not in one global slot
    - Only the medium that launched a game may quit it (Plugin._launch_origin)
    - No auto-relaunch: requires the medium to be physically re-presented
    - Game state is authoritative from frontend (Router.MainRunningApp)

    The names are NFC-flavoured for historical reasons; CARD_PRESENT means
    "some medium is loaded on some source", which includes a disk in a drive.
    """
    IDLE         = "IDLE"          # No source available to trigger anything
    READY        = "READY"         # At least one source up, no media, no game
    CARD_PRESENT = "CARD_PRESENT"  # Media loaded, URI parsed, awaiting launch decision
    GAME_RUNNING = "GAME_RUNNING"  # A game is running; its launching medium is locked


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
                    "enabled": True,
                    "device_path": self._get_default_device_path(),
                    "baudrate": 115200,
                    "polling_interval": 0.5,
                    "reader_type": "pn532_uart",
                },
                "storage": {
                    "enabled": True,
                    # Per-category switches, off unless the drive exists to be a
                    # trigger. A floppy drive on a Steam Deck is there on
                    # purpose; optical, USB and card readers are general storage
                    # holding the user's own data. Must stay in step with
                    # DEFAULT_DRIVE_KINDS in storage_source.py.
                    "drive_kinds": {
                        "floppy": True,
                        "optical": False,
                        "usb": False,
                        "flash": False,
                    },
                },
                "camera": {
                    "enabled": False,
                    "device": "/dev/video0",
                    "poll_interval": 1.0,
                },
                "mqtt": {
                    "enabled": False,
                    "broker_host": "localhost",
                    "broker_port": 1883,
                    "topic": "decky-links",
                    "secret": "",
                },
                "serial": {
                    "enabled": False,
                    "port": "/dev/ttyUSB1",
                    "baudrate": 9600,
                },
                "file_watch": {
                    "enabled": False,
                    "watch_dir": "",
                    "poll_interval": 2.0,
                },
            },
        }
        self.load()

    def _get_default_device_path(self):
        if sys.platform == "darwin":
            return "/dev/cu.usbserial-1440"
        # PN532 UART boards attach via CH340/CP2102/FTDI bridges, which enumerate
        # as ttyUSB*, not ttyACM*. NfcSource._find_serial_port() still globs both
        # as a fallback.
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

    def __init__(self):
        self.settings = None
        self.key_manager = None
        self.signature_manager = None
        self.nfc_source = None
        self.storage_source = None
        self.camera_source = None
        self.mqtt_source = None
        self.serial_source = None
        self.file_watch_source = None
        self.source_manager = None
        self.state = "IDLE"
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.running_game_id = None
        self.is_pairing = False
        self.pairing_uri = None
        self.pairing_source_id = None
        # source_id -> active medium. One entry per source; the authoritative
        # record of what is currently presented anywhere.
        self._active_media = {}
        # Which medium launched the running game, so only that medium can quit it.
        self._launch_origin = None
        self._pending_launch_origin = None

    # --- Lifecycle ---

    async def _main(self):
        # euid is the ground truth for the plugin.json "root" flag: it is fixed
        # when this process spawns, so a deploy without a loader restart leaves
        # it stale. Mounting storage media fails outright when this is not 0.
        decky.logger.info(
            f"Decky Links starting... (euid={os.geteuid()}, "
            f"{'root — storage mounts available' if os.geteuid() == 0 else 'unprivileged — storage mounts will fail'})"
        )
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
        self.is_pairing      = False
        self.pairing_uri     = None
        self.pairing_source_id = None
        self.running_game_id = None
        self.current_tag_uid = None
        self.current_tag_uri = None
        self._active_media   = {}
        self._launch_origin  = None
        self._pending_launch_origin = None
        # RPC call caching to reduce load with thread-safe lock
        self._tag_status_lock = threading.RLock()
        self._last_tag_status_query = 0
        self._tag_status_cache = None

        # --- Source-based architecture ---
        self._event_queue: asyncio.Queue[PluginEvent] = asyncio.Queue()
        self.nfc_source = NfcSource(
            settings=self.settings.get_source_settings("nfc"),
            key_manager=self.key_manager,
            signature_manager=self.signature_manager,
            logger=decky.logger,
        )
        self.storage_source = StorageSource(
            settings=self.settings.get_source_settings("storage"),
            logger=decky.logger,
        )
        self.camera_source = CameraSource(
            settings=self.settings.get_source_settings("camera"),
            logger=decky.logger,
        )
        self.mqtt_source = MqttSource(
            settings=self.settings.get_source_settings("mqtt"),
            logger=decky.logger,
        )
        self.serial_source = SerialSource(
            settings=self.settings.get_source_settings("serial"),
            logger=decky.logger,
        )
        self.file_watch_source = FileWatchSource(
            settings=self.settings.get_source_settings("file_watch"),
            logger=decky.logger,
        )
        self.source_manager = SourceManager(
            event_queue=self._event_queue,
            logger=decky.logger,
        )
        self.source_manager.register(self.nfc_source)
        self.source_manager.register(self.storage_source)
        self.source_manager.register(self.camera_source)
        self.source_manager.register(self.mqtt_source)
        self.source_manager.register(self.serial_source)
        self.source_manager.register(self.file_watch_source)

        await self.source_manager.start_all()
        self.polling_task = asyncio.create_task(self._event_loop())

    async def _unload(self):
        decky.logger.info("Decky Links unloading...")
        if hasattr(self, "polling_task"):
            self.polling_task.cancel()
        if hasattr(self, "source_manager"):
            await self.source_manager.stop_all()

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

    def _all_sources(self):
        """Every source this plugin owns.

        Prefers the manager's registry, falling back to the attributes so the
        helpers below still work before ``_main`` has registered anything.
        """
        if self.source_manager and self.source_manager.sources:
            return list(self.source_manager.sources)
        return [s for s in (
            self.nfc_source, self.storage_source, self.camera_source,
            self.mqtt_source, self.serial_source, self.file_watch_source,
        ) if s is not None]

    def _any_source_available(self, exclude: Optional[str] = None) -> bool:
        """True when at least one enabled source is currently usable.

        Drives the IDLE transition. IDLE means "nothing can trigger a launch",
        which is not the same as "the NFC reader is unplugged".

        ``exclude`` skips a source by id — used when handling that source's own
        DISCONNECTED event, so the answer never depends on how promptly the
        source got round to reporting itself inactive.
        """
        for source in self._all_sources():
            try:
                if source.source_id == exclude:
                    continue
                if source.is_enabled() and source.is_active():
                    return True
            except Exception:
                continue
        return False

    async def _handle_source_event(self, event: SourceEvent):
        """Handle hardware lifecycle events (connect/disconnect)."""
        is_nfc = event.source_type == SourceType.NFC

        if event.kind == SourceEventKind.CONNECTED:
            decky.logger.info(
                f"Source connected: {event.source_type.value} ({event.source_id})"
            )
            # Any working source means the plugin can be triggered — IDLE is
            # "nothing to trigger with", not "no NFC reader". With only a floppy
            # drive attached the machine used to sit in IDLE indefinitely.
            if self.state == PluginState.IDLE:
                self._set_state(PluginState.READY)
            if is_nfc:
                await decky.emit("reader_status", {
                    "connected": True,
                    "path": self.settings.get("device_path"),
                    "source_type": event.source_type.value,
                })

        elif event.kind == SourceEventKind.DISCONNECTED:
            decky.logger.info(
                f"Source disconnected: {event.source_type.value} ({event.source_id})"
            )
            # Hardware that has gone away cannot still be holding media. Drop
            # its registry entry, or the medium lingers as active forever and
            # keeps a stale claim on the running game.
            had_media = self._active_media.pop(event.source_id, None) is not None
            if self._launch_origin and self._launch_origin.get("source_id") == event.source_id:
                decky.logger.info(
                    f"Source {event.source_id} launched game {self.running_game_id} "
                    f"but has disconnected; dropping its claim."
                )
                self._launch_origin = None

            if is_nfc:
                await decky.emit("reader_status", {
                    "connected": False,
                    "path": self.settings.get("device_path"),
                    "source_type": event.source_type.value,
                })
                # If a tag was present when the reader disconnected, clear it
                # so the frontend doesn't keep showing the old tag as active.
                if self.current_tag_uid:
                    self.current_tag_uid = None
                    self.current_tag_uri = None
                    self.current_tag_meta = None
                    had_media = True

            # Unplugging a drive with a disk in it must clear the panel the same
            # way ejecting the disk would; otherwise it keeps showing media that
            # is no longer attached to anything.
            if had_media:
                await decky.emit("tag_removed", {
                    "source_type": event.source_type.value,
                    "source_id": event.source_id,
                })

            # IDLE only when nothing at all is left to trigger with. Losing the
            # NFC reader while a floppy drive is still connected is not idle.
            if not self._any_source_available(exclude=event.source_id):
                self._set_state(PluginState.IDLE)
            elif self.state == PluginState.CARD_PRESENT and not self._active_media:
                self._set_state(PluginState.READY)

        statuses = await self.get_source_statuses()
        await decky.emit("source_statuses", statuses)

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
        is_nfc = event.source_type == SourceType.NFC

        # Collision check (Spec §6.2) — scoped to *this* source. Comparing
        # against a single global slot meant a floppy insert looked like an NFC
        # tag collision, and vice versa. Only one medium can occupy one source.
        previous = self._active_media.get(event.source_id)
        if previous and previous["media_id"] != uid_hex:
            decky.logger.info(
                f"Multiple media on {event.source_id}: "
                f"{previous['media_id']}, {uid_hex}"
            )
            await decky.emit("multiple_tags", {
                "previous": previous["media_id"],
                "current":  uid_hex,
                "source_type": event.source_type.value,
            })

        # Registry is the source of truth for which media each source holds.
        # A re-emitted LOAD for the *same* medium keeps the category we already
        # know: the panel matches a medium to its row by drive_kind, so a
        # partial payload must never downgrade it to None.
        prior_kind = (
            previous.get("drive_kind")
            if previous and previous["media_id"] == uid_hex
            else None
        )
        self._active_media[event.source_id] = {
            "source_id":   event.source_id,
            "source_type": event.source_type.value,
            "media_id":    uid_hex,
            "uri":         uri,
            "drive_kind":  event.payload.get("drive_kind") or prior_kind,
            "meta":        event.payload.get("tag_meta") if is_nfc else None,
        }

        # current_tag_* remain the NFC-specific view, kept for the existing RPC
        # and UI contract. Non-NFC sources must not clobber them — that is what
        # made a QR code leaving frame clear the tag shown in the panel.
        if is_nfc:
            self.current_tag_uid = uid_hex
            self.current_tag_uri = uri
            self.current_tag_meta = event.payload.get("tag_meta")

        self._set_state(PluginState.CARD_PRESENT)

        # Audio feedback (Spec §11)
        self._play_sound("scan.flac")

        # Pairing must be handled BEFORE any URI inspection (Spec §7).
        # A blank tag — the normal case when pairing a new card — carries no URI,
        # so deferring this until after the `if not uri: return` guard below made
        # pairing impossible for exactly the tags users want to pair.
        armed_for_this_source = (
            not self.pairing_source_id or self.pairing_source_id == event.source_id
        )
        if (
            self.is_pairing
            and armed_for_this_source
            and self._pairable_source(event.source_id) is not None
        ):
            await decky.emit("tag_detected", {
                "uid": uid_hex,
                "source_type": event.source_type.value,
                "source_id": event.source_id,
                "drive_kind": event.payload.get("drive_kind"),
            })
            await self._handle_pairing(uid_hex, source_id=event.source_id)
            return

        # Emit tag_detected immediately — matches old _handle_scan behavior where
        # the UID appeared in the UI as soon as the card was read.
        await decky.emit("tag_detected", {
            "uid": uid_hex,
            "source_type": event.source_type.value,
            "source_id": event.source_id,
            "drive_kind": event.payload.get("drive_kind"),
        })

        # Emit NDEF records if available (NFC-specific)
        if "ndef_records" in event.payload:
            await decky.emit("ndef_detected", {"records": event.payload["ndef_records"]})

        # Emit tag metadata if available
        if event.source_type == SourceType.NFC and self.current_tag_meta:
            await decky.emit("tag_metadata", self.current_tag_meta)

        # No URI — play error sound and emit null so frontend clears any stale URI
        if not uri:
            # Distinguish "blank, ready to pair" from "we could not read this at
            # all". Both produce no URI, but only one of them is the user's
            # problem to fix, and a disk that says nothing is indistinguishable
            # from a broken plugin.
            unreadable = bool(event.payload.get("unreadable"))
            decky.logger.info(
                f"{'Unreadable media' if unreadable else 'No URI found on media'} {uid_hex}"
            )
            self._play_sound("error.flac")
            if is_nfc:
                self.current_tag_uri = None
            await decky.emit("uri_detected", {
                "uri":   None,
                "uid":   uid_hex,
                "blank": not unreadable,
                "unreadable": unreadable,
                "error": event.payload.get("error"),
            })
            self._set_state(PluginState.READY)
            return

        # Allowlist check (Spec §4) — emit null URI so frontend knows it's blocked
        if not self._validate_uri(uri):
            decky.logger.warning(f"URI blocked by allowlist: {uri}")
            self._play_sound("error.flac")
            self.current_tag_uri = None
            await decky.emit("uri_detected", {"uri": None, "uid": uid_hex, "blocked": True})
            self._set_state(PluginState.READY)
            return

        decky.logger.info(f"URI found on media {uid_hex}: {uri}")

        # Decide whether this medium is about to cause a launch, and claim
        # credit for it, BEFORE emitting uri_detected.
        #
        # The frontend launches Steam URIs in response to that event and calls
        # set_running_game() as soon as RunGame returns. If the origin were
        # recorded after the emit, that call could arrive first, find no pending
        # origin, and attribute the game to nothing — after which removing the
        # tag would silently fail to quit it. Ordering is the whole fix here.
        will_launch = bool(self.settings.get("auto_launch")) and not self.running_game_id
        if will_launch:
            self._pending_launch_origin = {
                "source_id": event.source_id,
                "media_id":  uid_hex,
            }

        # Emit valid URI once — matches old code where uri_detected fired only with final URI
        await decky.emit("uri_detected", {"uri": uri, "uid": uid_hex})

        # (Pairing is handled at the top of this method, before URI inspection —
        # see the comment there for why it cannot live down here.)

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
        is_nfc = event.source_type == SourceType.NFC

        self._active_media.pop(event.source_id, None)

        # Only the medium that launched the running game may quit it. Without
        # this, ejecting a floppy or moving a QR code out of frame would quit a
        # game that was started by tapping an NFC tag.
        origin = self._launch_origin
        launched_this_game = (
            origin is not None
            and origin.get("source_id") == event.source_id
            and origin.get("media_id") == removed_uid
        )

        # Spec §6.3: removal during active game → notify frontend
        if (
            self.state == PluginState.GAME_RUNNING
            and not self.is_pairing
            and launched_this_game
        ):
            # The decision belongs here, next to the launch decision — the
            # backend is the only side that knows which medium started this
            # game. The frontend owns the mechanism (only it has SteamClient),
            # so it is told what to do rather than asked to work it out.
            action = "close" if self.settings.get("auto_close") else "pause"
            decky.logger.info(
                f"Media removed while game {self.running_game_id} active; "
                f"action={action}."
            )
            await decky.emit("card_removed_during_game", {
                "appid": self.running_game_id,
                "uid":   removed_uid,
                "uri":   removed_uri,
                "source_type": event.source_type.value,
                "action": action,
            })
        elif self.state == PluginState.GAME_RUNNING and not self.is_pairing:
            decky.logger.info(
                f"Media {removed_uid} removed from {event.source_id} while game "
                f"{self.running_game_id} is running, but that game was launched by "
                f"{origin} — leaving it alone."
            )
        else:
            decky.logger.info(
                f"Media removed. State={self.state.value}, Pairing={self.is_pairing}"
            )

        # Clear only the view belonging to this source — a storage eject must
        # not blank the NFC tag the panel is showing.
        if is_nfc:
            self.current_tag_uid = None
            self.current_tag_uri = None
            self.current_tag_meta = None
        await decky.emit("tag_removed", {
            "source_type": event.source_type.value,
            "source_id": event.source_id,
        })

        # Spec §6.6: card removed while READY → state stays READY.
        # Stay in CARD_PRESENT if another source still holds media.
        if self.state not in (PluginState.GAME_RUNNING, PluginState.IDLE):
            self._set_state(
                PluginState.CARD_PRESENT if self._active_media else PluginState.READY
            )

    # ── URI Validation ─────────────────────────────────────────────────

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

    # ── Pairing Handler ────────────────────────────────────────────────

    def _pairable_source(self, source_id: Optional[str]):
        """Return the registered source with this id, if it can be written to.

        Pairing is no longer NFC-only: a blank floppy is written by asking its
        own source to persist the URI, exactly as a blank tag is.
        """
        if not source_id:
            return None
        for source in self._all_sources():
            if source.source_id == source_id and source.can_write():
                return source
        return None

    async def _handle_pairing(self, media_id: str, source_id: Optional[str] = None):
        """Write the pairing URI onto the presented medium (Spec §7)."""
        if not self.pairing_uri:
            decky.logger.warning("Pairing triggered but no URI set!")
            self.is_pairing = False
            self.pairing_uri = None
            self.pairing_source_id = None
            return

        source = self._pairable_source(source_id)
        if source is None:
            decky.logger.warning(
                f"Pairing triggered for {source_id} which cannot be written to"
            )
            self.is_pairing = False
            self.pairing_uri = None
            self.pairing_source_id = None
            await decky.emit("pairing_result", {
                "success": False,
                "uid":     media_id,
                "error":   "This trigger source cannot be paired",
            })
            return

        # Atomic state update: exit pairing mode immediately to prevent
        # new media from interfering with the write operation
        pairing_uri = self.pairing_uri
        self.is_pairing = False
        self.pairing_uri = None
        self.pairing_source_id = None

        decky.logger.info(f"Pairing: writing {pairing_uri} to {media_id} via {source_id}")
        try:
            success, error_msg = await source.write_uri(media_id, pairing_uri)
            self._play_sound("success.flac" if success else "error.flac")

            if success:
                # The medium now holds this URI, but nothing will re-read it
                # while it stays put — poll() only reports media on arrival.
                # Without this the panel shows "Url: Empty" until the user
                # lifts and replaces the card, even though pairing succeeded.
                await self._sync_uri_after_pairing(media_id, pairing_uri, source_id)

            await decky.emit("pairing_result", {
                "success": success,
                "uid":     media_id,
                "error":   error_msg,
                "source_type": source.source_type.value,
            })
        except Exception as e:
            decky.logger.error(f"Critical error in pairing handler: {e}")
            await decky.emit("pairing_result", {
                "success": False,
                "uid":     media_id,
                "error":   str(e),
            })

    async def _sync_uri_after_pairing(self, media_id: str, uri: str, source_id: Optional[str]):
        """Reflect a freshly-written URI in plugin state and tell the frontend.

        Emits ``uri_detected`` with ``paired: True``. The flag matters: the
        frontend launches on that event, and pairing a card must not also start
        the game — it would yank the user out of whatever they were doing right
        after they pressed a button that only promised to write a tag.
        """
        source = self._pairable_source(source_id)
        is_nfc = source is not None and source.source_type == SourceType.NFC

        # Keep the registry and the source's own view consistent, so the RPC
        # poll fallback and any later reads agree with what the panel shows.
        entry = self._active_media.get(source_id) if source_id else None
        if entry is None and is_nfc:
            entry = next(
                (m for m in self._active_media.values() if m.get("source_type") == "nfc"),
                None,
            )
        if entry is not None:
            entry["uri"] = uri

        # current_tag_* is the NFC-specific view behind get_tag_status; pairing
        # a floppy must not overwrite what the reader is holding.
        if is_nfc:
            self.current_tag_uri = uri
            if self.nfc_source is not None:
                self.nfc_source.current_tag_uri = uri

        decky.logger.info(f"Pairing wrote {uri} to {media_id}; syncing UI state")
        # source_id/source_type let the panel address the medium directly
        # instead of guessing from media_id, which matters once more than one
        # trigger is holding media at the same time.
        await decky.emit("uri_detected", {
            "uri":    uri,
            "uid":    media_id,
            "paired": True,
            "source_id": source_id or (entry or {}).get("source_id"),
            "source_type": (entry or {}).get("source_type"),
        })

    # ── Launch ─────────────────────────────────────────────────────────

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
            # The decky CLI zips a fixed allowlist (main.py, plugin.json,
            # package.json, dist/, py_modules/, LICENSE, README.md) — a
            # top-level assets/ is dropped, so the build vendors the sounds
            # into py_modules/. Check the source-tree location first so a
            # development checkout still works.
            candidates = [
                os.path.join(decky.DECKY_PLUGIN_DIR, "assets", "sounds", filename),
                os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules", "assets", "sounds", filename),
            ]
            sound_path = next((p for p in candidates if os.path.exists(p)), None)

            if sound_path is None:
                decky.logger.error(
                    f"Sound file not found: {filename} (looked in {', '.join(candidates)})"
                )
                return
            
            # Verify it's a regular file (not a directory or symlink to sensitive location)
            if not os.path.isfile(sound_path):
                decky.logger.error(f"Sound path is not a regular file: {sound_path}")
                return
            
            subprocess.Popen(["paplay", sound_path], env=self._audio_env())
        except Exception as e:
            decky.logger.error(f"Failed to play sound {filename}: {e}")

    def _audio_env(self):
        """Environment that lets paplay reach the desktop user's audio server.

        The plugin runs as root (needed to mount disks), which puts it outside
        the user's PipeWire session — paplay would find no server and every
        sound would silently vanish. Point it at the session socket explicitly;
        root can open it regardless of its ownership.
        """
        env = dict(os.environ)
        if os.geteuid() != 0:
            return env
        try:
            import pwd
            user = getattr(decky, "DECKY_USER", None) or "deck"
            uid = pwd.getpwnam(user).pw_uid
        except (KeyError, ImportError):
            return env
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault("PULSE_SERVER", f"unix:/run/user/{uid}/pulse/native")
        return env

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
        if key in ("device_path", "baudrate", "reader_type") and self.nfc_source:
            self.nfc_source._reader = None  # force reconnect on next poll
        return True

    async def start_pairing(self, uri, source_id: Optional[str] = None):
        """Arm pairing. With `source_id`, only that trigger may be written.

        The panel offers a Pair button per trigger, so it says which one it
        means. Without a target, any writable source wins — the game-page
        link button arms everything and lets the user choose by presenting a
        medium.
        """
        if not self._validate_uri(uri):
            decky.logger.warning(f"Pairing URI rejected by allowlist: {uri}")
            return False
        if source_id and self._pairable_source(source_id) is None:
            decky.logger.warning(f"Pairing target {source_id!r} cannot be written to")
            return False
        decky.logger.info(
            f"UI requested pairing for URI: {uri}"
            + (f" on {source_id}" if source_id else " on any trigger")
        )
        self.is_pairing  = True
        self.pairing_uri = uri
        self.pairing_source_id = source_id
        # Re-arm every source so media already in place — a card resting on the
        # reader, a disk already in the drive — is picked up on the next poll,
        # instead of requiring the user to remove and re-present it.
        for source in self._all_sources():
            try:
                source.rearm()
            except Exception as e:
                decky.logger.warning(f"rearm failed for {source.source_id}: {e}")
        return True

    async def cancel_pairing(self):
        self.is_pairing  = False
        self.pairing_uri = None
        self.pairing_source_id = None
        return True

    async def get_reader_status(self):
        # Polled twice a second by the frontend, and reached before _main()
        # finishes (or at all, if _main raised), so it must never throw —
        # an exception here becomes a steady stream of RPC errors.
        reader = self.nfc_source.reader if self.nfc_source else None
        return {
            "connected": reader is not None,
            "path":      self.settings.get("device_path") if self.settings else None,
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
        # Classification needs a live reader; without one this is still a useful
        # debug helper, just without metadata.
        if uid and self.nfc_source and self.nfc_source.reader:
            try:
                self.current_tag_meta = self.nfc_source._classify_tag(uid)
            except Exception as e:
                decky.logger.warning(f"simulate_tag: classification failed: {e}")
                self.current_tag_meta = None
        else:
            self.current_tag_meta = None
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
            return self.nfc_source._classify_tag(uid_bytes)
        except Exception as e:
            return {"error": str(e)}

    async def get_reader_diagnostics(self):
        """Return low-level diagnostics about the connected reader."""
        reader = self.nfc_source.reader if self.nfc_source else None
        info = {"connected": reader is not None}
        if reader:
            try:
                info["firmware"] = reader.firmware_version()
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
            meta = self.nfc_source._classify_tag(uid_bytes)
            if meta.get("type") != "mifare-classic":
                decky.logger.warning(f"Sector info only supported for Mifare Classic, got {meta.get('type')}")
                return []

            # Create handler and get sector info
            from nfc.tag_handlers import MifareClassicHandler
            handler = MifareClassicHandler(uid_bytes, self.key_manager)

            reader = self.nfc_source.reader if self.nfc_source else None
            if not reader:
                decky.logger.error("No reader available for sector info")
                return []

            return handler.get_sector_info(reader)
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
            meta = self.nfc_source._classify_tag(uid_bytes)
            if meta.get("type") != "mifare-classic":
                decky.logger.warning(f"Sector locking only supported for Mifare Classic")
                return False

            capacity = meta.get("capacity_bytes", 0)
            max_sectors = 40 if capacity > 2048 else 16

            if sector < 0 or sector >= max_sectors:
                decky.logger.warning(f"Invalid sector {sector} for {capacity}-byte tag (max {max_sectors - 1})")
                return False

            reader = self.nfc_source.reader if self.nfc_source else None
            if not reader:
                decky.logger.error("No reader available for sector lock")
                return False

            # Create handler and lock sector
            from nfc.tag_handlers import MifareClassicHandler
            handler = MifareClassicHandler(uid_bytes, self.key_manager)

            success, error = handler.lock_sector(reader, sector, key_a_bytes, key_b_bytes)
            
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
            # Attribute the game to whichever medium triggered the launch, so
            # _handle_media_unload only quits it for that medium. A launch the
            # user started by hand has no pending origin and is attributed to
            # nothing, which correctly means no medium can quit it.
            self._launch_origin = self._pending_launch_origin
            self._pending_launch_origin = None
            if self._launch_origin:
                decky.logger.info(f"Game {appid} attributed to {self._launch_origin}")
            self._set_state(PluginState.GAME_RUNNING)
        else:
            self._launch_origin = None
            self._pending_launch_origin = None
            if self.state == PluginState.GAME_RUNNING:
                # Spec §6.4: game exited — return to CARD_PRESENT when media is
                # still presented somewhere, otherwise READY.
                self._set_state(
                    PluginState.CARD_PRESENT if self._active_media else PluginState.READY
                )

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
            
            # Without cryptography, verification is impossible and the manager
            # fails closed (always False). Distinguish that from a genuine
            # mismatch so the UI can say "can't check" rather than "forged".
            unavailable = not getattr(self.signature_manager, "crypto_available", False)
            if unavailable:
                decky.logger.error(
                    "Signature could not be verified: cryptography is unavailable. "
                    "Reporting invalid rather than assuming authenticity."
                )

            decky.logger.info(f"Signature verification: {valid}")
            return {
                "valid": valid,
                "key_id": sig_record.key_id,
                "algorithm": sig_record.algorithm,
                "unverifiable": unavailable,
            }
        except Exception as e:
            decky.logger.error(f"Failed to verify signature: {e}")
            return {"valid": False, "error": str(e)}

    async def get_active_media(self):
        """Return every medium currently presented, across all sources.

        The per-source view that `get_tag_status` cannot express: that RPC
        reports the NFC slot only, for backwards compatibility.
        """
        return list(self._active_media.values())

    async def get_launch_origin(self):
        """Return the medium credited with launching the running game, if any."""
        return self._launch_origin

    async def get_source_statuses(self):
        """Return status for all registered sources."""
        if not self.source_manager:
            return []
        result = []
        for source in self.source_manager.sources:
            entry = {
                "source_id": source.source_id,
                "source_type": source.source_type.value,
                # The row tracks the hardware, not the media: ejecting a floppy
                # does not unplug the drive, and greying the whole source out
                # the moment a disk comes out reads as a fault.
                "active": source.has_drive(),
                "has_media": source.has_media(),
                # Whether media on this source can be written to. The game-page
                # link button uses this to decide if pairing is possible at all,
                # instead of asking specifically about the NFC reader.
                "can_pair": source.can_write(),
                "enabled": source.is_enabled(),
            }
            # Storage is one source covering several kinds of drive, and the
            # panel shows a row per kind — so it needs presence per kind, not
            # just "some drive is attached".
            if hasattr(source, "drive_kinds_present"):
                settings = self.settings.get_source_settings("storage") or {}
                configured = settings.get("drive_kinds") or {}
                entry["drive_kinds"] = {
                    kind: {
                        "present": present,
                        "enabled": bool(configured.get(kind, DEFAULT_DRIVE_KINDS.get(kind, False))),
                    }
                    for kind, present in source.drive_kinds_present().items()
                }
            result.append(entry)
        return result

    async def set_source_setting(self, source_type: str, key: str, value):
        """Update a per-source setting."""
        # storage and camera joined the list when sources gained an explicit
        # on/off switch — a disabled source idles instead of retrying forever,
        # so the switch has to be reachable from the panel.
        ALLOWED_SOURCE_TYPES = {"nfc", "mqtt", "serial", "file_watch", "storage", "camera"}
        if source_type not in ALLOWED_SOURCE_TYPES:
            decky.logger.warning(f"set_source_setting: unknown source_type {source_type!r}")
            return False

        ALLOWED_KEYS: Dict[str, type] = {
            "enabled": bool,
            "drive_kinds": dict,
            "broker_host": str,
            "broker_port": int,
            "topic": str,
            "secret": str,
            "port": str,
            "baudrate": int,
            "watch_dir": str,
            "poll_interval": float,
        }

        if key not in ALLOWED_KEYS:
            decky.logger.warning(f"set_source_setting: unknown key {key!r}")
            return False

        expected_type = ALLOWED_KEYS[key]
        if not isinstance(value, expected_type):
            # Allow int where float is expected
            if expected_type is float and isinstance(value, int):
                value = float(value)
            else:
                decky.logger.warning(
                    f"set_source_setting: {key!r} expects {expected_type.__name__}, got {type(value).__name__}"
                )
                return False

        sources = self.settings.settings.setdefault("sources", {})
        sources.setdefault(source_type, {})[key] = value
        self.settings.save()
        decky.logger.info(f"Source setting updated: {source_type}.{key} = {value!r}")
        return True
