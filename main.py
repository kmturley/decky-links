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
import ipaddress
import json
import traceback
import subprocess
import threading
import re
import secrets
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
    build_all,
)

import decky

from cards import PRINT_DPI
from decky_links import settings_schema
from decky_links import uri as uri_rules
from decky_links import card_rpcs
from decky_links import nfc_rpcs
from decky_links.media_registry import MediaRegistry
from decky_links.settings import SettingsManager
from nfc.key_manager import KeyManager


# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

# Re-exported for existing references; defined in decky_links.uri next to the
# rules that use them.
ALLOWED_STEAM_URI_PREFIXES = uri_rules.ALLOWED_STEAM_URI_PREFIXES
ALLOWED_URI_SCHEMES = uri_rules.ALLOWED_URI_SCHEMES
STEAM_APPID_PATTERN = uri_rules.STEAM_APPID_PATTERN

# How often the event loop wakes with no event to re-check source status.
# Drives appear and disappear without producing a media event, and the storage
# source never disconnects, so a change would otherwise never reach the panel.
# Only differences are emitted, so an idle plugin sends nothing.
STATUS_TICK_SECONDS = 2.0

# Derived from settings_schema rather than restated here. They were duplicated
# in three places and the copies had already drifted — see that module.
TOP_LEVEL_SETTING_KEYS = settings_schema.TOP_LEVEL_SETTING_KEYS
NFC_SETTING_KEYS = settings_schema.NFC_SETTING_KEYS
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
# Plugin
# -----------------------------------------------------------------------

class Plugin:

    def __init__(self):
        self.settings = None
        self.key_manager = None
        self.source_manager = None
        self.state = "IDLE"
        self.current_tag_uid = None
        self.current_tag_uri = None
        self.running_game_id = None
        self.is_pairing = False
        self.pairing_uri = None
        self.pairing_source_id = None
        # What is presented where, and which medium started the running game.
        # See decky_links.media_registry for the invariants.
        self._registry = MediaRegistry()
        self._last_statuses = None

    # --- Lifecycle ---

    def _log_runtime(self):
        """Report the interpreter this plugin actually runs under, and whether
        each compiled dependency loaded.

        Decky Loader is a frozen binary carrying its own Python, which is *not*
        the SteamOS `python3` that `deck:status` reports. When they differ,
        every version-tagged extension we vendor is built for the wrong one —
        and it fails at import with a message that names a missing symbol
        rather than a version, which is how `undefined symbol:
        PyObject_GetTypeData` (a Python 3.12 addition) turned out to mean "this
        interpreter is older than 3.12".

        Cheap, once, at startup, and it turns that class of bug into one line.
        """
        v = sys.version_info
        decky.logger.info(
            f"Python runtime: {v.major}.{v.minor}.{v.micro} "
            f"(build this plugin with DECK_PYTHON={v.major}.{v.minor}) "
            f"executable={sys.executable}"
        )
        # Pure-Python dependencies import under any version and prove nothing;
        # only the compiled ones can be wrong.
        # Each is the module the code actually imports, not a proxy for it:
        # cryptography.hazmat.backends loaded on 3.11 while cryptography.fernet
        # — what KeyManager uses — did not, because fernet pulls in _cffi_backend,
        # a version-tagged extension. Probing the wrong one reported healthy
        # while keys were silently being stored unencrypted.
        for module in ("cryptography.fernet", "PIL.Image", "zxingcpp"):
            try:
                __import__(module)
                decky.logger.info(f"  compiled dep OK   {module}")
            except Exception as e:
                decky.logger.warning(
                    f"  compiled dep FAIL {module}: {type(e).__name__}: {e}"
                )

    async def _main(self):
        # euid is the ground truth for the plugin.json "root" flag: it is fixed
        # when this process spawns, so a deploy without a loader restart leaves
        # it stale. Mounting storage media fails outright when this is not 0.
        decky.logger.info(
            f"Decky Links starting... (euid={os.geteuid()}, "
            f"{'root — storage mounts available' if os.geteuid() == 0 else 'unprivileged — storage mounts will fail'})"
        )
        self._log_runtime()
        self.settings = SettingsManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"),
            logger=decky.logger,
        )
        self.key_manager = KeyManager(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "keys.json"),
            logger=decky.logger
        )
        self.state           = PluginState.IDLE
        self.is_pairing      = False
        self.pairing_uri     = None
        self.pairing_source_id = None
        self.running_game_id = None
        self.current_tag_uid = None
        self.current_tag_uri = None
        self._registry.reset()
        # RPC call caching to reduce load with thread-safe lock
        self._tag_status_lock = threading.RLock()
        self._last_tag_status_query = 0
        self._tag_status_cache = None

        # --- Source-based architecture ---
        self._event_queue: asyncio.Queue[PluginEvent] = asyncio.Queue()
        self.source_manager = SourceManager(
            event_queue=self._event_queue,
            logger=decky.logger,
        )
        # One entry per source lives in sources.source_classes(); key_manager
        # reaches only the sources whose constructor declares it.
        for source in build_all(
            self.settings.get_source_settings,
            logger=decky.logger,
            key_manager=self.key_manager,
        ):
            self.source_manager.register(source)

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

        The wait has a timeout so statuses are re-checked even when no event
        arrives. Plugging in a floppy *drive* produces no media event — there
        is no disk in it yet — and the storage source never disconnects, so
        without this the panel's view of which drives are attached was frozen
        at whatever it was when the source first connected. That is why a
        connected drive kept reading "Not connected".
        """
        while True:
            try:
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(), timeout=STATUS_TICK_SECONDS
                    )
                except asyncio.TimeoutError:
                    await self._publish_statuses()
                    continue

                if isinstance(event, SourceEvent):
                    await self._handle_source_event(event)
                elif isinstance(event, MediaEvent):
                    await self._handle_media_event(event)
                await self._publish_statuses()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"Event loop error: {e}")
                decky.logger.error(traceback.format_exc())

    async def _publish_statuses(self, force: bool = False):
        """Push source statuses to the panel, but only when they changed.

        Called after every event and on every idle tick, so it has to be
        silent when nothing moved — an unconditional emit here would be a
        message per second forever.
        """
        statuses = await self.get_source_statuses()
        if not force and statuses == self._last_statuses:
            return
        self._last_statuses = statuses
        await decky.emit("source_statuses", statuses)

    def _all_sources(self):
        """Every source this plugin owns.

        The manager's registry is the only record now. It used to be that plus
        six named attributes holding the same objects, with this method
        reconciling them — so registering a source and remembering to also
        assign it were two things that could disagree.
        """
        if not self.source_manager:
            return []
        return list(self.source_manager.sources)

    def _source_of_type(self, source_type: SourceType):
        """The registered source of a given kind, or None.

        A handful of RPCs are genuinely reader-specific — Mifare keys, sector
        locking, firmware diagnostics — and need to address the NFC source
        directly. They ask by type rather than holding a reference, so there is
        still only one record of what exists.
        """
        for source in self._all_sources():
            if source.source_type == source_type:
                return source
        return None

    @property
    def nfc_source(self):
        """The NFC reader, for the reader-specific RPCs. May be None."""
        return self._source_of_type(SourceType.NFC)

    @property
    def storage_source(self):
        """The storage source, for the drive-category rescan. May be None."""
        return self._source_of_type(SourceType.STORAGE)

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
            had_media = self._registry.remove(event.source_id)
            if self._registry.launch_origin and self._registry.launch_origin.get("source_id") == event.source_id:
                decky.logger.info(
                    f"Source {event.source_id} launched game {self.running_game_id} "
                    f"but has disconnected; dropping its claim."
                )
                self._registry.drop_origin_for_source(event.source_id)

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
            elif self.state == PluginState.CARD_PRESENT and not self._registry.any_present():
                self._set_state(PluginState.READY)

        await self._publish_statuses()

    async def _handle_media_event(self, event: MediaEvent):
        """Handle media interaction events (tag tap, floppy insert, etc.)."""
        if event.kind == MediaEventKind.LOAD:
            await self._handle_media_load(event)
        elif event.kind == MediaEventKind.UNLOAD:
            await self._handle_media_unload(event)
        elif event.kind == MediaEventKind.LOADING:
            await self._handle_media_loading(event)

    async def _handle_media_loading(self, event: MediaEvent):
        """Announce a medium that is present but not yet readable.

        Deliberately does not touch the media registry or the plugin state: the
        medium may still turn out to be unreadable, and a half-entry would let
        the rest of the plugin treat it as pairable. It is purely something for
        the panel to show while a slow read is in progress, and is always
        superseded by a LOAD.
        """
        decky.logger.info(f"Media loading on {event.source_id}: {event.media_id}")
        await decky.emit("media_loading", {
            "source_id":   event.source_id,
            "source_type": event.source_type.value,
            "media_id":    event.media_id,
            "drive_kind":  event.payload.get("drive_kind"),
        })

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
        previous = self._registry.get(event.source_id)
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
        self._registry.put(event.source_id, {
            "source_id":   event.source_id,
            "source_type": event.source_type.value,
            "media_id":    uid_hex,
            "uri":         uri,
            "drive_kind":  event.payload.get("drive_kind") or prior_kind,
            "meta":        event.payload.get("tag_meta") if is_nfc else None,
        })

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
            self._registry.claim_launch(event.source_id, uid_hex)

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

        self._registry.remove(event.source_id)

        # Only the medium that launched the running game may quit it. Without
        # this, ejecting a floppy or moving a QR code out of frame would quit a
        # game that was started by tapping an NFC tag.
        origin = self._registry.launch_origin
        launched_this_game = self._registry.launched_by(event.source_id, removed_uid)

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
                PluginState.CARD_PRESENT if self._registry.any_present() else PluginState.READY
            )

    # ── URI Validation ─────────────────────────────────────────────────

    def _validate_uri(self, uri: str) -> bool:
        """Whether this URI may be acted on. The rules live in decky_links.uri.

        They moved because this is the plugin's trust boundary and had no
        business needing a Plugin instance — with settings, a key manager and
        six sources — to evaluate a string. This wrapper stays to log the
        reason, which is what tells a blocked card apart from a broken reader.
        """
        ok, reason = uri_rules.validate(uri)
        if not ok:
            decky.logger.warning(f"URI rejected: {reason} ({uri!r})")
        return ok

    def _validate_setting(self, key, value) -> bool:
        """Thin adapter over settings_schema.

        This was a near-verbatim copy of SettingsManager's version, carrying a
        comment that said so. Both now defer to the same table.
        """
        ok, _reason = settings_schema.validate(key, value)
        return ok

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
        entry = self._registry.get(source_id) if source_id else None
        if entry is None and is_nfc:
            entry = self._registry.first_of_type("nfc")
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
            self._spawn(["xdg-open", uri])
        except Exception as e:
            decky.logger.error(f"Launch failed: {e}")

    # --- Child processes ---

    def _spawn(self, argv, env=None):
        """Start a fire-and-forget child, reaping any that have finished.

        Nothing ever waited on these, so each one stayed a zombie in the
        process table for the life of the plugin — and _play_sound runs on
        every single scan, so it accumulated one per tap indefinitely.

        We do not want to block on them either: the point is that a sound or a
        launch happens alongside the plugin, not in front of it. So sweep the
        previous ones on the way past, which is enough because spawning is the
        only thing that creates the debt.
        """
        self._children = [p for p in getattr(self, "_children", []) if p.poll() is None]
        # start_new_session detaches the child from our process group, so a
        # signal sent to the plugin does not also kill the game the user just
        # launched.
        proc = subprocess.Popen(argv, shell=False, env=env, start_new_session=True)
        self._children.append(proc)
        return proc

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
            
            self._spawn(["paplay", sound_path], env=self._audio_env())
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
        """Update a top-level or NFC setting. False means it did not stick.

        Returning True unconditionally meant a settings file that could not be
        written — no permission, disk full — still reported success, so the
        panel showed the new value and the old one came back on restart.
        """
        ok, reason = settings_schema.validate(key, value)
        if not ok:
            decky.logger.warning(f"Rejected setting update: {reason} (got {value!r})")
            return False
        value = settings_schema.coerce(key, value)

        if not self.settings.set(key, value):
            decky.logger.error(f"Setting {key!r} could not be written to disk")
            return False
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

    # --- Mifare Classic keys and sectors ---
    #
    # Thin delegations: these need a key manager and a reader, not the state
    # machine or the media registry, so the bodies live in
    # decky_links.nfc_rpcs. The names here are the RPC surface.

    async def set_tag_key(self, uid: str, key_a: str, key_b: str):
        return await nfc_rpcs.set_tag_key(decky, self.key_manager, uid, key_a, key_b)

    async def get_tag_key(self, uid: str):
        return await nfc_rpcs.get_tag_key(decky, self.key_manager, uid)

    async def list_tag_keys(self):
        return await nfc_rpcs.list_tag_keys(decky, self.key_manager)

    async def get_sector_info(self, uid: Optional[str] = None):
        return await nfc_rpcs.get_sector_info(
            decky, self.key_manager, self.nfc_source, uid,
            current_uid=self.current_tag_uid,
        )

    async def lock_sector(self, uid: str, sector: int, key_a: str, key_b: str):
        return await nfc_rpcs.lock_sector(
            decky, self.key_manager, self.nfc_source, uid, sector, key_a, key_b
        )

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
            #
            # Only a *new* origin, or a genuinely different game, may change the
            # attribution. The frontend reports the running game repeatedly —
            # "Running game updated: 400 → 400" a second after the launch — and
            # unconditionally taking the (now empty) pending origin wiped the
            # attribution immediately after setting it. Auto-close then refused
            # to quit anything, because every game had been "launched by None".
            self._registry.confirm_launch(appid, prev)
            if self._registry.launch_origin:
                decky.logger.info(
                    f"Game {appid} attributed to {self._registry.launch_origin}"
                )
            self._set_state(PluginState.GAME_RUNNING)
        else:
            self._registry.clear_launch()
            if self.state == PluginState.GAME_RUNNING:
                # Spec §6.4: game exited — return to CARD_PRESENT when media is
                # still presented somewhere, otherwise READY.
                self._set_state(
                    PluginState.CARD_PRESENT if self._registry.any_present() else PluginState.READY
                )

        return True

    # --- Printable cards ---
    #
    # Thin delegations: the rendering has nothing to do with the state
    # machine, the sources or the reader, so it lives in decky_links.card_rpcs
    # as plain functions. These stay because the names are the RPC surface the
    # frontend calls.

    def _card_output_dir(self) -> str:
        return card_rpcs.output_dir(decky)

    def _card_owner(self):
        return card_rpcs.owner(decky)

    async def get_qr_preview(self, uri: str, module_px: int = 6):
        return await card_rpcs.qr_preview(decky, uri, module_px)

    async def save_game_card(self, uri: str, title: str = "", appid: str = ""):
        return await card_rpcs.save_card(decky, PRINT_DPI, uri, title, appid)

    async def get_active_media(self):
        """Return every medium currently presented, across all sources.

        The per-source view that `get_tag_status` cannot express: that RPC
        reports the NFC slot only, for backwards compatibility.
        """
        return self._registry.all()

    async def get_launch_origin(self):
        """Return the medium credited with launching the running game, if any."""
        return self._registry.launch_origin

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
            # just "some drive is attached". Part of the MediaSource contract:
            # this used to be a hasattr probe into the subclass, with the
            # enablement half recomputed here from the source's own settings
            # and an imported copy of its defaults.
            sub_devices = source.sub_devices()
            if sub_devices:
                entry["drive_kinds"] = sub_devices
            result.append(entry)
        return result

    async def set_source_setting(self, source_type: str, key: str, value):
        """Update a per-source setting.

        Validated by settings_schema, the same table that governs set_setting
        and the on-disk loader. This path used to check the *type* of a value
        and nothing else, which is how broker_port accepted 70000, serial
        `port` accepted any string at all — bypassing the /dev/ rule the
        equivalent NFC setting was held to — and watch_dir accepted "/",
        pointing a root process's directory scanner at the filesystem root.
        """
        if source_type not in settings_schema.SOURCE_TYPES:
            decky.logger.warning(f"set_source_setting: unknown source_type {source_type!r}")
            return False

        ok, reason = settings_schema.validate(key, value, source_type=source_type)
        if not ok:
            decky.logger.warning(f"set_source_setting: rejected {source_type}.{key} — {reason}")
            return False
        value = settings_schema.coerce(key, value, source_type=source_type)

        sources = self.settings.settings.setdefault("sources", {})
        sources.setdefault(source_type, {})[key] = value

        # MQTT will not start without a shared secret, and the panel has no
        # field to type one into — so switching it on would silently do
        # nothing. Mint a strong one instead of asking the user to invent it,
        # and log it so it can be copied to whatever publishes to the topic.
        if source_type == "mqtt" and key == "enabled" and value:
            if not sources["mqtt"].get("secret"):
                secret = secrets.token_urlsafe(24)
                sources["mqtt"]["secret"] = secret
                decky.logger.info(
                    f"MQTT enabled with no shared secret; generated one. "
                    f"Publishers must include it as a 'secret' field in every "
                    f"message: {secret}"
                )

        if not self.settings.save():
            decky.logger.error(
                f"set_source_setting: {source_type}.{key} could not be written to disk"
            )
            return False
        decky.logger.info(f"Source setting updated: {source_type}.{key} = {value!r}")

        # Switching a category on has to pick up media that is already sitting
        # in the drive. udev fired when it went in, we declined it because the
        # category was off, and udev will not fire again.
        if source_type == "storage" and self.storage_source is not None:
            try:
                await self.storage_source.rescan()
            except Exception as e:
                decky.logger.warning(f"set_source_setting: rescan failed: {e}")

        await self._publish_statuses(force=True)
        return True
