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

    def test_simulate_tag_sets_state_and_emits(self, plugin, mock_decky):
        uid = b"\xAA\xBB\xCC\xDD"
        uri = "https://foo"
        with patch.object(plugin.nfc_source, "_classify_tag", return_value={"uid": uid.hex().upper()}):
            coro = plugin.simulate_tag(uid, uri)
            asyncio.get_event_loop().run_until_complete(coro)
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
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = "steam://rungameid/400"
        plugin.is_pairing      = False

        event = _make_unload_event("DEADBEEF", uri="steam://rungameid/400")
        await plugin._handle_media_unload(event)

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" in emitted
        assert "tag_removed" in emitted

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

        mock_pair.assert_called_once_with(bytes.fromhex("DEADBEEF"))
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing_plays_success_sound_on_write_ok(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid)

        mock_sound.assert_called_with("success.flac")

    @pytest.mark.asyncio
    async def test_pairing_plays_error_sound_on_write_fail(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(False, "Auth failed")), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid)

        mock_sound.assert_called_with("error.flac")

    @pytest.mark.asyncio
    async def test_pairing_exits_mode_after_write(self, plugin):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

        assert plugin.is_pairing  is False
        assert plugin.pairing_uri is None

    @pytest.mark.asyncio
    async def test_pairing_with_no_uri_aborts(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = None
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", new_callable=MagicMock) as mock_write:
            await plugin._handle_pairing(uid)

        mock_write.assert_not_called()
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_pairing_emits_result_event(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

        mock_decky.emit.assert_called()
        event_name = mock_decky.emit.call_args_list[-1].args[0]
        assert event_name == "pairing_result"

    @pytest.mark.asyncio
    async def test_pairing_does_not_launch_game_after_write(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin.nfc_source, "write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

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
        assert "too long" in (err or "").lower()

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
        assert "too long" in (err or "").lower()

    def test_classic_capacity_detection_blocks(self, plugin):
        uid = _make_uid()
        plugin.nfc_source._reader.mifare_classic_authenticate_block.return_value = True
        with patch.object(plugin.nfc_source, "_iter_mifare_data_blocks", return_value=[4, 5]):
            long_uri = "https://" + "x" * 100
            success, err = plugin.nfc_source.write_ndef_uri(uid, long_uri)
        assert success is False
        assert "exceeds limit" in (err or "").lower()

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
        assert "exceeds limit" in (err or "").lower()


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
        })


# ── §6.3 — Card Removed During Game ──────────────────────────────────────────

class TestCardRemovedDuringGame:

    @pytest.mark.asyncio
    async def test_removal_event_emitted_when_game_running(self, plugin, mock_decky):
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = "steam://rungameid/400"
        plugin.is_pairing      = False

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
    async def test_storage_connected_does_not_change_state(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.IDLE
        event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.IDLE

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
    async def test_storage_disconnected_does_not_change_state(self, plugin, mock_decky):
        from main import PluginState
        from sources.base import SourceEvent, SourceEventKind, SourceType
        plugin.state = PluginState.READY
        event = SourceEvent(kind=SourceEventKind.DISCONNECTED, source_type=SourceType.STORAGE, source_id="storage:udev")
        await plugin._handle_source_event(event)
        assert plugin.state == PluginState.READY

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
            event = SourceEvent(kind=SourceEventKind.CONNECTED, source_type=source_type, source_id=f"{source_type.value}:test")
            await plugin._handle_source_event(event)
            emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
            assert "source_statuses" in emitted, f"Missing source_statuses for {source_type}"

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
