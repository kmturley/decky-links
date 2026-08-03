"""The single settings validation table.

There used to be three answers to "is this setting value acceptable" and they
disagreed. The dangerous one was set_source_setting, which checked the type
and nothing else — so every range and format rule that the equivalent
top-level setting was held to simply did not apply to per-source settings.

These tests pin the rules, and the last class pins the property that motivated
the refactor: that all three entry points now give the same answer.
"""

import pytest

from decky_links import settings_schema as schema


class TestTopLevel:

    @pytest.mark.parametrize("key", ["auto_launch", "auto_close"])
    def test_booleans_accepted(self, key):
        assert schema.validate(key, True)[0]
        assert schema.validate(key, False)[0]

    @pytest.mark.parametrize("value", [1, 0, "true", None, []])
    def test_non_booleans_rejected(self, value):
        assert not schema.validate("auto_launch", value)[0]

    def test_unknown_key_rejected(self):
        ok, reason = schema.validate("definitely_not_a_setting", True)
        assert not ok
        assert "unknown setting" in reason


class TestRangesAreEnforcedOnSourceSettings:
    """The gap this refactor closed: these all passed before, because the
    per-source path only ever checked isinstance()."""

    @pytest.mark.parametrize("port", [-1, 0, 70000, 65536])
    def test_broker_port_out_of_range_rejected(self, port):
        assert not schema.validate("broker_port", port, source_type="mqtt")[0]

    @pytest.mark.parametrize("port", [1, 1883, 8883, 65535])
    def test_broker_port_in_range_accepted(self, port):
        assert schema.validate("broker_port", port, source_type="mqtt")[0]

    @pytest.mark.parametrize("interval", [0.0, 0.1, 61.0, -5.0])
    def test_file_watch_poll_interval_out_of_range_rejected(self, interval):
        assert not schema.validate("poll_interval", interval, source_type="file_watch")[0]

    @pytest.mark.parametrize("baud", [0, 1199, 2_000_000])
    def test_baudrate_out_of_range_rejected(self, baud):
        assert not schema.validate("baudrate", baud, source_type="serial")[0]


class TestDevicePathsAreEnforcedEverywhere:
    """Serial's `port` is the same kind of value as NFC's `device_path`, but
    it used to be an unchecked str while device_path required a /dev/ prefix."""

    @pytest.mark.parametrize("source_type,key", [
        ("nfc", "device_path"),
        ("serial", "port"),
        ("camera", "device"),
    ])
    def test_dev_paths_accepted(self, source_type, key):
        assert schema.validate(key, "/dev/ttyUSB0", source_type=source_type)[0]

    @pytest.mark.parametrize("source_type,key", [
        ("nfc", "device_path"),
        ("serial", "port"),
        ("camera", "device"),
    ])
    @pytest.mark.parametrize("bad", [
        "/etc/passwd",
        "ttyUSB0",
        "/dev/../etc/passwd",   # starts with /dev/ but does not stay there
        "",
    ])
    def test_non_dev_paths_rejected(self, source_type, key, bad):
        assert not schema.validate(key, bad, source_type=source_type)[0]


class TestWatchDir:
    """FileWatchSource scans this on a timer as root."""

    def test_ordinary_absolute_directory_accepted(self):
        assert schema.validate("watch_dir", "/home/deck/triggers",
                               source_type="file_watch")[0]

    @pytest.mark.parametrize("bad", ["/", "/proc", "/sys", "/dev", "/proc/self", "relative/path", ""])
    def test_root_and_pseudo_filesystems_rejected(self, bad):
        assert not schema.validate("watch_dir", bad, source_type="file_watch")[0]


class TestBoolIsNotAnInt:
    """bool subclasses int, so an int-typed rule would otherwise accept True
    and store it as a baud rate or a port number."""

    def test_true_is_not_a_baudrate(self):
        assert not schema.validate("baudrate", True, source_type="serial")[0]

    def test_true_is_not_a_port(self):
        assert not schema.validate("broker_port", True, source_type="mqtt")[0]


class TestCoercion:

    def test_int_becomes_float_for_interval_settings(self):
        assert isinstance(
            schema.coerce("poll_interval", 2, source_type="file_watch"), float
        )

    def test_other_values_pass_through(self):
        assert schema.coerce("broker_port", 1883, source_type="mqtt") == 1883
        assert schema.coerce("enabled", True, source_type="mqtt") is True


class TestReasonsAreUsable:
    """These are returned to the frontend, so they have to read as a
    requirement rather than a restatement of the failure."""

    def test_reason_states_the_requirement(self):
        _ok, reason = schema.validate("broker_port", 70000, source_type="mqtt")
        assert "1-65535" in reason

    def test_reason_names_the_key(self):
        _ok, reason = schema.validate("watch_dir", "/", source_type="file_watch")
        assert "watch_dir" in reason


class TestEntryPointsAgree:
    """The property the refactor exists to guarantee."""

    def test_nfc_settings_validate_the_same_by_either_route(self):
        # set_setting addresses NFC settings by bare name; set_source_setting
        # addresses them under their source. Same rule either way.
        for value, expected in [("/dev/ttyUSB0", True), ("/etc/passwd", False)]:
            assert schema.validate("device_path", value)[0] is expected
            assert schema.validate(
                "device_path", value, source_type="nfc"
            )[0] is expected

    def test_every_source_type_shipped_has_rules(self, tmp_path):
        """A source whose settings are not in the table is unreachable from
        the panel, because set_source_setting rejects unknown source types."""
        from main import SettingsManager
        mgr = SettingsManager(str(tmp_path / "settings.json"))
        assert set(mgr.settings["sources"]) == set(schema.SOURCE_RULES)

    def test_every_shipped_default_passes_its_own_rule(self, tmp_path):
        """Read from SettingsManager rather than restated here — a default the
        validator rejects would be discarded by the loader on first read, and
        duplicating the list is what let the rules drift in the first place.

        watch_dir is exempt: it ships empty meaning "unconfigured", and
        FileWatchSource refuses to start until it is set.
        """
        from main import SettingsManager
        mgr = SettingsManager(str(tmp_path / "settings.json"))
        exempt = {("file_watch", "watch_dir")}

        for source_type, values in mgr.settings["sources"].items():
            for key, value in values.items():
                if (source_type, key) in exempt:
                    continue
                ok, reason = schema.validate(key, value, source_type=source_type)
                assert ok, f"default {source_type}.{key}={value!r} rejected: {reason}"
