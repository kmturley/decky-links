"""Settings persistence, separated from the plugin that consumes them.

Two things live here: the defaults the plugin ships with, and the merge that
decides which values from an on-disk settings.json are allowed to override
them. What counts as a valid value does not live here — that is
:mod:`decky_links.settings_schema`, which the RPC entry points share, so the
file on disk and the panel cannot disagree about what is acceptable.

The logger is injected rather than imported. ``decky`` only exists inside the
plugin loader's process, and importing it at module scope is what forced every
settings test to go through a fully constructed Plugin.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

from decky_links import settings_schema
from decky_links.settings_schema import (
    RESTRICTED_SETTING_KEYS,
    NFC_SETTING_KEYS,
    TOP_LEVEL_SETTING_KEYS,
)

# What ``get_restricted`` falls back to for a key an older settings.json never had.
_RESTRICTED_DEFAULTS = {
    "key_hash": "",
    "key_label": "",
}


class _NullLogger:
    """Used when no logger is supplied, so tests need not pass one."""

    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class SettingsManager:
    def __init__(self, path, logger=None):
        self.path = path
        self._log = logger or _NullLogger()
        self.settings = {
            "auto_launch": True,
            "auto_close": False,
            # The launch splash (issue #8). Opt-in: see TOP_LEVEL_RULES.
            "splash": False,
            # Restricted mode. Its own block rather than four more top-level keys,
            # because top-level keys are what the generic set_setting RPC is
            # allowed to write, and the lock must not be one of those.
            "restricted": {
                # SHA-256 of the token carried by the key medium, and the one
                # thing that switches restricted mode on. Empty means no key,
                # which means the feature is off. Whether the plugin is
                # *locked* is not stored: it is whether that key is present.
                "key_hash": "",
                "key_label": "",
            },
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
                    # Empty means the source will refuse to start. Anything
                    # able to publish to the topic can launch games on this
                    # device, so there is no sensible default here — see
                    # MqttSource.start.
                    "secret": "",
                    "tls": False,
                    "username": "",
                    "password": "",
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
                    self._forget_family_view_pin(loaded)
        except Exception as e:
            self._log.error(f"Failed to load settings: {e}")

    def _forget_family_view_pin(self, loaded: Dict[str, Any]) -> None:
        """Erase a Family View PIN left by a version that stored one.

        Dropping the key from the schema already stops it being read, but a
        secret nobody reads is still a secret on disk — and this one is the PIN
        protecting the account on the device the file sits on. It would survive
        until something else happened to save, which for a user who never
        changes a setting is never.
        """
        if not isinstance(loaded.get("restricted"), dict):
            return
        if "family_view_pin" not in loaded["restricted"]:
            return
        self._log.info("Discarding stored Family View PIN — no longer used")
        self.save()

    def save(self) -> bool:
        """Persist the settings. Returns whether they actually reached disk.

        The return value matters: this used to fail silently on a permissions
        error while set_setting went on to return True to the frontend, so the
        panel showed a toggle as saved when nothing had been written and the
        old value came back on the next restart.

        Written to a temp file and renamed so an interrupted write cannot
        leave a truncated settings.json, which loads as invalid JSON and
        resets every preference the user had.
        """
        try:
            dir_path = os.path.dirname(self.path)
            os.makedirs(dir_path, exist_ok=True)
            if not os.access(dir_path, os.W_OK):
                self._log.error(f"No write permission for settings directory: {dir_path}")
                return False
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(self.settings, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            return True
        except IOError as e:
            self._log.error(f"Failed to write settings file {self.path}: {e}")
        except Exception as e:
            self._log.error(f"Failed to save settings: {e}")
        return False

    def get(self, key):
        if key in TOP_LEVEL_SETTING_KEYS:
            return self.settings.get(key)
        if key in NFC_SETTING_KEYS:
            return self.settings["sources"]["nfc"].get(key)
        return self.settings.get(key)

    def set(self, key, value) -> bool:
        """Store one setting and persist. Returns whether the write succeeded."""
        if key in TOP_LEVEL_SETTING_KEYS:
            self.settings[key] = value
        elif key in NFC_SETTING_KEYS:
            self.settings["sources"]["nfc"][key] = value
        elif key == "sources.nfc" and isinstance(value, dict):
            self.settings["sources"]["nfc"].update(value)
        else:
            self.settings[key] = value
        return self.save()

    def _validate_setting(self, key, value) -> bool:
        """Retained as a thin adapter — the rules live in settings_schema."""
        ok, _reason = settings_schema.validate(key, value)
        return ok

    def get_restricted(self, key: Optional[str] = None):
        """The restricted block, or one value from it.

        Defaults to the shipped value rather than None when a key is missing,
        so a settings.json written by an older version — which has no restricted
        block at all — reads as "unlocked, no key" rather than as None, which
        would make ``locked`` falsy by accident rather than by decision.
        """
        block = self.settings.setdefault("restricted", {})
        if key is None:
            return block
        return block.get(key, _RESTRICTED_DEFAULTS.get(key))

    def set_restricted(self, key: str, value: Any) -> bool:
        """Store one restricted setting and persist. Returns whether it was written.

        Validated here as well as at the RPC: this is the only writer, and a
        caller that skipped the check would put a value on disk that
        ``load()`` then refuses, silently reverting the change on restart.
        """
        ok, _reason = settings_schema.validate_restricted(key, value)
        if not ok:
            self._log.warning(f"Refusing invalid restricted setting: {key!r}={value!r}")
            return False
        self.settings.setdefault("restricted", {})[key] = value
        return self.save()

    def get_source_settings(self, source_type: str) -> Dict[str, Any]:
        sources = self.settings.setdefault("sources", {})
        source_settings = sources.setdefault(source_type, {})
        return source_settings

    def _merge_loaded_settings(self, loaded: Dict[str, Any]) -> None:
        loaded_restricted = loaded.get("restricted")
        if isinstance(loaded_restricted, dict):
            for key, value in loaded_restricted.items():
                if key not in RESTRICTED_SETTING_KEYS:
                    continue
                ok, reason = settings_schema.validate_restricted(key, value)
                if ok:
                    self.settings["restricted"][key] = value
                else:
                    self._log.warning(f"Ignoring invalid restricted setting from file: {reason}")

        for key in TOP_LEVEL_SETTING_KEYS:
            if key in loaded:
                value = loaded[key]
                if self._validate_setting(key, value):
                    self.settings[key] = value
                else:
                    self._log.warning(
                        f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                    )

        # Every source, not just NFC.
        #
        # This used to read `loaded["sources"]["nfc"]` and nothing else, so
        # every other source's settings were discarded on load and silently
        # reverted to the shipped defaults on the next restart: switching USB
        # storage on, the MQTT broker and its secret, the serial port, the
        # watched directory. Each was written to disk correctly and then
        # ignored when read back, which is the worst shape a settings bug can
        # take — the panel showed the change, and only a restart undid it.
        #
        # `nfc` had a hand-written branch because it was the only source when
        # this was written; the schema now describes them all, so the merge is
        # a loop over it rather than a branch per source that will drift again.
        loaded_sources = loaded.get("sources")
        if isinstance(loaded_sources, dict):
            for source_type, values in loaded_sources.items():
                if source_type not in settings_schema.SOURCE_TYPES:
                    self._log.warning(f"Ignoring unknown source in settings: {source_type!r}")
                    continue
                if not isinstance(values, dict):
                    continue
                target = self.settings["sources"].setdefault(source_type, {})
                for key, value in values.items():
                    ok, reason = settings_schema.validate(key, value, source_type=source_type)
                    if ok:
                        target[key] = settings_schema.coerce(
                            key, value, source_type=source_type
                        )
                    else:
                        self._log.warning(f"Ignoring invalid setting from file: {reason}")

        for key in NFC_SETTING_KEYS:
            if key in loaded:
                value = loaded[key]
                if self._validate_setting(key, value):
                    self.settings["sources"]["nfc"][key] = value
                else:
                    self._log.warning(
                        f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                    )
