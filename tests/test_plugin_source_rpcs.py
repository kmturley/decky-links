"""
test_plugin_source_rpcs.py — tests for get_source_statuses() and set_source_setting() RPCs.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from sources.base import SourceType


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_plugin_with_sources(tmp_path):
    from main import Plugin, PluginState, SettingsManager
    from sources.manager import SourceManager
    from sources.nfc_source import NfcSource
    from sources.mqtt_source import MqttSource
    from sources.serial_source import SerialSource
    from sources.file_watch_source import FileWatchSource

    p = Plugin()

    _settings = {
        "auto_launch": True,
        "auto_close": False,
        # Unlocked, no key — the shipped state. Plugin.locked reads this on
        # every guarded RPC, and a bare MagicMock attribute reads as locked.
        "restricted": {"key_hash": "",
                  "key_label": "", "family_view_pin": ""},
        "sources": {
            "nfc":  {"device_path": "/dev/ttyUSB0", "baudrate": 115200,
                     "polling_interval": 0.5, "reader_type": "pn532_uart"},
            "mqtt": {"enabled": False, "broker_host": "localhost", "broker_port": 1883,
                     "topic": "decky-links", "secret": ""},
            "serial": {"enabled": False, "port": "/dev/ttyUSB1", "baudrate": 9600},
            "file_watch": {"enabled": False, "watch_dir": "", "poll_interval": 2.0},
        },
    }
    settings_path = str(tmp_path / "settings.json")
    mock_settings = MagicMock(spec=SettingsManager)
    mock_settings.get.side_effect = lambda key, default=None: (
        _settings.get(key, _settings["sources"]["nfc"].get(key, default))
    )
    mock_settings.settings = _settings
    mock_settings.get_source_settings = lambda src: _settings["sources"].get(src, {})
    mock_settings.get_restricted = lambda key=None: (
        _settings["restricted"] if key is None else _settings["restricted"].get(key)
    )
    mock_settings.save = MagicMock()

    p.settings = mock_settings
    p._event_queue = asyncio.Queue()
    p.source_manager = SourceManager(p._event_queue, logger=MagicMock())

    # Registered only. The manager's registry is the sole record of what
    # exists; Plugin.nfc_source and friends are lookups into it, so assigning
    # them as well was how a source could be registered but not remembered.
    for source in (
        NfcSource(_settings["sources"]["nfc"], logger=MagicMock()),
        MqttSource(_settings["sources"]["mqtt"], logger=MagicMock()),
        SerialSource(_settings["sources"]["serial"], logger=MagicMock()),
        FileWatchSource(_settings["sources"]["file_watch"], logger=MagicMock()),
    ):
        p.source_manager.register(source)

    p.state = PluginState.READY
    p.is_pairing = False
    p.pairing_uri = None
    p.running_game_id = None
    p.current_tag_uid = None
    p.current_tag_uri = None

    return p, _settings


# ── get_source_statuses() ─────────────────────────────────────────────────────

class TestGetSourceStatuses:

    @pytest.mark.asyncio
    async def test_returns_list(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_returns_entry_per_registered_source(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        assert len(result) == 4

    @pytest.mark.asyncio
    async def test_entry_has_required_fields(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        for entry in result:
            assert "source_id" in entry
            assert "source_type" in entry
            assert "active" in entry

    @pytest.mark.asyncio
    async def test_active_is_bool(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        for entry in result:
            assert isinstance(entry["active"], bool)

    @pytest.mark.asyncio
    async def test_inactive_sources_report_false(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        # None are started, so all inactive
        for entry in result:
            assert entry["active"] is False

    @pytest.mark.asyncio
    async def test_source_types_match_known_values(self, tmp_path):
        from sources.base import SourceType
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        known_types = {st.value for st in SourceType}
        for entry in result:
            assert entry["source_type"] in known_types

    @pytest.mark.asyncio
    async def test_nfc_source_included(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        types = [e["source_type"] for e in result]
        assert "nfc" in types

    @pytest.mark.asyncio
    async def test_mqtt_source_included(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        types = [e["source_type"] for e in result]
        assert "mqtt" in types

    @pytest.mark.asyncio
    async def test_serial_source_included(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        types = [e["source_type"] for e in result]
        assert "serial" in types

    @pytest.mark.asyncio
    async def test_file_watch_source_included(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        types = [e["source_type"] for e in result]
        assert "file_watch" in types

    @pytest.mark.asyncio
    async def test_source_id_is_string(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        for entry in result:
            assert isinstance(entry["source_id"], str)
            assert len(entry["source_id"]) > 0

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_source_manager(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        p.source_manager = None
        result = await p.get_source_statuses()
        assert result == []

    @pytest.mark.asyncio
    async def test_active_source_reports_true(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        # Manually activate mqtt source
        p._source_of_type(SourceType.MQTT)._active = True
        result = await p.get_source_statuses()
        mqtt_entry = next(e for e in result if e["source_type"] == "mqtt")
        assert mqtt_entry["active"] is True

    @pytest.mark.asyncio
    async def test_source_ids_are_unique(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.get_source_statuses()
        ids = [e["source_id"] for e in result]
        assert len(ids) == len(set(ids))


# ── set_source_setting() ──────────────────────────────────────────────────────

class TestSetSourceSetting:

    @pytest.mark.asyncio
    async def test_set_mqtt_enabled_true(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "enabled", True)
        assert result is True
        assert settings["sources"]["mqtt"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_set_mqtt_enabled_false(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        settings["sources"]["mqtt"]["enabled"] = True
        result = await p.set_source_setting("mqtt", "enabled", False)
        assert result is True
        assert settings["sources"]["mqtt"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_set_serial_enabled(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("serial", "enabled", True)
        assert result is True
        assert settings["sources"]["serial"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_set_file_watch_enabled(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("file_watch", "enabled", True)
        assert result is True
        assert settings["sources"]["file_watch"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_unknown_source_type_rejected(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("teleporter", "enabled", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_nfc_can_be_switched_off(self, tmp_path):
        """NFC was the one source with no enabled key, so it scanned for a
        reader whether or not the user wanted it to."""
        p, settings = _make_plugin_with_sources(tmp_path)
        assert await p.set_source_setting("nfc", "enabled", False) is True
        assert settings["sources"]["nfc"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "inject_code", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_type_rejected(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "enabled", "yes")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_mqtt_broker_host(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "broker_host", "192.168.1.10")
        assert result is True
        assert settings["sources"]["mqtt"]["broker_host"] == "192.168.1.10"

    @pytest.mark.asyncio
    async def test_set_mqtt_broker_port(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "broker_port", 8883)
        assert result is True
        assert settings["sources"]["mqtt"]["broker_port"] == 8883

    @pytest.mark.asyncio
    async def test_set_mqtt_topic(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "topic", "home/trigger")
        assert result is True
        assert settings["sources"]["mqtt"]["topic"] == "home/trigger"

    @pytest.mark.asyncio
    async def test_set_serial_port(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("serial", "port", "/dev/ttyACM0")
        assert result is True
        assert settings["sources"]["serial"]["port"] == "/dev/ttyACM0"

    @pytest.mark.asyncio
    async def test_set_serial_baudrate(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("serial", "baudrate", 115200)
        assert result is True
        assert settings["sources"]["serial"]["baudrate"] == 115200

    @pytest.mark.asyncio
    async def test_set_file_watch_watch_dir(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("file_watch", "watch_dir", "/tmp/triggers")
        assert result is True
        assert settings["sources"]["file_watch"]["watch_dir"] == "/tmp/triggers"

    @pytest.mark.asyncio
    async def test_set_file_watch_poll_interval_float(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("file_watch", "poll_interval", 5.0)
        assert result is True
        assert settings["sources"]["file_watch"]["poll_interval"] == 5.0

    @pytest.mark.asyncio
    async def test_set_file_watch_poll_interval_int_coerced(self, tmp_path):
        p, settings = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("file_watch", "poll_interval", 3)
        assert result is True
        assert settings["sources"]["file_watch"]["poll_interval"] == 3.0

    @pytest.mark.asyncio
    async def test_saves_settings_after_update(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        await p.set_source_setting("mqtt", "enabled", True)
        p.settings.save.assert_called()

    @pytest.mark.asyncio
    async def test_broker_port_must_be_int(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        result = await p.set_source_setting("mqtt", "broker_port", "1883")
        assert result is False


# ── get_source_statuses() — has_media() for StorageSource ────────────────────

def _make_plugin_with_storage(tmp_path):
    """Like _make_plugin_with_sources but registers a StorageSource as well."""
    from sources.storage_source import StorageSource
    p, settings = _make_plugin_with_sources(tmp_path)
    storage = StorageSource({}, logger=MagicMock())
    p.source_manager.register(storage)   # p.storage_source is a lookup into this
    return p, storage


class TestGetSourceStatusesHasMedia:
    """`active` tracks the drive; `has_media` tracks the disk in it.

    Reporting the source as inactive the moment a floppy is ejected reads as a
    fault — the drive is still plugged in and still watching for a disk.
    """

    @pytest.mark.asyncio
    async def test_no_drive_reports_inactive(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()  # udev monitor running, but no drive
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "storage")
        assert entry["active"] is False
        assert entry["has_media"] is False

    @pytest.mark.asyncio
    async def test_drive_with_disk_reports_active_with_media(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._drives["/dev/sda"] = "floppy"
        storage._active_media["/dev/sda"] = "steam://run/12345"
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "storage")
        assert entry["active"] is True
        assert entry["has_media"] is True

    @pytest.mark.asyncio
    async def test_drive_stays_active_after_disk_ejected(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._drives["/dev/sda"] = "floppy"
        storage._active_media["/dev/sda"] = "steam://run/12345"
        del storage._active_media["/dev/sda"]
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "storage")
        assert entry["active"] is True, "ejecting a disk does not unplug the drive"
        assert entry["has_media"] is False

    @pytest.mark.asyncio
    async def test_unplugging_the_drive_reports_inactive(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._drives["/dev/sda"] = "floppy"
        storage._drives.pop("/dev/sda", None)
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "storage")
        assert entry["active"] is False

    @pytest.mark.asyncio
    async def test_non_storage_sources_use_is_active_via_has_media(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        p._source_of_type(SourceType.MQTT)._active = True
        result = await p.get_source_statuses()
        mqtt_entry = next(e for e in result if e["source_type"] == "mqtt")
        assert mqtt_entry["active"] is True


# ── can_pair — the game-page link button's gate ───────────────────────────────

class TestGetSourceStatusesCanPair:
    """The link button on a game page used to ask get_reader_status, so it
    reported "NFC reader not detected" even with a pairable floppy connected.
    It now looks for any source that can be written to."""

    @pytest.mark.asyncio
    async def test_storage_advertises_can_pair(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "storage")
        assert entry["can_pair"] is True

    @pytest.mark.asyncio
    async def test_nfc_advertises_can_pair(self, tmp_path):
        p, _ = _make_plugin_with_storage(tmp_path)
        result = await p.get_source_statuses()
        entry = next(e for e in result if e["source_type"] == "nfc")
        assert entry["can_pair"] is True

    @pytest.mark.asyncio
    async def test_read_only_sources_cannot_pair(self, tmp_path):
        """A camera reads QR codes; there is nothing to write back to."""
        p, _ = _make_plugin_with_storage(tmp_path)
        result = await p.get_source_statuses()
        for entry in result:
            if entry["source_type"] in ("camera", "mqtt", "file_watch"):
                assert entry["can_pair"] is False, entry["source_type"]

    @pytest.mark.asyncio
    async def test_a_connected_floppy_is_pairable_without_any_nfc_reader(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._drives["/dev/sda"] = "floppy"
        result = await p.get_source_statuses()
        pairable = [e for e in result if e["can_pair"] and e["active"]]
        assert [e["source_type"] for e in pairable] == ["storage"]


# ── MQTT secret provisioning ──────────────────────────────────────────────────

class TestMqttSecretIsProvisionedOnEnable:
    """MqttSource refuses to start without a shared secret, and the panel has
    no field to type one into — so enabling the toggle would silently do
    nothing. One is minted instead of asking the user to invent it."""

    @pytest.mark.asyncio
    async def test_enabling_mqtt_mints_a_secret(self, plugin):
        plugin.settings.settings["sources"]["mqtt"]["secret"] = ""
        assert await plugin.set_source_setting("mqtt", "enabled", True)
        secret = plugin.settings.settings["sources"]["mqtt"]["secret"]
        assert secret
        assert len(secret) >= 24

    @pytest.mark.asyncio
    async def test_an_existing_secret_is_never_replaced(self, plugin):
        plugin.settings.settings["sources"]["mqtt"]["secret"] = "mine"
        await plugin.set_source_setting("mqtt", "enabled", True)
        assert plugin.settings.settings["sources"]["mqtt"]["secret"] == "mine"

    @pytest.mark.asyncio
    async def test_disabling_does_not_mint_one(self, plugin):
        plugin.settings.settings["sources"]["mqtt"]["secret"] = ""
        await plugin.set_source_setting("mqtt", "enabled", False)
        assert plugin.settings.settings["sources"]["mqtt"]["secret"] == ""

    @pytest.mark.asyncio
    async def test_generated_secrets_differ_between_installs(self, plugin):
        plugin.settings.settings["sources"]["mqtt"]["secret"] = ""
        await plugin.set_source_setting("mqtt", "enabled", True)
        first = plugin.settings.settings["sources"]["mqtt"]["secret"]
        plugin.settings.settings["sources"]["mqtt"]["secret"] = ""
        await plugin.set_source_setting("mqtt", "enabled", True)
        assert plugin.settings.settings["sources"]["mqtt"]["secret"] != first
