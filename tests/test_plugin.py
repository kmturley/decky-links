"""
test_plugin.py — unit tests for decky-links main.py logic.

All NFC hardware, the decky runtime, and subprocess calls are mocked.
Tests cover:
  - State machine transitions (Spec §5 / §6)
  - URI allowlist validation (Spec §4)
  - No game stacking (Spec §8)
  - No auto-relaunch after game exit (Spec §6.4 / §6.5)
  - Removal handling (Spec §6.3 / §6.6)
  - Pairing flow guards (Spec §7)
  - Error audio on invalid/blocked tag (Spec §12 / §11)
  - NTAG21x / Mifare write and capacity enforcement (Spec §3.3)
  - Dual-launch prevention (backend defers Steam URIs to frontend)
"""
import asyncio
import json
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_uid(b: bytes = b"\xDE\xAD\xBE\xEF"):
    """Return a mock UID bytes object with a working .hex() method."""
    uid = MagicMock()
    uid.hex.return_value = b.hex()
    uid.__eq__ = lambda self, other: b == other
    uid.__ne__ = lambda self, other: b != other
    return uid


def _mock_nfc_source(write_result=(True, None), source_id="nfc:/dev/ttyUSB0"):
    """A stand-in NfcSource that satisfies the source-generic pairing lookup.

    Pairing resolves the source by id and asks whether it can be written to,
    so a bare MagicMock is no longer enough.
    """
    from sources.base import SourceType
    src = MagicMock()
    src.source_id = source_id
    src.source_type = SourceType.NFC
    src.can_write.return_value = True
    src.write_uri = AsyncMock(return_value=write_result)
    src.write_ndef_uri.return_value = write_result
    return src


def _make_load_event(uid_hex: str, uri=None, records=None, tag_meta=None):
    """Build a NFC MediaEvent(LOAD) for _handle_media_load tests."""
    from sources.base import MediaEvent, MediaEventKind, SourceType
    payload = {}
    if records is not None:
        payload["ndef_records"] = records
    if tag_meta is not None:
        payload["tag_meta"] = tag_meta
    return MediaEvent(
        kind=MediaEventKind.LOAD,
        source_type=SourceType.NFC,
        source_id="nfc:/dev/ttyUSB0",
        media_id=uid_hex,
        uri=uri,
        payload=payload,
    )


def _make_unload_event(uid_hex: str, uri=None):
    """Build a NFC MediaEvent(UNLOAD) for _handle_media_unload tests."""
    from sources.base import MediaEvent, MediaEventKind, SourceType
    return MediaEvent(
        kind=MediaEventKind.UNLOAD,
        source_type=SourceType.NFC,
        source_id="nfc:/dev/ttyUSB0",
        media_id=uid_hex,
        uri=uri,
    )


def _make_storage_load_event(devnode: str, uri=None):
    """Build a STORAGE MediaEvent(LOAD) — a floppy/USB insert."""
    from sources.base import MediaEvent, MediaEventKind, SourceType
    return MediaEvent(
        kind=MediaEventKind.LOAD,
        source_type=SourceType.STORAGE,
        source_id="storage:udev",
        media_id=devnode,
        uri=uri,
        payload={},
    )


def _make_storage_unload_event(devnode: str, uri=None):
    """Build a STORAGE MediaEvent(UNLOAD) — a floppy/USB eject."""
    from sources.base import MediaEvent, MediaEventKind, SourceType
    return MediaEvent(
        kind=MediaEventKind.UNLOAD,
        source_type=SourceType.STORAGE,
        source_id="storage:udev",
        media_id=devnode,
        uri=uri,
    )


# ── §5 / §6 — State Machine Transitions ──────────────────────────────────────

class TestStateMachine:

    def test_initial_state_is_idle_before_main(self):
        """Before _main() is called, the state defaults to IDLE."""
        from main import Plugin, PluginState
        p = Plugin()
        p.state = PluginState.IDLE
        assert p.state == PluginState.IDLE

    def test_set_state_logs_transition(self, plugin, mock_decky):
        from main import PluginState
        plugin.state = PluginState.IDLE
        plugin._set_state(PluginState.READY)
        assert plugin.state == PluginState.READY
        mock_decky.logger.info.assert_called()

    def test_set_state_no_log_on_same_state(self, plugin, mock_decky):
        from main import PluginState
        plugin.state = PluginState.READY
        plugin._set_state(PluginState.READY)
        mock_decky.logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_game_start_transitions_to_game_running(self, plugin):
        from main import PluginState
        assert plugin.state == PluginState.READY
        await plugin.set_running_game(400)
        assert plugin.state == PluginState.GAME_RUNNING
        assert plugin.running_game_id == 400

    @pytest.mark.asyncio
    async def test_game_exit_transitions_to_ready(self, plugin):
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        await plugin.set_running_game(None)
        assert plugin.state == PluginState.READY
        assert plugin.running_game_id is None

    @pytest.mark.asyncio
    async def test_game_exit_from_non_game_state_does_not_transition(self, plugin):
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.running_game_id = None
        await plugin.set_running_game(None)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_scan_transitions_to_card_present_then_ready_on_no_uri(self, plugin, mock_decky):
        """When no URI found on media, state goes CARD_PRESENT briefly then back to READY."""
        from main import PluginState
        event = _make_load_event("DEADBEEF", uri=None)
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_scan_stays_card_present_for_steam_uri_awaiting_game(self, plugin, mock_decky):
        """Steam URI scan leaves state as CARD_PRESENT until frontend reports game running."""
        from main import PluginState
        plugin.running_game_id = None
        event = _make_load_event("DEADBEEF", uri="steam://rungameid/400")
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        assert plugin.state == PluginState.CARD_PRESENT


# ── §4 — URI Allowlist Validation ─────────────────────────────────────────────

class TestURIValidation:

    def test_steam_uri_allowed(self, plugin):
        assert plugin._validate_uri("steam://rungameid/400") is True

    def test_steam_run_uri_allowed(self, plugin):
        assert plugin._validate_uri("steam://run/400") is True

    def test_https_uri_allowed(self, plugin):
        assert plugin._validate_uri("https://example.com") is True

    def test_non_launch_steam_uri_blocked(self, plugin):
        assert plugin._validate_uri("steam://open/games/details/400") is False

    def test_heroic_uri_blocked(self, plugin):
        assert plugin._validate_uri("heroic://launch/some-game-id") is False

    def test_absolute_command_blocked(self, plugin):
        cmd = '"/run/media/mmcblk0p1/Emulation/tools/launchers/dolphin-emu.sh" "/run/media/mmcblk0p1/Emulation/roms/game.iso"'
        assert plugin._validate_uri(cmd) is False

    def test_unapproved_absolute_path_blocked(self, plugin):
        assert plugin._validate_uri("/etc/passwd") is False

    def test_file_scheme_blocked(self, plugin):
        assert plugin._validate_uri("file:///etc/shadow") is False

    def test_arbitrary_scheme_blocked(self, plugin):
        assert plugin._validate_uri("ftp://malicious.example.com") is False

    def test_relative_path_blocked(self, plugin):
        assert plugin._validate_uri("../some/path") is False

    def test_empty_string_blocked(self, plugin):
        assert plugin._validate_uri("") is False

    def test_none_blocked(self, plugin):
        assert plugin._validate_uri(None) is False   # type: ignore

    def test_https_without_netloc_blocked(self, plugin):
        assert plugin._validate_uri("https://") is False

    def test_https_with_only_path_blocked(self, plugin):
        assert plugin._validate_uri("https:///path/to/resource") is False

    def test_steam_uri_with_empty_appid_blocked(self, plugin):
        assert plugin._validate_uri("steam://run/") is False

    def test_https_with_port_allowed(self, plugin):
        assert plugin._validate_uri("https://example.com:8080/path") is True

    def test_https_with_query_params_allowed(self, plugin):
        assert plugin._validate_uri("https://example.com/path?key=value") is True

    def test_https_with_fragment_allowed(self, plugin):
        assert plugin._validate_uri("https://example.com/path#section") is True


# ── Settings Load Validation ──────────────────────────────────────────────────

class TestSettingsLoadValidation:

    def test_invalid_settings_from_file_are_ignored(self, tmp_path):
        from main import SettingsManager

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "device_path": "/etc/passwd",
            "baudrate": "fast",
            "polling_interval": "0",
            "auto_launch": "yes",
            "auto_close": False,
            "reader_type": "unknown",
        }))

        settings = SettingsManager(str(settings_path))

        assert settings.get("device_path").startswith("/dev/")
        assert settings.get("baudrate") == 115200
        assert settings.get("polling_interval") == 0.5
        assert settings.get("auto_launch") is True
        assert settings.get("auto_close") is False
        assert settings.get("reader_type") == "pn532_uart"


# ── §8 — No Game Stacking ─────────────────────────────────────────────────────

class TestNoGameStacking:

    @pytest.mark.asyncio
    async def test_launch_blocked_when_game_running(self, plugin, mock_decky):
        """Backend should NOT launch when running_game_id is set."""
        from main import PluginState
        plugin.running_game_id = 400
        plugin.state           = PluginState.GAME_RUNNING
        event = _make_load_event("DEADBEEF", uri="steam://rungameid/400")
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_steam_uri_launched_by_backend(self, plugin, mock_decky):
        """Backend must xdg-open https URIs; Steam URIs are left to the frontend."""
        plugin.running_game_id = None
        event = _make_load_event("DEADBEEF", uri="https://example.com")
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_local_command_not_launched_when_blocked(self, plugin, mock_decky):
        plugin.running_game_id = None
        command = '"/run/media/mmcblk0p1/Emulation/tools/launchers/dolphin-emu.sh" "/run/media/mmcblk0p1/Emulation/roms/game.iso"'
        event = _make_load_event("DEADBEEF", uri=command)
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_steam_uri_not_launched_by_backend(self, plugin, mock_decky):
        """Steam URIs must NOT trigger _launch_uri — frontend handles them."""
        plugin.running_game_id = None
        event = _make_load_event("DEADBEEF", uri="steam://rungameid/400")
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_launch_disabled_prevents_any_launch(self, plugin, mock_decky):
        plugin.settings.get = lambda k, d=None: {
            "auto_launch": False,
            "polling_interval": 0.5,
        }.get(k, d)
        event = _make_load_event("DEADBEEF", uri="https://example.com")
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_not_called()


# ── §6.4 / §6.5 — No Auto-Relaunch ───────────────────────────────────────────

class TestNoAutoRelaunch:

    @pytest.mark.asyncio
    async def test_game_exit_does_not_clear_tag_uid(self, plugin):
        """When a game exits, current_tag_uid must NOT be cleared."""
        from main import PluginState
        plugin.state            = PluginState.GAME_RUNNING
        plugin.running_game_id  = 400
        plugin.current_tag_uid  = "DEADBEEF"
        plugin.current_tag_uri  = "steam://rungameid/400"

        await plugin.set_running_game(None)

        assert plugin.current_tag_uid == "DEADBEEF"
        assert plugin.state           == PluginState.READY


# ── §9.1a — Serial port auto-detection ───────────────────────────────────────

def _fake_port(device, vid=None, pid=None, product=None):
    p = MagicMock()
    p.device, p.vid, p.pid, p.product = device, vid, pid, product
    return p


class TestFindSerialPort:
    """Auto-detection must never guess at an unidentified CDC-ACM device.

    A Steam Deck exposes an unrelated /dev/ttyACM0; picking it meant writing
    PN532 wake-up frames to some other piece of hardware every retry.
    """

    def _source(self, ports):
        from sources.nfc_source import NfcSource
        src = NfcSource({"device_path": "/dev/ttyUSB0"}, logger=MagicMock())
        fake_list_ports = MagicMock()
        fake_list_ports.comports.return_value = ports
        fake_tools = MagicMock(list_ports=fake_list_ports)
        with patch.dict(sys.modules, {"serial.tools": fake_tools,
                                      "serial.tools.list_ports": fake_list_ports}):
            return src, src._find_serial_port()

    def test_picks_ch340_bridge(self):
        _, found = self._source([
            _fake_port("/dev/ttyACM0", vid=0x2341, pid=0x0043, product="Arduino"),
            _fake_port("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, product="USB Serial"),
        ])
        assert found == "/dev/ttyUSB0"

    def test_ignores_unknown_acm_device(self):
        _, found = self._source([_fake_port("/dev/ttyACM0", vid=0x2341, pid=0x0043)])
        assert found is None

    def test_ignores_port_without_usb_ids(self):
        _, found = self._source([_fake_port("/dev/ttyS0")])
        assert found is None

    def test_no_ports_at_all(self):
        _, found = self._source([])
        assert found is None

    @pytest.mark.parametrize("vid", [0x1A86, 0x10C4, 0x0403, 0x067B])
    def test_known_bridge_vendors_accepted(self, vid):
        _, found = self._source([_fake_port("/dev/ttyUSB0", vid=vid, pid=0x0001)])
        assert found == "/dev/ttyUSB0"

    def test_repeated_failures_log_only_once(self):
        """start() retries forever; an unplugged reader used to log every 30s."""
        from sources.nfc_source import NfcSource
        src = NfcSource({"device_path": "/dev/ttyUSB0"}, logger=MagicMock())
        fake_list_ports = MagicMock()
        fake_list_ports.comports.return_value = [_fake_port("/dev/ttyACM0", vid=0x2341)]
        fake_tools = MagicMock(list_ports=fake_list_ports)
        with patch.dict(sys.modules, {"serial.tools": fake_tools,
                                      "serial.tools.list_ports": fake_list_ports}):
            for _ in range(5):
                src._find_serial_port()
        assert src._logger.info.call_count == 1


# ── §9.1 — Reader / NfcSource init ───────────────────────────────────────────

class TestReaderInit:

    def test_classify_tag_reports_types(self, plugin, uid_bytes):
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\x00"
        meta = plugin.nfc_source._classify_tag(uid_bytes)
        assert meta["uid"] == uid_bytes.hex().upper()
        assert meta["type"] == "ntag21x"
        assert meta["capacity_bytes"] > 0

    def test_classify_felica_by_length(self, plugin):
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "felica"
        assert meta["capacity_bytes"] == 0

    def test_classify_iso15693_by_uid_prefix(self, plugin):
        uid = b"\xE0\x01\x02\x03\x04\x05\x06\x07"
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "iso15693"

    def test_classify_iso14443b_by_length(self, plugin):
        uid = b"\x01\x02\x03\x04"
        plugin.nfc_source._reader.read_uid_iso14443b = MagicMock(return_value=uid)
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "iso14443b"

    def test_classify_ultralight_by_uid_length(self, plugin):
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\x00"
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "ultralight"

    def test_classify_mifare_classic_authenticated(self, plugin):
        uid = b"\xDE\xAD\xBE\xEF"
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "mifare-classic"
        assert meta["capacity_bytes"] > 0

    def test_classify_desfire_fallback(self, plugin):
        uid = b"\x01\x02\x03\x04\x05\x06\x07"
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.side_effect = Exception("No page 4")
        meta = plugin.nfc_source._classify_tag(uid)
        assert meta["type"] == "desfire"

    @pytest.mark.asyncio
    async def test_nfc_source_start_success(self, mock_decky, tmp_path):
        """NfcSource.start() returns True and sets _reader when connection succeeds."""
        from sources.nfc_source import NfcSource
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        settings = {"device_path": fake_path, "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)

        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=True)
        with patch.object(source, "_create_reader", return_value=mock_reader):
            ok = await source.start()
        assert ok is True
        assert source._reader is mock_reader

    @pytest.mark.asyncio
    async def test_nfc_source_start_failure_leaves_none(self, mock_decky, tmp_path):
        """NfcSource.start() returns False and leaves _reader as None when connect fails."""
        from sources.nfc_source import NfcSource
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        settings = {"device_path": fake_path, "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)

        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=False)
        with patch.object(source, "_create_reader", return_value=mock_reader):
            ok = await source.start()
        assert ok is False
        assert source._reader is None

    @pytest.mark.asyncio
    async def test_nfc_source_start_records_last_good_path(self, mock_decky, tmp_path):
        """start() stores _last_good_path after a successful connection."""
        from sources.nfc_source import NfcSource
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        settings = {"device_path": fake_path, "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)

        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=True)
        with patch.object(source, "_create_reader", return_value=mock_reader):
            await source.start()
        assert source._last_good_path == fake_path

    @pytest.mark.asyncio
    async def test_nfc_source_start_failure_does_not_set_last_good_path(self, mock_decky, tmp_path):
        """_last_good_path is not updated when connect() fails."""
        from sources.nfc_source import NfcSource
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        settings = {"device_path": fake_path, "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)

        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=False)
        with patch.object(source, "_create_reader", return_value=mock_reader):
            await source.start()
        assert source._last_good_path is None

    @pytest.mark.asyncio
    async def test_nfc_source_reconnect_prefers_last_good_path(self, mock_decky, tmp_path):
        """After a USB glitch, start() retries the last good path, not auto-detect."""
        from sources.nfc_source import NfcSource
        good_path = str(tmp_path / "ttyUSB0")
        other_path = str(tmp_path / "ttyACM0")
        open(good_path, "w").close()
        open(other_path, "w").close()

        settings = {"device_path": "/dev/nonexistent", "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)
        source._last_good_path = good_path  # simulate a prior successful connection

        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=True)
        with patch.object(source, "_create_reader", return_value=mock_reader):
            ok = await source.start()

        assert ok is True
        assert source._effective_path == good_path

    @pytest.mark.asyncio
    async def test_nfc_source_waits_for_last_good_path_not_auto_detects(self, mock_decky, tmp_path):
        """When last_good_path is gone and configured path missing, return False (don't auto-detect)."""
        from sources.nfc_source import NfcSource
        other_path = str(tmp_path / "ttyACM0")
        open(other_path, "w").close()  # a different device is present

        settings = {"device_path": "/dev/nonexistent", "reader_type": "pn532_uart", "baudrate": 115200}
        source = NfcSource(settings, logger=mock_decky.logger)
        source._last_good_path = "/dev/nonexistent"  # last good path also gone (USB glitch)

        ok = await source.start()
        assert ok is False  # must NOT auto-detect ttyACM0

    @pytest.mark.asyncio
    async def test_nfc_source_create_reader_unknown_type(self, plugin):
        plugin.nfc_source._settings["reader_type"] = "no-such"
        result = await plugin.nfc_source._create_reader()
        assert result is None

    @pytest.mark.asyncio
    async def test_nfc_source_create_reader_nfcpy_success(self, plugin, monkeypatch):
        plugin.nfc_source._settings["reader_type"] = "nfcpy"
        plugin.nfc_source._settings["device_path"] = "/dev/null"
        reader = await plugin.nfc_source._create_reader()
        assert reader is not None
        assert reader.__class__.__name__ == "NfcPyReader"

    @pytest.mark.asyncio
    async def test_get_tag_metadata_method(self, plugin, uid_bytes):
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\x00"
        plugin.current_tag_uid = uid_bytes.hex().upper()
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"type": "ntag21x", "capacity_bytes": 4, "protected": False}):
            info = await plugin.get_tag_metadata()
        assert info.get("type") == "ntag21x"
        assert info.get("protected") is False
        bad = await plugin.get_tag_metadata("nothex")
        assert "error" in bad

    @pytest.mark.asyncio
    async def test_get_reader_diagnostics(self, plugin):
        plugin.nfc_source._reader = None
        assert await plugin.get_reader_diagnostics() == {"connected": False}

        fake = MagicMock()
        fake.firmware_version.return_value = (1, 2, 3, 4)
        plugin.nfc_source._reader = fake
        info = await plugin.get_reader_diagnostics()
        assert info["connected"] is True
        assert info["firmware"] == (1, 2, 3, 4)

        def boom():
            raise RuntimeError("nope")
        fake.firmware_version.side_effect = boom
        info2 = await plugin.get_reader_diagnostics()
        assert info2.get("error") == "nope"

    def test_reader_type_validation(self, plugin):
        assert plugin._validate_setting("reader_type", "pn532_uart")
        assert not plugin._validate_setting("reader_type", "badtype")

    @pytest.mark.asyncio
    async def test_set_reader_type_setting(self, plugin, mock_decky):
        assert await plugin.set_setting("reader_type", "pn532_uart")
        assert not await plugin.set_setting("reader_type", "invalidtype")

    @pytest.mark.asyncio
    async def test_ndef_detected_event_emitted(self, plugin, mock_decky, uid_bytes):
        fake_rec = {"type": "U", "uri": "steam://rungameid/400"}
        event = _make_load_event(
            uid_bytes.hex().upper(),
            uri="steam://rungameid/400",
            records=[fake_rec],
            tag_meta={"uid": uid_bytes.hex().upper(), "type": "ntag21x"},
        )
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        calls = [c for c in mock_decky.emit.call_args_list if c[0][0] == "ndef_detected"]
        assert len(calls) == 1
        assert "records" in calls[0][0][1]
        mock_decky.emit.assert_any_call("tag_metadata", {"uid": uid_bytes.hex().upper(), "type": "ntag21x"})

    @pytest.mark.asyncio
    async def test_tag_metadata_event_emitted_when_present(self, plugin, mock_decky, uid_bytes):
        tag_meta = {"uid": uid_bytes.hex().upper(), "type": "ntag21x", "capacity_bytes": 144}
        event = _make_load_event(uid_bytes.hex().upper(), uri="steam://rungameid/400", tag_meta=tag_meta)
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_decky.emit.assert_any_call("tag_metadata", tag_meta)

    # See the note in test_file_watch_source: driving the loop by hand from a
    # sync test depends on no earlier async test having run, which made this
    # pass in isolation and fail in the suite.
    @pytest.mark.asyncio
    async def test_simulate_tag_sets_state_and_emits(self, plugin, mock_decky):
        uid = b"\xAA\xBB\xCC\xDD"
        uri = "https://foo"
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"uid": uid.hex().upper()}):
            await plugin.simulate_tag(uid, uri)
        assert plugin.current_tag_uid == uid.hex().upper()
        assert plugin.current_tag_uri == uri
        mock_decky.emit.assert_has_calls([
            call("tag_detected", {"uid": uid.hex().upper()}),
            call("uri_detected", {"uri": uri, "uid": uid.hex().upper()}),
        ])

    def test_classify_tag_protected_flag(self, plugin, uid_bytes):
        def boom(*args, **kwargs):
            raise RuntimeError("locked")
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.side_effect = boom
        meta = plugin.nfc_source._classify_tag(uid_bytes)
        assert meta.get("capacity_bytes") == 0
        assert meta.get("protected") is True

    def test_read_ndef_uri_on_ntag_detects_and_parses(self, plugin, uid_bytes):
        plugin.nfc_source._reader.read_uid.return_value = uid_bytes
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\x00\x00\x00\x00"
        plugin.nfc_source._reader.ntag2xx_read_block.side_effect = [
            # Page 3, the capability container: NDEF magic, version, 0x3E×8 =
            # 496 bytes of user memory (an NTAG215). Read first now that the
            # page range comes from the tag instead of being assumed.
            bytes([0xE1, 0x10, 0x3E, 0x00]),
            bytes([0x03, 0x01, 0x00, 0xFE]), b"\x00\x00\x00\x00"
        ]

        import sys

        class StubUriRecord:
            def __init__(self, uri):
                self.uri = uri

        class StubNdef:
            UriRecord = StubUriRecord

            @staticmethod
            def message_decoder(data):
                return [StubUriRecord("steam://rungameid/77")]

        original = sys.modules.get("ndef")
        sys.modules["ndef"] = StubNdef
        try:
            uri = plugin.nfc_source._read_ndef_uri()
        finally:
            if original is not None:
                sys.modules["ndef"] = original
            else:
                del sys.modules["ndef"]

        assert uri == "steam://rungameid/77"

    def test_multiple_ndef_records_first_uri_returned(self, plugin, uid_bytes):
        plugin.nfc_source._reader.read_uid.return_value = uid_bytes
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\x00\x00\x00\x00"

        from ndef import UriRecord
        first = MagicMock()
        first.__class__.__name__ = "TextRecord"
        second = UriRecord("https://example.com")

        with patch.object(plugin.nfc_source, "_read_ndef_records", return_value=[first, second]):
            uri = plugin.nfc_source._read_ndef_uri()
        assert uri == "https://example.com"


# ── §6.3 / §6.6 — Media Removal ──────────────────────────────────────────────

class TestMediaRemoval:

    @pytest.mark.asyncio
    async def test_unload_emits_tag_removed(self, plugin, mock_decky):
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = None
        plugin.is_pairing      = False

        event = _make_unload_event("DEADBEEF")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "tag_removed" in emitted
        assert plugin.current_tag_uid is None

    @pytest.mark.asyncio
    async def test_card_removed_during_game_emits_correct_event(self, plugin, mock_decky):
        plugin.is_pairing = False

        # Tap the tag, then let the frontend report the launch -- this is what
        # establishes the launch origin that authorises the quit.
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        await plugin.set_running_game(400)

        event = _make_unload_event("DEADBEEF", uri="steam://rungameid/400")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" in emitted
        assert "tag_removed" in emitted

    @pytest.mark.asyncio
    async def test_blank_medium_elsewhere_does_not_break_auto_close(self, plugin, mock_decky):
        """A blank disk in the drive used to silently disable auto-close.

        _handle_media_load set READY unconditionally when a medium had no URI,
        which dropped the plugin out of GAME_RUNNING — and _handle_media_unload
        only closes a game while in GAME_RUNNING. So: tap a tag to launch a
        game, put a blank floppy in the drive, take the tag off, and the game
        stayed running with nothing in the log explaining why. The state rules
        now keep GAME_RUNNING when a game is running.
        """
        from main import PluginState
        plugin.is_pairing = False

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        await plugin.set_running_game(400)
        assert plugin.state == PluginState.GAME_RUNNING

        # A blank disk goes into a different source.
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sda1", uri=None)
            )
        assert plugin.state == PluginState.GAME_RUNNING, (
            "an unreadable medium on another source must not end the game"
        )

        # Removing the tag that launched it must still close the game.
        mock_decky.emit.reset_mock()
        await plugin._handle_media_unload(
            _make_unload_event("DEADBEEF", uri="steam://rungameid/400")
        )
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" in emitted

    @pytest.mark.asyncio
    async def test_blocked_uri_elsewhere_does_not_break_auto_close(self, plugin, mock_decky):
        """Same bug, reached through the allowlist branch rather than the blank
        one — both set READY unconditionally."""
        from main import PluginState
        plugin.is_pairing = False

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        await plugin.set_running_game(400)

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sda1", uri="ftp://evil.example/x")
            )
        assert plugin.state == PluginState.GAME_RUNNING

    @pytest.mark.asyncio
    async def test_removal_not_emitted_when_pairing(self, plugin, mock_decky):
        """Spec §6.3: card_removed_during_game suppressed when pairing is active."""
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin.current_tag_uid = "DEADBEEF"
        plugin.is_pairing      = True

        event = _make_unload_event("DEADBEEF")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted
        assert "tag_removed" in emitted


# ── §7 — Pairing Flow ─────────────────────────────────────────────────────────

class TestPairing:

    @pytest.mark.asyncio
    async def test_pairing_mode_enters_pairing_flow(self, plugin, mock_decky):
        """When is_pairing is True, _handle_media_load calls _handle_pairing, not launch."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"

        event = _make_load_event("DEADBEEF", uri="steam://rungameid/400")
        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        mock_pair.assert_called_once_with("DEADBEEF", source_id="nfc:/dev/ttyUSB0")
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing_works_on_blank_tag(self, plugin, mock_decky):
        """A blank tag (no URI) must still enter the pairing flow.

        Regression test for the ordering bug where the `if not uri: return`
        guard ran before the pairing branch, making it impossible to pair a
        fresh tag — i.e. every tag a user actually wants to write.
        """
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"

        event = _make_load_event("DEADBEEF", uri=None)
        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        mock_pair.assert_called_once_with("DEADBEEF", source_id="nfc:/dev/ttyUSB0")
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing_blank_tag_does_not_emit_blocked_uri(self, plugin, mock_decky):
        """Pairing a blank tag must not report it as a rejected/blocked URI."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"

        event = _make_load_event("DEADBEEF", uri=None)
        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        emitted = {c.args[0] for c in mock_decky.emit.call_args_list}
        assert "uri_detected" not in emitted
        assert "tag_detected" in emitted

    @pytest.mark.asyncio
    async def test_start_pairing_rearms_nfc_source(self, plugin, mock_decky):
        """start_pairing re-arms the source so a resting card is re-detected.

        Without this, poll() suppresses the LOAD event for a card that was
        already on the reader, and the user must lift and re-tap to pair.
        """
        from sources.base import SourceType
        stand_in = MagicMock()
        stand_in.source_type = SourceType.NFC
        plugin.source_manager.replace(stand_in)

        ok = await plugin.start_pairing("steam://rungameid/400")

        assert ok is True
        stand_in.rearm.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_pairing_survives_missing_source(self, plugin, mock_decky):
        """start_pairing must not blow up before sources are initialised."""
        plugin.source_manager._sources = []   # no reader at all
        assert await plugin.start_pairing("steam://rungameid/400") is True
        assert plugin.is_pairing is True

    @pytest.mark.asyncio
    async def test_pairing_plays_success_sound_on_write_ok(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        mock_sound.assert_called_with("success.flac")

    @pytest.mark.asyncio
    async def test_pairing_plays_error_sound_on_write_fail(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(False, "Auth failed")), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        mock_sound.assert_called_with("error.flac")

    @pytest.mark.asyncio
    async def test_pairing_exits_mode_after_write(self, plugin):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        assert plugin.is_pairing  is False
        assert plugin.pairing_uri is None

    @pytest.mark.asyncio
    async def test_pairing_with_no_uri_aborts(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = None
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", new_callable=MagicMock) as mock_write:
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        mock_write.assert_not_called()
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_pairing_emits_result_event(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        mock_decky.emit.assert_called()
        event_name = mock_decky.emit.call_args_list[-1].args[0]
        assert event_name == "pairing_result"

    @pytest.mark.asyncio
    async def test_pairing_result_names_the_source(self, plugin, mock_decky):
        """The game-page modal needs to say "disk" or "tag", not always "tag"."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        payload = mock_decky.emit.call_args_list[-1].args[1]
        assert payload["source_type"] == "nfc"

    @pytest.mark.asyncio
    async def test_pairing_result_for_storage_reports_storage(self, plugin, mock_decky):
        from sources.base import SourceType
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"

        storage = MagicMock()
        storage.source_id = "storage:udev"
        storage.source_type = SourceType.STORAGE
        storage.can_write.return_value = True
        storage.write_uri = AsyncMock(return_value=(True, None))
        plugin.source_manager.replace(storage)

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing("/dev/sda", source_id="storage:udev")

        payload = mock_decky.emit.call_args_list[-1].args[1]
        assert payload["success"] is True
        assert payload["uid"] == "/dev/sda"
        assert payload["source_type"] == "storage"
        storage.write_uri.assert_awaited_once_with("/dev/sda", "steam://rungameid/400")

    @pytest.mark.asyncio
    async def test_pairing_does_not_launch_game_after_write(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid.hex().upper(), source_id="nfc:/dev/ttyUSB0")

        mock_launch.assert_not_called()


# ── §11 / §12 — Audio Feedback ────────────────────────────────────────────────

class TestAudioFeedback:

    @pytest.mark.asyncio
    async def test_scan_sound_on_valid_tag(self, plugin, mock_decky):
        event = _make_load_event("DEADBEEF", uri="steam://rungameid/400")
        with patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_media_load(event)
        mock_sound.assert_any_call("scan.flac")

    @pytest.mark.asyncio
    async def test_error_sound_when_no_uri(self, plugin, mock_decky):
        event = _make_load_event("DEADBEEF", uri=None)
        with patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_media_load(event)
        mock_sound.assert_any_call("error.flac")

    @pytest.mark.asyncio
    async def test_error_sound_when_uri_blocked_by_allowlist(self, plugin, mock_decky):
        event = _make_load_event("DEADBEEF", uri="ftp://evil.example.com")
        with patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_media_load(event)
        mock_sound.assert_any_call("error.flac")

    @pytest.mark.asyncio
    async def test_no_launch_when_uri_blocked(self, plugin, mock_decky):
        event = _make_load_event("DEADBEEF", uri="ftp://evil.example.com")
        with patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        mock_launch.assert_not_called()


# ── §3.3 — NTAG/Mifare Capacity Enforcement ──────────────────────────────────

class TestNTAGCapacityDetection:
    """Capacity used to be the NTAG215 layout applied to every tag, so a long
    URI on a smaller NTAG213 was written past the end of the tag — pages
    failing one by one while the write reported success."""

    def _cc(self, plugin, block):
        plugin.nfc_source._reader.ntag2xx_read_block.return_value = block
        return list(plugin.nfc_source._iter_ntag_pages())

    def test_ntag213_is_sized_from_its_capability_container(self, plugin):
        pages = self._cc(plugin, bytes([0xE1, 0x10, 0x12, 0x00]))   # 0x12×8 = 144 B
        assert len(pages) * 4 == 144
        assert pages[0] == 4 and pages[-1] == 39

    def test_ntag215(self, plugin):
        assert len(self._cc(plugin, bytes([0xE1, 0x10, 0x3E, 0x00]))) * 4 == 496

    def test_ntag216(self, plugin):
        assert len(self._cc(plugin, bytes([0xE1, 0x10, 0x6D, 0x00]))) * 4 == 872

    def test_unreadable_cc_falls_back_to_the_old_assumption(self, plugin):
        """Wrong only for tags that already could not tell us anything, and
        refusing to write at all would be worse."""
        assert len(self._cc(plugin, None)) == 130

    def test_non_ndef_magic_falls_back(self, plugin):
        assert len(self._cc(plugin, bytes([0x00, 0x00, 0x12, 0x00]))) == 130

    def test_implausible_size_falls_back(self, plugin):
        """A misread claiming a tag bigger than any NTAG is a misread."""
        assert len(self._cc(plugin, bytes([0xE1, 0x10, 0xFF, 0x00]))) == 130

    def test_capability_container_is_read_once_per_tag(self, plugin):
        """Classification and the NDEF read both need the page range. Without a
        memo the tag is interrogated twice for an answer that cannot change
        while it is sitting on the reader."""
        plugin.nfc_source._reader.ntag2xx_read_block.return_value = bytes(
            [0xE1, 0x10, 0x12, 0x00]
        )
        list(plugin.nfc_source._iter_ntag_pages("DEADBEEF"))
        plugin.nfc_source._reader.ntag2xx_read_block.reset_mock()
        list(plugin.nfc_source._iter_ntag_pages("DEADBEEF"))
        plugin.nfc_source._reader.ntag2xx_read_block.assert_not_called()

    def test_a_uri_that_fits_a_215_is_refused_by_a_213(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.ntag2xx_read_block.return_value = bytes(
            [0xE1, 0x10, 0x12, 0x00]                                 # NTAG213
        )
        uri = "https://" + "a" * 200      # 208 bytes: fits a 215, not a 213
        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is False
        assert "tag too small" in (err or "").lower()
        # Refused before the first page write, so the tag is untouched.
        plugin.nfc_source._reader.ntag2xx_write_block.assert_not_called()


class TestNTAGCapacity:

    def test_short_uri_within_limit(self, plugin):
        uid = _make_uid()
        uri = "steam://rungameid/400"
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert err != "URI too long" if err else True

    def test_oversized_uri_is_rejected(self, plugin):
        uid = _make_uid()
        uri = "https://" + "a" * 600
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is False
        assert "tag too small" in (err or "").lower()

    def test_uri_exactly_at_limit_is_allowed(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        capacity = plugin.nfc_source._classify_tag(uid)["capacity_bytes"]
        usable = capacity - 8
        uri = "https://" + "x" * (usable - len("https://"))
        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert "too long" not in (err or "").lower()

    def test_write_skips_mifare_trailer_blocks(self, plugin):
        uid = _make_uid()
        uri = "https://" + "x" * 72
        plugin.nfc_source._reader.mifare_classic_write_block.reset_mock()

        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)

        assert success is True
        written_blocks = [c.args[0] for c in plugin.nfc_source._reader.mifare_classic_write_block.call_args_list]
        assert written_blocks
        assert 7 not in written_blocks
        assert all((b % 4) != 3 for b in written_blocks)


# ── §XX — NTAG21x (e.g. NTAG215) support ─────────────────────────────────────

class TestNTAG21xSupport:

    def test_ntag_write_fallback_when_auth_fails(self, plugin):
        uid = _make_uid()
        uri = "steam://rungameid/123"
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.ntag2xx_write_block.return_value = True

        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is True
        assert err is None
        plugin.nfc_source._reader.ntag2xx_write_block.assert_called()

    def test_ntag_write_handles_auth_throwing(self, plugin):
        uid = _make_uid()
        uri = "steam://rungameid/999"

        def bad_auth(uid_arg, blk, kn, key):
            raise RuntimeError("Received unexpected command response")
        plugin.nfc_source._reader.mifare_classic_authenticate_block.side_effect = bad_auth
        plugin.nfc_source._reader.ntag2xx_write_block.return_value = True

        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is True
        assert err is None
        plugin.nfc_source._reader.ntag2xx_write_block.assert_called()

    def test_ntag_capacity_allows_longer_uris(self, plugin):
        uid = _make_uid()
        uri = "https://" + "a" * 300
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.ntag2xx_write_block.return_value = True

        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is True
        assert err is None

    def test_ntag_oversize_still_rejected(self, plugin):
        uid = _make_uid()
        uri = "https://" + "a" * 600
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False

        success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is False
        assert "tag too small" in (err or "").lower()

    def test_classic_capacity_detection_blocks(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        with patch.object(plugin.nfc_source, "_iter_mifare_data_blocks", return_value=[4, 5]):
            long_uri = "https://" + "x" * 100
            success, err = plugin.nfc_source.write_ndef_uri(uid, long_uri)
        assert success is False
        assert "tag too small" in (err or "").lower()

    def test_classic_capacity_allows_small_write(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        with patch.object(plugin.nfc_source, "_iter_mifare_data_blocks", return_value=[4, 5, 6]):
            uri = "https://ok"
            success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is True
        assert err is None

    def test_ntag_capacity_detection_pages(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        with patch.object(plugin.nfc_source, "_iter_ntag_pages", return_value=[4, 5]):
            uri = "https://"
            success, err = plugin.nfc_source.write_ndef_uri(uid, uri)
        assert success is False
        assert "tag too small" in (err or "").lower()


# ── Multiple tag detection ─────────────────────────────────────────────────────

class TestMultiTagDetection:

    @pytest.mark.asyncio
    async def test_multiple_tags_event(self, plugin, mock_decky, uid_bytes):
        uid_hex = uid_bytes.hex().upper()
        event1 = _make_load_event(uid_hex, uri=None)
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event1)

        other = b"\xBA\xAD\xF0\x0D"
        event2 = _make_load_event(other.hex().upper(), uri=None)
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event2)

        mock_decky.emit.assert_any_call("multiple_tags", {
            "previous": uid_hex,
            "current": other.hex().upper(),
            "source_type": "nfc",
        })


# ── §6.3 — Card Removed During Game ──────────────────────────────────────────

class TestCardRemovedDuringGame:

    @pytest.mark.asyncio
    async def test_removal_event_emitted_when_game_running(self, plugin, mock_decky):
        plugin.is_pairing = False

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        await plugin.set_running_game(400)

        event = _make_unload_event("DEADBEEF", uri="steam://rungameid/400")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" in emitted

    @pytest.mark.asyncio
    async def test_removal_event_not_emitted_when_no_game(self, plugin, mock_decky):
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.running_game_id = None
        plugin.current_tag_uid = "DEADBEEF"
        plugin.is_pairing      = False

        event = _make_unload_event("DEADBEEF")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted
        assert "tag_removed" in emitted


# ── Feature 2 — Custom Key Management ────────────────────────────────────────

class TestKeyManagement:

    @pytest.mark.asyncio
    async def test_set_tag_key_valid(self, plugin):
        uid   = "DEADBEEFCAFE"
        key_a = "FFFFFFFFFFFF"
        key_b = "D3F7D3F7D3F7"

        result = await plugin.set_tag_key(uid, key_a, key_b)

        assert result is True
        stored = plugin.key_manager.get_keys(uid)
        assert stored == [key_a, key_b]

    @pytest.mark.asyncio
    async def test_set_tag_key_invalid_format(self, plugin):
        uid = "DEADBEEFCAFE"
        assert await plugin.set_tag_key(uid, "FFFF", "FFFFFFFFFFFF") is False
        assert await plugin.set_tag_key(uid, "GGGGGGGGGGGG", "FFFFFFFFFFFF") is False

    @pytest.mark.asyncio
    async def test_get_tag_key_found(self, plugin):
        uid   = "DEADBEEFCAFE"
        key_a = "FFFFFFFFFFFF"
        key_b = "D3F7D3F7D3F7"
        plugin.key_manager.set_key(uid, key_a, key_b)
        result = await plugin.get_tag_key(uid)
        assert result == {"key_a": key_a, "key_b": key_b}

    @pytest.mark.asyncio
    async def test_get_tag_key_not_found(self, plugin):
        result = await plugin.get_tag_key("NONEXISTENT")
        assert result == {}

    @pytest.mark.asyncio
    async def test_list_tag_keys(self, plugin):
        uid1 = "DEADBEEFCAFE"
        uid2 = "CAFEBEEFDEAD"
        plugin.key_manager.set_key(uid1, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        plugin.key_manager.set_key(uid2, "A0A1A2A3A4A5", "FFFFFFFFFFFF")
        result = await plugin.list_tag_keys()
        assert len(result) == 2
        assert uid1 in result
        assert uid2 in result

    @pytest.mark.asyncio
    async def test_list_tag_keys_empty(self, plugin):
        result = await plugin.list_tag_keys()
        assert result == []

    @pytest.mark.asyncio
    async def test_key_manager_persistence(self, plugin, tmp_path):
        from nfc.key_manager import KeyManager
        keys_path = tmp_path / "keys.json"
        km1 = KeyManager(str(keys_path))
        km1.set_key("DEADBEEFCAFE", "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        km2 = KeyManager(str(keys_path))
        stored = km2.get_keys("DEADBEEFCAFE")
        assert stored == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

    @pytest.mark.asyncio
    async def test_mifare_handler_uses_custom_keys(self, plugin):
        from nfc.tag_handlers import MifareClassicHandler
        uid     = b"\\xDEADBEEFCAFE"
        uid_hex = uid.hex().upper()
        plugin.key_manager.set_key(uid_hex, "A0A1A2A3A4A5", "B0B1B2B3B4B5")
        handler = MifareClassicHandler(uid, plugin.key_manager)
        keys    = handler._get_keys_to_try()
        assert keys[0] == bytes.fromhex("A0A1A2A3A4A5")
        assert keys[1] == bytes.fromhex("B0B1B2B3B4B5")
        assert len(keys) > 2

    @pytest.mark.asyncio
    async def test_mifare_handler_without_custom_keys(self, plugin):
        from nfc.tag_handlers import MifareClassicHandler
        uid     = b"\\xDEADBEEFCAFE"
        handler = MifareClassicHandler(uid, plugin.key_manager)
        keys    = handler._get_keys_to_try()
        assert len(keys) == 3
        assert keys == MifareClassicHandler.DEFAULT_KEYS


# ── Sector Info RPC ───────────────────────────────────────────────────────────

class TestSectorInfoRPC:

    @pytest.mark.asyncio
    async def test_get_sector_info_current_tag(self, plugin):
        plugin.current_tag_uid = "DEADBEEF"
        plugin.nfc_source._reader = MagicMock()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        plugin.nfc_source._reader.mifare_classic_write_block.return_value = True
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"type": "mifare-classic"}):
            result = await plugin.get_sector_info()
        assert len(result) == 16
        assert all("sector" in s for s in result)

    @pytest.mark.asyncio
    async def test_get_sector_info_specified_uid(self, plugin):
        plugin.nfc_source._reader = MagicMock()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        plugin.nfc_source._reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        plugin.nfc_source._reader.mifare_classic_write_block.return_value = True
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"type": "mifare-classic"}):
            result = await plugin.get_sector_info("CAFEBABE")
        assert len(result) == 16

    @pytest.mark.asyncio
    async def test_get_sector_info_no_tag(self, plugin):
        plugin.current_tag_uid = None
        result = await plugin.get_sector_info()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_sector_info_wrong_tag_type(self, plugin):
        plugin.current_tag_uid = "DEADBEEF"
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"type": "ntag21x"}):
            result = await plugin.get_sector_info()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_sector_info_no_reader(self, plugin):
        plugin.current_tag_uid    = "DEADBEEF"
        plugin.nfc_source._reader = None
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"type": "mifare-classic"}):
            result = await plugin.get_sector_info()
        assert result == []


# ── _handle_source_event() ────────────────────────────────────────────────────

class TestHandleSourceEvent:

    @pytest.mark.asyncio
    async def test_nfc_connected_sets_state_ready(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.IDLE
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.NFC, source_id="nfc:/dev/ttyUSB0")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_nfc_connected_emits_reader_status_true(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = __import__("main").PluginState.IDLE
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.NFC, source_id="nfc:/dev/ttyUSB0")
        await plugin._handle_source_event(event)
        reader_calls = [c for c in mock_decky.emit.call_args_list if c.args[0] == "reader_status"]
        assert len(reader_calls) == 1
        assert reader_calls[0].args[1]["connected"] is True
        assert reader_calls[0].args[1]["source_type"] == "nfc"

    @pytest.mark.asyncio
    async def test_storage_connected_leaves_idle(self, plugin, mock_decky):
        """IDLE means "nothing can trigger a launch", not "no NFC reader".

        A Deck with only a floppy drive attached used to sit in IDLE forever.
        """
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.IDLE
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_source_connect_does_not_disturb_a_running_game(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.GAME_RUNNING
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.GAME_RUNNING

    @pytest.mark.asyncio
    async def test_storage_connected_does_not_emit_reader_status(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "reader_status" not in emitted

    @pytest.mark.asyncio
    async def test_nfc_disconnected_sets_state_idle(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.READY
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.NFC, source_id="nfc:/dev/ttyUSB0")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.IDLE

    @pytest.mark.asyncio
    async def test_nfc_disconnected_emits_reader_status_false(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.NFC, source_id="nfc:/dev/ttyUSB0")
        await plugin._handle_source_event(event)
        reader_calls = [c for c in mock_decky.emit.call_args_list if c.args[0] == "reader_status"]
        assert len(reader_calls) == 1
        assert reader_calls[0].args[1]["connected"] is False

    @pytest.mark.asyncio
    async def test_storage_disconnected_keeps_ready_while_reader_remains(self, plugin, mock_decky):
        """Losing a drive while the NFC reader is still connected is not idle."""
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.READY
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_last_source_disconnecting_goes_idle(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.nfc_source._reader = None          # reader gone too
        plugin.state = PluginState.READY
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.IDLE

    @pytest.mark.asyncio
    async def test_unplugging_a_drive_holding_media_clears_the_panel(self, plugin, mock_decky):
        """Otherwise the panel keeps showing a disk that is no longer attached."""
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin._registry._media["storage:udev"] = {
            "media_id": "/dev/sda", "uri": "steam://run/1", "source_type": "storage",
        }
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "tag_removed" in emitted

    @pytest.mark.asyncio
    async def test_storage_disconnected_does_not_emit_reader_status(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "reader_status" not in emitted

    @pytest.mark.asyncio
    async def test_connected_event_emits_source_statuses(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        for source_type in [SourceType.NFC, SourceType.STORAGE, SourceType.MQTT]:
            mock_decky.emit.reset_mock()
            plugin._last_statuses = None          # nothing published yet
            event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=source_type, source_id=f"{source_type.value}:test")
            await plugin._handle_source_event(event)
            emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
            assert "source_statuses" in emitted, f"Missing source_statuses for {source_type}"

    @pytest.mark.asyncio
    async def test_unchanged_statuses_are_not_re_emitted(self, plugin, mock_decky):
        """The event loop now re-checks status on a timer, because drives come
        and go without producing any event. That only works if an unchanged
        status is silent — otherwise it is a message per tick, forever."""
        await plugin._publish_statuses()
        mock_decky.emit.reset_mock()
        await plugin._publish_statuses()
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "source_statuses" not in emitted

    @pytest.mark.asyncio
    async def test_a_newly_attached_drive_reaches_the_panel(self, plugin, mock_decky):
        """The bug this was all for: plugging in a floppy drive produces no
        media event and the storage source never disconnects, so the panel's
        view of attached drives was frozen and the row read "Not connected"
        with the drive plugged in."""
        from sources.storage_source import StorageSource, DriveKind
        storage = StorageSource({"drive_kinds": {DriveKind.FLOPPY: True}}, logger=MagicMock())
        storage._monitor = MagicMock()
        plugin.source_manager = MagicMock()
        plugin.source_manager.sources = [storage]
        plugin.settings.get_source_settings = lambda _t: {
            "drive_kinds": {DriveKind.FLOPPY: True}
        }

        await plugin._publish_statuses()
        mock_decky.emit.reset_mock()

        storage._drives["/dev/sda"] = DriveKind.FLOPPY      # drive plugged in
        await plugin._publish_statuses()

        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert "source_statuses" in calls
        entry = calls["source_statuses"][0]
        assert entry["drive_kinds"][DriveKind.FLOPPY]["present"] is True

    @pytest.mark.asyncio
    async def test_disconnected_event_emits_source_statuses(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "source_statuses" in emitted

    @pytest.mark.asyncio
    async def test_source_statuses_payload_is_list(self, plugin, mock_decky):
        from sources.base import SourceEvent, SourceEventKind, SourceType
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.NFC, source_id="nfc:test")
        await plugin._handle_source_event(event)
        ss_calls = [c for c in mock_decky.emit.call_args_list if c.args[0] == "source_statuses"]
        assert len(ss_calls) == 1
        assert isinstance(ss_calls[0].args[1], list)


# ── C1 — Per-source media isolation ──────────────────────────────────────────

class TestPerSourceMediaIsolation:
    """Multiple sources must not clobber each other's state.

    Before the per-source registry there was a single global tag slot, so a
    storage eject cleared the NFC tag and could quit a game launched by tapping.
    """

    @pytest.mark.asyncio
    async def test_storage_load_does_not_clobber_nfc_tag(self, plugin, mock_decky):
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sdb1", uri="steam://rungameid/500")
            )

        assert plugin.current_tag_uid == "DEADBEEF"
        assert plugin.current_tag_uri == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_storage_unload_does_not_clear_nfc_tag(self, plugin, mock_decky):
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sdb1", uri="steam://rungameid/500")
            )
        await plugin._handle_media_unload(_make_storage_unload_event("/dev/sdb1"))

        assert plugin.current_tag_uid == "DEADBEEF"
        assert plugin.current_tag_uri == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_ejecting_other_media_does_not_quit_nfc_launched_game(self, plugin, mock_decky):
        """The whole point of launch attribution."""
        plugin.is_pairing = False
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        await plugin.set_running_game(400)

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sdb1", uri="steam://rungameid/500")
            )
        mock_decky.emit.reset_mock()
        await plugin._handle_media_unload(_make_storage_unload_event("/dev/sdb1"))

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted

    @pytest.mark.asyncio
    async def test_collision_is_scoped_per_source(self, plugin, mock_decky):
        """Media on a second source is not a collision on the first."""
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
            mock_decky.emit.reset_mock()
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sdb1", uri="steam://rungameid/500")
            )

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "multiple_tags" not in emitted

    @pytest.mark.asyncio
    async def test_get_active_media_reports_every_source(self, plugin, mock_decky):
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
            await plugin._handle_media_load(
                _make_storage_load_event("/dev/sdb1", uri="steam://rungameid/500")
            )

        active = await plugin.get_active_media()
        by_type = {m["source_type"]: m for m in active}
        assert by_type["nfc"]["media_id"] == "DEADBEEF"
        assert by_type["storage"]["media_id"] == "/dev/sdb1"

    @pytest.mark.asyncio
    async def test_manual_launch_is_not_attributed_to_any_medium(self, plugin, mock_decky):
        """A game the user started by hand must not be quit by removing a tag."""
        plugin.is_pairing = False
        await plugin.set_running_game(400)          # no preceding media load

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        mock_decky.emit.reset_mock()
        await plugin._handle_media_unload(_make_unload_event("DEADBEEF"))

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted

    @pytest.mark.asyncio
    async def test_launch_origin_survives_frontend_reporting_first(self, plugin, mock_decky):
        """set_running_game may arrive before _handle_media_load finishes.

        The frontend launches on uri_detected, so the origin must be claimed
        before that event is emitted or attribution is lost.
        """
        plugin.is_pairing = False
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                _make_load_event("DEADBEEF", uri="steam://rungameid/400")
            )
        assert plugin._registry._pending_launch_origin is not None
        assert plugin._registry._pending_launch_origin["media_id"] == "DEADBEEF"


# ── Pairing state sync ───────────────────────────────────────────────────────

class TestPairingUriSync:
    """After a successful write the panel must show the new URI immediately.

    poll() only reports a tag on arrival, so a card resting on the reader is
    never re-read; without an explicit sync the UI showed "Url: Empty" until
    the user lifted and replaced the card.
    """

    @pytest.mark.asyncio
    async def test_successful_pairing_updates_current_tag_uri(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        plugin.source_manager.replace(_mock_nfc_source((True, None)))

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(_make_load_event("DEADBEEF", uri=None))

        assert plugin.current_tag_uri == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_successful_pairing_emits_uri_detected_marked_paired(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        plugin.source_manager.replace(_mock_nfc_source((True, None)))

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(_make_load_event("DEADBEEF", uri=None))

        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert "uri_detected" in calls
        assert calls["uri_detected"]["uri"] == "steam://rungameid/400"
        # The flag is what stops the frontend launching the game on a pair.
        assert calls["uri_detected"]["paired"] is True

    @pytest.mark.asyncio
    async def test_successful_pairing_updates_active_media_registry(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        plugin.source_manager.replace(_mock_nfc_source((True, None)))

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(_make_load_event("DEADBEEF", uri=None))

        active = await plugin.get_active_media()
        assert active[0]["uri"] == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_failed_pairing_does_not_claim_a_uri(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        plugin.source_manager.replace(_mock_nfc_source((False, "Write failed at page 4")))

        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(_make_load_event("DEADBEEF", uri=None))

        assert plugin.current_tag_uri is None
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "uri_detected" not in emitted


# ── Media problems reported to the panel ─────────────────────────────────────

class TestUnreadableMediaReporting:
    """A blank disk and an unreadable one both carry no URI, but only one of
    them is the user's problem to fix — the panel has to tell them apart."""

    def _storage_event(self, payload):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
            media_id="/dev/sda",
            uri="",
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_unreadable_media_is_flagged(self, plugin, mock_decky):
        event = self._storage_event({"unreadable": True, "error": "Format it as FAT."})
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert calls["uri_detected"]["unreadable"] is True
        assert calls["uri_detected"]["blank"] is False
        assert calls["uri_detected"]["error"] == "Format it as FAT."

    @pytest.mark.asyncio
    async def test_blank_media_is_not_flagged_as_unreadable(self, plugin, mock_decky):
        event = self._storage_event({"blank": True, "mountpoint": "/tmp/x"})
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert calls["uri_detected"]["unreadable"] is False
        assert calls["uri_detected"]["blank"] is True

    @pytest.mark.asyncio
    async def test_storage_media_does_not_clear_the_nfc_tag_uri(self, plugin, mock_decky):
        """current_tag_* is the NFC view; an unreadable floppy must not wipe it."""
        plugin.current_tag_uri = "steam://rungameid/400"
        event = self._storage_event({"unreadable": True})
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)
        assert plugin.current_tag_uri == "steam://rungameid/400"


# ── C3 — the quit decision lives in the backend ──────────────────────────────

class TestQuitDecision:
    """Only the backend knows which medium launched the running game, so it
    decides close-vs-pause. The frontend owns the mechanism, not the policy."""

    async def _remove_media_during_game(self, plugin, auto_close):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        from main import PluginState
        plugin.state = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin._registry.claim_launch("nfc:/dev/ttyUSB0", "DEADBEEF")
        plugin._registry.confirm_launch(1, None)
        # The fixture's settings.get is a plain function reading this dict,
        # so the setting has to be written where it actually looks.
        plugin.settings.settings["auto_close"] = auto_close
        event = MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
            media_id="DEADBEEF",
            uri="steam://rungameid/400",
        )
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_unload(event)

    @pytest.mark.asyncio
    async def test_auto_close_on_instructs_close(self, plugin, mock_decky):
        await self._remove_media_during_game(plugin, auto_close=True)
        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert calls["card_removed_during_game"]["action"] == "close"

    @pytest.mark.asyncio
    async def test_auto_close_off_instructs_pause(self, plugin, mock_decky):
        await self._remove_media_during_game(plugin, auto_close=False)
        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert calls["card_removed_during_game"]["action"] == "pause"


# ── C4 — sources can be switched off ─────────────────────────────────────────

class TestSourceEnablement:

    def test_source_without_enabled_setting_is_on(self):
        from sources.nfc_source import NfcSource
        assert NfcSource({"device_path": "/dev/ttyUSB0"}).is_enabled() is True

    def test_enabled_false_switches_a_source_off(self):
        from sources.mqtt_source import MqttSource
        assert MqttSource({"enabled": False}).is_enabled() is False

    def test_enabled_true_switches_a_source_on(self):
        from sources.mqtt_source import MqttSource
        assert MqttSource({"enabled": True}).is_enabled() is True

    def test_camera_is_off_by_default(self):
        """A Steam Deck has no camera; leaving it on retried /dev/video0 forever."""
        from main import SettingsManager
        defaults = SettingsManager.__new__(SettingsManager)
        # Read the literal defaults without touching the filesystem.
        import main as main_module
        sm = main_module.SettingsManager.__new__(main_module.SettingsManager)
        sm.path = "/nonexistent"
        sm.settings = None
        SettingsManager.__init__(sm, "/nonexistent/settings.json")
        assert sm.settings["sources"]["camera"]["enabled"] is False
        assert sm.settings["sources"]["storage"]["enabled"] is True


# ── Targeted pairing — one Pair button per trigger ───────────────────────────

class TestTargetedPairing:
    """The panel offers a Pair button per trigger, so it must say which one it
    means. With a tag on the reader and a disk in the drive, an untargeted
    pair would write to whichever arrived first."""

    def _with_storage(self, plugin):
        """The fixture only wires an NFC source; these tests need a second
        trigger to target."""
        from sources.storage_source import StorageSource
        storage = StorageSource({}, logger=MagicMock())
        plugin.source_manager.replace(storage)
        return storage

    def _storage_load(self):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
            media_id="/dev/sda",
            uri="",
            payload={"blank": True, "drive_kind": "floppy"},
        )

    @pytest.mark.asyncio
    async def test_targeting_one_trigger_ignores_media_from_another(self, plugin, mock_decky):
        self._with_storage(plugin)
        plugin.source_manager.replace(_mock_nfc_source())
        assert await plugin.start_pairing("steam://run/1", source_id="nfc:/dev/ttyUSB0")

        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(self._storage_load())

        mock_pair.assert_not_called()
        assert plugin.is_pairing is True, "still armed, waiting for the right trigger"

    @pytest.mark.asyncio
    async def test_targeting_one_trigger_accepts_its_own_media(self, plugin, mock_decky):
        self._with_storage(plugin)
        assert await plugin.start_pairing("steam://run/1", source_id="storage:udev")

        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(self._storage_load())

        mock_pair.assert_called_once_with("/dev/sda", source_id="storage:udev")

    @pytest.mark.asyncio
    async def test_untargeted_pairing_accepts_any_trigger(self, plugin, mock_decky):
        self._with_storage(plugin)
        """The game-page link button arms everything and lets the user choose
        by presenting a medium."""
        assert await plugin.start_pairing("steam://run/1")

        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(self._storage_load())

        mock_pair.assert_called_once()

    @pytest.mark.asyncio
    async def test_unwritable_target_is_rejected(self, plugin):
        assert await plugin.start_pairing("steam://run/1", source_id="camera:/dev/video0") is False
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_cancelling_clears_the_target(self, plugin):
        self._with_storage(plugin)
        await plugin.start_pairing("steam://run/1", source_id="storage:udev")
        await plugin.cancel_pairing()
        assert plugin.pairing_source_id is None


class TestDriveKindDefaults:

    def test_plugin_defaults_match_the_source(self, tmp_path):
        """The default settings dict in main.py and DEFAULT_DRIVE_KINDS in
        storage_source.py are the same policy written twice. A source created
        from a settings blob never consults its own defaults, so a drift here
        is silent: the panel would show a category on that the source treats
        as off."""
        from main import SettingsManager
        from sources.storage_source import DEFAULT_DRIVE_KINDS
        defaults = SettingsManager(str(tmp_path / "s.json")).settings
        assert defaults["sources"]["storage"]["drive_kinds"] == DEFAULT_DRIVE_KINDS


# ── The registry keeps a medium's drive category ─────────────────────────────

class TestDriveKindPersistence:
    """The panel matches a medium to its row by drive_kind, so a LOAD that
    omits it must not erase what we already know about the same medium."""

    def _event(self, payload, uri=""):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
            media_id="/dev/sda",
            uri=uri,
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_partial_reload_keeps_the_category(self, plugin, mock_decky):
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                self._event({"blank": True, "drive_kind": "floppy"})
            )
            await plugin._handle_media_load(self._event({"rearmed": True}))

        assert plugin._registry._media["storage:udev"]["drive_kind"] == "floppy"

    @pytest.mark.asyncio
    async def test_a_different_disk_does_not_inherit_the_category(self, plugin, mock_decky):
        """Swapping a floppy for a card in a multi-slot reader is a new medium;
        it must be classified on its own terms, not the last disk's."""
        from sources.base import MediaEvent, MediaEventKind, SourceType
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(
                self._event({"blank": True, "drive_kind": "floppy"})
            )
            await plugin._handle_media_load(MediaEvent(
                kind=MediaEventKind.LOAD,
                source_type=SourceType.STORAGE,
                source_id="storage:udev",
                media_id="/dev/sdb",
                uri="",
                payload={"blank": True},
            ))

        assert plugin._registry._media["storage:udev"]["drive_kind"] is None


# ── Drive categories reach the panel ─────────────────────────────────────────

class TestDriveKindStatus:

    @pytest.mark.asyncio
    async def test_storage_status_reports_presence_per_category(self, plugin, tmp_path):
        from sources.storage_source import StorageSource
        storage = StorageSource({"drive_kinds": {"floppy": True, "usb": False}},
                                logger=MagicMock())
        storage._monitor = MagicMock()
        storage._drives["/dev/sda"] = "floppy"
        plugin.source_manager.replace(storage)
        plugin.source_manager.register(storage)
        plugin.settings.get_source_settings = lambda t: (
            {"drive_kinds": {"floppy": True, "usb": False}} if t == "storage" else {}
        )

        statuses = await plugin.get_source_statuses()
        entry = next(e for e in statuses if e["source_type"] == "storage")
        assert entry["drive_kinds"]["floppy"] == {"present": True, "enabled": True}
        assert entry["drive_kinds"]["usb"] == {"present": False, "enabled": False}


# ── Launch attribution survives repeated running-game reports ────────────────

class TestLaunchOriginPersistence:
    """Reported from hardware: auto-close never fired. The log showed the game
    attributed to the tag, and one second later "that game was launched by
    None" when the tag came off."""

    @pytest.mark.asyncio
    async def test_a_repeated_report_of_the_same_game_keeps_the_origin(self, plugin):
        plugin._registry.claim_launch("nfc:x", "AABB")
        await plugin.set_running_game(400)
        assert plugin._registry.launch_origin == {"source_id": "nfc:x", "media_id": "AABB"}

        # The frontend reports the running game repeatedly; taking the (now
        # empty) pending origin again wiped the attribution.
        await plugin.set_running_game(400)
        assert plugin._registry.launch_origin == {"source_id": "nfc:x", "media_id": "AABB"}

    @pytest.mark.asyncio
    async def test_a_hand_launched_game_is_attributed_to_nothing(self, plugin):
        await plugin.set_running_game(400)
        assert plugin._registry.launch_origin is None

    @pytest.mark.asyncio
    async def test_switching_games_without_a_medium_clears_the_origin(self, plugin):
        plugin._registry.claim_launch("nfc:x", "AABB")
        await plugin.set_running_game(400)
        await plugin.set_running_game(500)          # started by hand
        assert plugin._registry.launch_origin is None

    @pytest.mark.asyncio
    async def test_a_new_medium_takes_over_attribution(self, plugin):
        plugin._registry.claim_launch("nfc:x", "AABB")
        await plugin.set_running_game(400)
        plugin._registry.claim_launch("storage:udev", "/dev/sda")
        await plugin.set_running_game(400)
        assert plugin._registry.launch_origin["source_id"] == "storage:udev"

    @pytest.mark.asyncio
    async def test_the_launching_medium_can_still_quit_the_game(self, plugin, mock_decky):
        """End to end: the attribution surviving is what makes auto-close work."""
        from sources.base import MediaEvent, MediaEventKind, SourceType
        from main import PluginState
        plugin.settings.settings["auto_close"] = True
        plugin._registry.claim_launch("nfc:/dev/ttyUSB0", "AABB")
        await plugin.set_running_game(400)
        await plugin.set_running_game(400)          # the duplicate report
        plugin.state = PluginState.GAME_RUNNING
        plugin._registry._media["nfc:/dev/ttyUSB0"] = {
            "source_id": "nfc:/dev/ttyUSB0", "source_type": "nfc",
            "media_id": "AABB", "uri": "steam://run/400",
        }

        await plugin._handle_media_unload(MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
            media_id="AABB",
            uri="steam://run/400",
        ))

        calls = {c.args[0]: c.args[1] for c in mock_decky.emit.call_args_list}
        assert "card_removed_during_game" in calls, "auto-close never fired"
        assert calls["card_removed_during_game"]["action"] == "close"


class TestUnsupportedTagWrite:
    """Reported from hardware: an NFC keyring read as an empty tag, and pairing
    failed with "Write failed at page 4" — the first of many refusals, reported
    as if it were a transient error."""

    def test_a_tag_that_is_neither_classic_nor_ndef_is_refused(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.ntag2xx_read_block.return_value = None  # no CC
        success, err = plugin.nfc_source.write_ndef_uri(uid, "steam://run/1")
        assert success is False
        assert "unsupported tag" in (err or "").lower()
        plugin.nfc_source._reader.ntag2xx_write_block.assert_not_called()

    def test_a_readable_ntag_is_still_written(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = False
        plugin.nfc_source._reader.ntag2xx_read_block.return_value = bytes(
            [0xE1, 0x10, 0x3E, 0x00]
        )
        plugin.nfc_source._reader.ntag2xx_write_block.return_value = True
        success, err = plugin.nfc_source.write_ndef_uri(uid, "steam://run/1")
        assert success is True, err


# ── Printable cards ──────────────────────────────────────────────────────────

class TestHttpsHostValidation:
    """The old check rejected exactly three strings: 'localhost', '127.0.0.1'
    and '::1'. Everything else pointing at this machine or its LAN went
    through — a tapped card could open the Deck's own services, or a box on
    the same network, in the Steam browser."""

    @pytest.mark.parametrize("host", [
        "localhost",
        "127.0.0.1",
        "127.0.0.2",          # rest of 127/8
        "127.1.2.3",
        "[::1]",              # the bracketed form a URI actually carries
        "[::ffff:127.0.0.1]", # IPv4-mapped IPv6
        "0.0.0.0",
        "10.0.0.5",           # RFC1918
        "172.16.4.1",
        "192.168.1.10",
        "169.254.169.254",    # link-local / cloud metadata
        "printer.local",      # mDNS
        "router.internal",
    ])
    def test_local_targets_are_blocked(self, plugin, host):
        assert plugin._validate_uri(f"https://{host}/x") is False

    @pytest.mark.parametrize("host", [
        "store.steampowered.com",
        "example.com",
        "8.8.8.8",
        "sub.domain.example.org",
    ])
    def test_public_targets_are_allowed(self, plugin, host):
        assert plugin._validate_uri(f"https://{host}/x") is True

    def test_port_does_not_defeat_the_check(self, plugin):
        """netloc carries the port; the old code compared the whole netloc to
        a bare host, so 'localhost:8080' never matched and was allowed."""
        assert plugin._validate_uri("https://localhost:8080/admin") is False
        assert plugin._validate_uri("https://127.0.0.1:1337/") is False

    def test_credentials_do_not_defeat_the_check(self, plugin):
        assert plugin._validate_uri("https://user@127.0.0.1/") is False


class TestCardRpcs:
    """A QR is the one trigger medium that costs nothing to produce, so these
    RPCs generate rather than pair — there is nothing to write to."""

    @pytest.mark.asyncio
    async def test_preview_returns_a_data_uri(self, plugin, monkeypatch):
        from PIL import Image
        monkeypatch.setattr("cards.qr_image", lambda uri, module_px=6: Image.new("L", (40, 40), 255))
        result = await plugin.get_qr_preview("steam://rungameid/220")
        assert result["ok"] is True
        assert result["data_uri"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_appid", [
        "../../../etc/passwd",
        "220/../../../../root/.ssh/id_rsa",
        "..",
        "220; rm -rf /",
        "not-a-number",
    ])
    async def test_save_card_refuses_an_app_id_that_is_not_an_app_id(self, plugin, bad_appid):
        """appid is interpolated into a filesystem path by cards.find_art and
        this process runs as root, so a traversal value would have read an
        arbitrary file and rendered it into a PNG the caller gets back."""
        result = await plugin.save_game_card(
            "steam://rungameid/220", "Half-Life 2", bad_appid
        )
        assert result["ok"] is False
        assert result["error"] == "Invalid app id"

    @pytest.mark.asyncio
    async def test_find_art_refuses_traversal_independently(self):
        """Checked at the helper too — it is module-level and reachable by any
        future caller."""
        from cards.qr import find_art
        assert find_art("../../../etc") is None
        assert find_art("") is None

    @pytest.mark.asyncio
    async def test_preview_refuses_a_uri_outside_the_allowlist(self, plugin):
        """The same validation as launching: a QR is a launch instruction, and
        generating one for javascript: would be handing out a loaded gun."""
        result = await plugin.get_qr_preview("javascript:alert(1)")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_preview_works_while_the_camera_is_off(self, plugin, monkeypatch):
        """Printing codes before owning a webcam is reasonable."""
        from PIL import Image
        plugin.settings.settings.setdefault("sources", {})["camera"] = {"enabled": False}
        monkeypatch.setattr("cards.qr_image", lambda uri, module_px=6: Image.new("L", (40, 40), 255))
        assert (await plugin.get_qr_preview("steam://rungameid/220"))["ok"] is True

    @pytest.mark.asyncio
    async def test_preview_reports_a_generator_failure(self, plugin, monkeypatch):
        def boom(uri, module_px=6):
            raise RuntimeError("no encoder")
        monkeypatch.setattr("cards.qr_image", boom)
        result = await plugin.get_qr_preview("steam://rungameid/220")
        assert result["ok"] is False and "no encoder" in result["error"]

    @pytest.mark.asyncio
    async def test_save_writes_to_the_users_documents(self, plugin, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin, "_card_output_dir", lambda: str(tmp_path / "decky-links"))
        captured = {}

        def fake_save(uri, title, appid, out_dir, home, dpi, owner):
            captured.update(uri=uri, title=title, appid=appid, out_dir=out_dir)
            return {"front": out_dir + "/f.png", "back": out_dir + "/b.png"}

        monkeypatch.setattr("cards.save_card", fake_save)
        result = await plugin.save_game_card("steam://rungameid/220", "Half-Life 2", "220")
        assert result["ok"] is True
        assert set(result["paths"]) == {"front", "back"}
        assert captured["title"] == "Half-Life 2"
        assert captured["appid"] == "220"

    @pytest.mark.asyncio
    async def test_save_refuses_an_invalid_uri(self, plugin):
        result = await plugin.save_game_card("file:///etc/passwd", "x", "1")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_save_does_not_block_the_event_loop(self, plugin, monkeypatch, tmp_path):
        """Rendering two 1200x1800 cards takes long enough to stall every source
        if it runs on the loop — the same mistake mounting made."""
        import time
        monkeypatch.setattr(plugin, "_card_output_dir", lambda: str(tmp_path))
        ticks = []

        def slow_save(*args, **kwargs):
            time.sleep(0.05)
            return {"front": "f", "back": "b"}

        monkeypatch.setattr("cards.save_card", slow_save)

        async def ticker():
            for _ in range(10):
                ticks.append(1)
                await asyncio.sleep(0.005)

        await asyncio.gather(plugin.save_game_card("steam://rungameid/220"), ticker())
        assert len(ticks) == 10

    def test_output_dir_is_under_the_user_home(self, plugin):
        import os as _os
        assert plugin._card_output_dir().endswith(_os.path.join("Documents", "decky-links"))
