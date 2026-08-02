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
from decky_links.settings_schema import NFC_SETTING_KEYS, TOP_LEVEL_SETTING_KEYS


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
        except Exception as e:
            self._log.error(f"Failed to load settings: {e}")

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
                    self._log.warning(
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
                        self._log.warning(
                            f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                        )

        for key in NFC_SETTING_KEYS:
            if key in loaded:
                value = loaded[key]
                if self._validate_setting(key, value):
                    self.settings["sources"]["nfc"][key] = value
                else:
                    self._log.warning(
                        f"Ignoring invalid setting from file: key={key!r}, value={value!r}"
                    )
