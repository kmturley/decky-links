"""SettingsManager — persistence and the on-disk merge.

This had no direct tests before it was extracted. The plugin fixture replaces
SettingsManager with a MagicMock, so everything here — which values from disk
are allowed to override the defaults, what happens to a corrupt file, whether
a save actually reached disk — was only ever exercised incidentally, if at
all. Extracting it from main.py made it constructible on its own, and this is
the reason that mattered.
"""

import json
import os

import pytest

from decky_links.settings import SettingsManager


def _mgr(tmp_path, name="settings.json"):
    return SettingsManager(str(tmp_path / name))


class TestDefaults:

    def test_ships_all_six_sources(self, tmp_path):
        mgr = _mgr(tmp_path)
        assert set(mgr.settings["sources"]) == {
            "nfc", "storage", "camera", "mqtt", "serial", "file_watch",
        }

    def test_auto_launch_on_auto_close_off(self, tmp_path):
        """Tapping a card should start a game; removing it should not close
        one unless the user asked for that."""
        mgr = _mgr(tmp_path)
        assert mgr.settings["auto_launch"] is True
        assert mgr.settings["auto_close"] is False

    def test_only_floppy_is_a_trigger_by_default(self, tmp_path):
        """A floppy drive on a Steam Deck is there on purpose. USB sticks and
        cards usually hold the user's own data, so mounting them uninvited is
        both a surprise and a delay."""
        kinds = _mgr(tmp_path).settings["sources"]["storage"]["drive_kinds"]
        assert kinds["floppy"] is True
        assert not any(v for k, v in kinds.items() if k != "floppy")

    def test_network_and_camera_sources_are_opt_in(self, tmp_path):
        sources = _mgr(tmp_path).settings["sources"]
        for name in ("camera", "mqtt", "serial", "file_watch"):
            assert sources[name]["enabled"] is False, name

    def test_missing_file_leaves_defaults_intact(self, tmp_path):
        mgr = _mgr(tmp_path, "does-not-exist.json")
        assert mgr.settings["auto_launch"] is True


class TestLoadMerge:

    def test_valid_values_override_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": False, "auto_close": True}))
        mgr = SettingsManager(str(path))
        assert mgr.settings["auto_launch"] is False
        assert mgr.settings["auto_close"] is True

    def test_invalid_values_are_ignored_not_adopted(self, tmp_path):
        """A settings file is editable by hand and survives upgrades, so it is
        as much an input to validate as anything from the panel."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": "yes please"}))
        mgr = SettingsManager(str(path))
        assert mgr.settings["auto_launch"] is True   # the default, not "yes please"

    def test_nfc_settings_merge_from_the_sources_block(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({
            "sources": {"nfc": {"device_path": "/dev/ttyACM3", "baudrate": 9600}}
        }))
        mgr = SettingsManager(str(path))
        assert mgr.settings["sources"]["nfc"]["device_path"] == "/dev/ttyACM3"
        assert mgr.settings["sources"]["nfc"]["baudrate"] == 9600

    def test_out_of_range_source_values_rejected(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"sources": {"nfc": {"baudrate": 99}}}))
        mgr = SettingsManager(str(path))
        assert mgr.settings["sources"]["nfc"]["baudrate"] == 115200

    def test_device_path_outside_dev_rejected(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"sources": {"nfc": {"device_path": "/etc/passwd"}}}))
        mgr = SettingsManager(str(path))
        assert mgr.settings["sources"]["nfc"]["device_path"] != "/etc/passwd"

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json at all")
        mgr = SettingsManager(str(path))
        assert mgr.settings["auto_launch"] is True

    def test_non_object_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("[1, 2, 3]")
        mgr = SettingsManager(str(path))
        assert mgr.settings["auto_launch"] is True

    def test_unknown_keys_do_not_crash_the_load(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": False, "from_a_newer_version": 1}))
        mgr = SettingsManager(str(path))
        assert mgr.settings["auto_launch"] is False


class TestEverySourceIsLoaded:
    """Found on a Deck: switching USB storage on, restarting, and finding it
    off again.

    The merge read `sources.nfc` and nothing else, so every other source's
    settings were written to disk correctly and then dropped when read back.
    The panel showed the change and only a restart undid it, which is why it
    survived: nothing in the session that made the change could see it.
    """

    def _reloaded(self, tmp_path, sources):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"sources": sources}))
        return SettingsManager(str(path)).settings["sources"]

    def test_storage_drive_kinds_survive_a_restart(self, tmp_path):
        kinds = {"floppy": True, "optical": False, "usb": True, "flash": False}
        loaded = self._reloaded(tmp_path, {"storage": {"drive_kinds": kinds}})
        assert loaded["storage"]["drive_kinds"]["usb"] is True

    def test_a_source_being_switched_on_survives(self, tmp_path):
        loaded = self._reloaded(tmp_path, {"serial": {"enabled": True}})
        assert loaded["serial"]["enabled"] is True

    def test_the_mqtt_secret_survives(self, tmp_path):
        """Minted once when MQTT is enabled. Lost on restart, every publisher
        it was given to stops being able to launch anything."""
        loaded = self._reloaded(tmp_path, {"mqtt": {"secret": "s3cret", "broker_port": 8883}})
        assert loaded["mqtt"]["secret"] == "s3cret"
        assert loaded["mqtt"]["broker_port"] == 8883

    def test_the_watched_directory_survives(self, tmp_path):
        loaded = self._reloaded(tmp_path, {"file_watch": {"watch_dir": "/home/deck/tags"}})
        assert loaded["file_watch"]["watch_dir"] == "/home/deck/tags"

    def test_nfc_still_loads(self, tmp_path):
        """The case that always worked, kept so the loop cannot regress it."""
        loaded = self._reloaded(tmp_path, {"nfc": {"baudrate": 9600}})
        assert loaded["nfc"]["baudrate"] == 9600

    def test_an_invalid_value_is_still_refused(self, tmp_path):
        """Loading more sources must not mean loading them unchecked — this is
        the same table set_source_setting is held to."""
        loaded = self._reloaded(tmp_path, {"mqtt": {"broker_port": 70000}})
        assert loaded["mqtt"]["broker_port"] == 1883

    def test_an_unknown_source_is_ignored(self, tmp_path):
        loaded = self._reloaded(tmp_path, {"telepathy": {"enabled": True}})
        assert "telepathy" not in loaded

    def test_an_int_is_coerced_like_the_rpc_does(self, tmp_path):
        """Otherwise a JSON 2 and a JSON 2.0 behave differently downstream
        depending on whether the value came from the panel or from disk."""
        loaded = self._reloaded(tmp_path, {"file_watch": {"poll_interval": 3}})
        assert loaded["file_watch"]["poll_interval"] == 3.0
        assert isinstance(loaded["file_watch"]["poll_interval"], float)


class TestRestrictedBlockPersists:

    def test_a_registered_key_survives_a_restart(self, tmp_path):
        """The key is what switches restricted mode on, so it is the one thing
        that has to come back. Whether the device is *locked* is not stored at
        all — it is whether that key is present."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"restricted": {"key_hash": "a" * 64}}))
        mgr = SettingsManager(str(path))
        assert mgr.get_restricted("key_hash") == "a" * 64

    def test_a_stored_lock_from_an_older_build_is_ignored(self, tmp_path):
        """It was briefly persisted. Loading it now would resurrect the second
        source of truth this design exists to remove."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"restricted": {"locked": True, "key_hash": "a" * 64}}))
        mgr = SettingsManager(str(path))
        assert "locked" not in mgr.get_restricted()

    def test_a_stored_family_view_pin_is_erased_from_disk(self, tmp_path):
        """It was stored while restricted mode leaned on Steam's Family View.
        Dropping the key from the schema stops it being read, but a secret
        nobody reads is still a secret on disk — and this one is the PIN for
        the account on the device the file sits on."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({
            "restricted": {"key_hash": "a" * 64, "family_view_pin": "4321"},
        }))
        mgr = SettingsManager(str(path))
        assert "family_view_pin" not in mgr.get_restricted()
        assert "4321" not in path.read_text()
        # The rest of the block survives the rewrite.
        assert mgr.get_restricted("key_hash") == "a" * 64

    def test_a_file_without_one_is_not_rewritten(self, tmp_path):
        """Loading settings must not write to disk in the ordinary case: the
        migration is the exception, and a save on every start is a chance to
        truncate the file for no reason at all."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"restricted": {"key_hash": "a" * 64}}))
        before = path.stat().st_mtime_ns
        SettingsManager(str(path))
        assert path.stat().st_mtime_ns == before

    def test_an_invalid_hash_is_refused(self, tmp_path):
        """A malformed hash that loaded would be a key nothing can match, on a
        device that still believes it can be locked."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"restricted": {"key_hash": "nonsense"}}))
        mgr = SettingsManager(str(path))
        assert mgr.get_restricted("key_hash") == ""

    def test_settings_from_before_restricted_mode_have_no_key(self, tmp_path):
        """An upgrade must not lock anybody out of their own device: no key
        means the feature is off."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": True}))
        mgr = SettingsManager(str(path))
        assert mgr.get_restricted("key_hash") == ""


class TestSave:

    def test_save_reports_success(self, tmp_path):
        assert _mgr(tmp_path).save() is True

    def test_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        mgr = SettingsManager(str(path))
        assert mgr.set("auto_close", True) is True
        assert SettingsManager(str(path)).settings["auto_close"] is True

    def test_set_returns_whether_it_reached_disk(self, tmp_path, monkeypatch):
        """Returning True unconditionally meant an unwritable settings file
        still showed as saved in the panel, and reverted on restart.

        The unwritable directory is simulated rather than created with chmod:
        the plugin runs as root and so does CI, and root ignores the mode.
        """
        mgr = _mgr(tmp_path)
        monkeypatch.setattr(os, "access", lambda *a, **k: False)
        assert mgr.set("auto_close", True) is False

    def test_a_write_error_is_reported_not_swallowed(self, tmp_path, monkeypatch):
        mgr = _mgr(tmp_path)
        monkeypatch.setattr(
            "builtins.open", lambda *a, **k: (_ for _ in ()).throw(IOError("nope"))
        )
        assert mgr.save() is False

    def test_no_temp_file_is_left_behind(self, tmp_path):
        """Written via temp-file-and-rename so an interrupted save cannot
        truncate settings.json — which loads as invalid JSON and resets every
        preference the user had."""
        mgr = _mgr(tmp_path)
        mgr.save()
        assert os.listdir(tmp_path) == ["settings.json"]

    def test_an_interrupted_save_does_not_destroy_the_old_file(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        mgr = SettingsManager(str(path))
        mgr.set("auto_close", True)
        original = path.read_text()

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        assert mgr.save() is False
        assert path.read_text() == original


class TestAccessors:

    def test_get_reads_top_level_and_nfc_keys(self, tmp_path):
        mgr = _mgr(tmp_path)
        assert mgr.get("auto_launch") is True
        assert mgr.get("baudrate") == 115200

    def test_get_source_settings_returns_the_live_dict(self, tmp_path):
        """Sources are handed this dict at construction and read it on every
        poll, which is how a panel toggle takes effect without a restart."""
        mgr = _mgr(tmp_path)
        live = mgr.get_source_settings("mqtt")
        mgr.settings["sources"]["mqtt"]["enabled"] = True
        assert live["enabled"] is True

    def test_get_source_settings_creates_missing_sections(self, tmp_path):
        assert _mgr(tmp_path).get_source_settings("something_new") == {}


class TestLoggerInjection:
    """decky only exists inside the plugin loader's process. Importing it at
    module scope is what forced these tests through a whole Plugin."""

    def test_works_without_a_logger(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": "nonsense"}))
        SettingsManager(str(path))   # must not raise on the warning path

    def test_uses_the_injected_logger(self, tmp_path):
        from unittest.mock import MagicMock
        log = MagicMock()
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_launch": "nonsense"}))
        SettingsManager(str(path), logger=log)
        assert log.warning.called
