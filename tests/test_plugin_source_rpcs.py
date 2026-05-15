"""
test_plugin_source_rpcs.py — tests for get_source_statuses() and set_source_setting() RPCs.
"""
import asyncio
import pytest
from unittest.mock import MagicMock


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
    mock_settings.save = MagicMock()

    p.settings = mock_settings
    p._event_queue = asyncio.Queue()
    p.source_manager = SourceManager(p._event_queue, logger=MagicMock())

    p.nfc_source = NfcSource(_settings["sources"]["nfc"], logger=MagicMock())
    p.mqtt_source = MqttSource(_settings["sources"]["mqtt"], logger=MagicMock())
    p.serial_source = SerialSource(_settings["sources"]["serial"], logger=MagicMock())
    p.file_watch_source = FileWatchSource(_settings["sources"]["file_watch"], logger=MagicMock())

    p.source_manager.register(p.nfc_source)
    p.source_manager.register(p.mqtt_source)
    p.source_manager.register(p.serial_source)
    p.source_manager.register(p.file_watch_source)

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
        p.mqtt_source._active = True
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
        result = await p.set_source_setting("nfc", "enabled", True)
        assert result is False

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
    p.storage_source = storage
    p.source_manager.register(storage)
    return p, storage


class TestGetSourceStatusesHasMedia:

    @pytest.mark.asyncio
    async def test_storage_udev_running_but_no_payload_reports_inactive(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()  # udev monitor running
        result = await p.get_source_statuses()
        storage_entry = next(e for e in result if e["source_type"] == "storage")
        assert storage_entry["active"] is False

    @pytest.mark.asyncio
    async def test_storage_with_active_media_reports_active(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._active_media["/dev/sdb1"] = "steam://run/12345"
        result = await p.get_source_statuses()
        storage_entry = next(e for e in result if e["source_type"] == "storage")
        assert storage_entry["active"] is True

    @pytest.mark.asyncio
    async def test_storage_after_media_removal_reports_inactive(self, tmp_path):
        p, storage = _make_plugin_with_storage(tmp_path)
        storage._monitor = MagicMock()
        storage._active_media["/dev/sdb1"] = "steam://run/12345"
        del storage._active_media["/dev/sdb1"]
        result = await p.get_source_statuses()
        storage_entry = next(e for e in result if e["source_type"] == "storage")
        assert storage_entry["active"] is False

    @pytest.mark.asyncio
    async def test_non_storage_sources_use_is_active_via_has_media(self, tmp_path):
        p, _ = _make_plugin_with_sources(tmp_path)
        p.mqtt_source._active = True
        result = await p.get_source_statuses()
        mqtt_entry = next(e for e in result if e["source_type"] == "mqtt")
        assert mqtt_entry["active"] is True
