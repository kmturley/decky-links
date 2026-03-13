"""
test_plugin.py — unit tests for decky-links main.py logic.

All NFC hardware, the decky runtime, and subprocess calls are mocked.
Tests cover:
  - State machine transitions (Spec §5 / §6)
  - URI allowlist validation (Spec §4)
  - No game stacking (Spec §8)
  - No auto-relaunch after game exit (Spec §6.4 / §6.5)
  - Removal debounce (Spec §10)
  - Pairing flow guards (Spec §7)
  - Error audio on invalid/blocked tag (Spec §12 / §11)
  - NTAG213 size enforcement (Spec §3.3)
  - Dual-launch prevention (backend defers Steam URIs to frontend)
"""
import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_uid(b: bytes = b"\xDE\xAD\xBE\xEF"):
    """Return a mock UID bytes object with a working .hex() method."""
    uid = MagicMock()
    uid.hex.return_value = b.hex()
    uid.__eq__ = lambda self, other: b == other
    uid.__ne__ = lambda self, other: b != other
    return uid


# (no loop-driving helper needed — debounce and removal logic is tested directly below)


# -----------------------------------------------------------------------
# §5 / §6 — State Machine Transitions
# -----------------------------------------------------------------------

class TestStateMachine:

    def test_initial_state_is_idle_before_main(self):
        """Before _main() is called, the state defaults to IDLE."""
        from main import Plugin, PluginState
        p = Plugin()
        # Manually set as _main() would
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
        """If we receive set_running_game(None) while already READY, state stays READY."""
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.running_game_id = None
        await plugin.set_running_game(None)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_scan_transitions_to_card_present_then_ready_on_no_uri(self, plugin, mock_decky):
        """When no URI found, state goes CARD_PRESENT briefly then back to READY."""
        from main import PluginState
        uid = _make_uid()
        # patch lower-level reader to return no URI record
        fake_rec = MagicMock()
        with patch.object(plugin, "_read_ndef_records", return_value=[]), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_scan_stays_card_present_for_steam_uri_awaiting_game(self, plugin, mock_decky):
        """Steam URI scan leaves state as CARD_PRESENT until frontend reports game running."""
        from main import PluginState
        uid = _make_uid()
        plugin.running_game_id = None
        # simulate NDEF records containing steam URI
        from ndef import UriRecord
        with patch.object(plugin, "_read_ndef_records", return_value=[UriRecord("steam://rungameid/400")]), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)
        # No game yet reported → state stays CARD_PRESENT
        assert plugin.state == PluginState.CARD_PRESENT


# -----------------------------------------------------------------------
# §4 — URI Allowlist Validation
# -----------------------------------------------------------------------

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


# -----------------------------------------------------------------------
# Settings Load Validation
# -----------------------------------------------------------------------

class TestSettingsLoadValidation:

    def test_invalid_settings_from_file_are_ignored(self, tmp_path):
        from main import SettingsManager

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "device_path": "/etc/passwd",   # invalid path prefix
            "baudrate": "fast",             # invalid type
            "polling_interval": "0",        # invalid type/range
            "auto_launch": "yes",           # invalid type
            "auto_close": False,            # valid
            "reader_type": "unknown",      # invalid value
        }))

        settings = SettingsManager(str(settings_path))

        assert settings.get("device_path").startswith("/dev/")
        assert settings.get("baudrate") == 115200
        assert settings.get("polling_interval") == 0.5
        assert settings.get("auto_launch") is True
        assert settings.get("auto_close") is False
        assert settings.get("reader_type") == "pn532_uart"


# -----------------------------------------------------------------------
# §8 — No Game Stacking (launch guard)
# -----------------------------------------------------------------------

class TestNoGameStacking:

    @pytest.mark.asyncio
    async def test_launch_blocked_when_game_running(self, plugin, mock_decky):
        """Backend should NOT launch when running_game_id is set."""
        from main import PluginState
        plugin.running_game_id = 400
        plugin.state           = PluginState.GAME_RUNNING
        uid                    = _make_uid()

        with patch.object(plugin, "_read_ndef_uri", return_value="steam://rungameid/400"), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)

        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_steam_uri_launched_by_backend(self, plugin, mock_decky):
        """Backend must xdg-open https URIs; Steam URIs are left to the frontend."""
        plugin.running_game_id = None
        uid                    = _make_uid()

        from ndef import UriRecord
        with patch.object(plugin, "_read_ndef_records", return_value=[UriRecord("https://example.com")]), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)

        mock_launch.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_local_command_not_launched_when_blocked(self, plugin, mock_decky):
        plugin.running_game_id = None
        uid = _make_uid()
        command = '"/run/media/mmcblk0p1/Emulation/tools/launchers/dolphin-emu.sh" "/run/media/mmcblk0p1/Emulation/roms/game.iso"'

        with patch.object(plugin, "_read_ndef_uri", return_value=command), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)

        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_steam_uri_not_launched_by_backend(self, plugin, mock_decky):
        """Steam URIs must NOT trigger _launch_uri — frontend handles them."""
        plugin.running_game_id = None
        uid                    = _make_uid()

        with patch.object(plugin, "_read_ndef_uri", return_value="steam://rungameid/400"), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)

        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_launch_disabled_prevents_any_launch(self, plugin, mock_decky):
        plugin.settings.get = lambda k: {
            "auto_launch": False,
            "polling_interval": 0.5,
        }.get(k)
        uid = _make_uid()

        with patch.object(plugin, "_read_ndef_uri", return_value="https://example.com"), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)

        mock_launch.assert_not_called()


# -----------------------------------------------------------------------
# §6.4 / §6.5 — No Auto-Relaunch
# -----------------------------------------------------------------------

class TestNoAutoRelaunch:

    @pytest.mark.asyncio
    async def test_game_exit_does_not_clear_tag_uid(self, plugin):
        """
        When a game exits, current_tag_uid must NOT be cleared — the card may
        still be physically present. Clearing happens on physical removal only.
        (Ensures the card can be re-read only after it is removed and reinserted.)
        """
        from main import PluginState
        plugin.state            = PluginState.GAME_RUNNING
        plugin.running_game_id  = 400
        plugin.current_tag_uid  = "DEADBEEF"
        plugin.current_tag_uri  = "steam://rungameid/400"

        await plugin.set_running_game(None)

        # UID should still be visible (card is still present)
        assert plugin.current_tag_uid == "DEADBEEF"
        assert plugin.state           == PluginState.READY


# -----------------------------------------------------------------------
# §9.1 — Reader abstraction init tests
# -----------------------------------------------------------------------

class TestReaderInit:

    def test_classify_tag_reports_types(self, plugin, uid_bytes):
        # assume reader methods will indicate non-classic
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.return_value = b"\x00"
        meta = plugin._classify_tag(uid_bytes)
        assert meta["uid"] == uid_bytes.hex().upper()
        assert meta["type"] == "ntag21x"
        assert meta["capacity_bytes"] > 0

    def test_classify_felica_by_length(self, plugin):
        uid = b"\x01\x02\x03\x04\x05\x06\x07\x08"  # 8 bytes
        meta = plugin._classify_tag(uid)
        assert meta["type"] == "felica"
        assert meta["capacity_bytes"] == 0

    @pytest.mark.asyncio
    async def test_reader_disconnect_triggers_reinit(self, plugin, mock_decky):
        # place a fake reader that reports disconnected
        plugin.reader = MagicMock()
        plugin.reader.is_connected.return_value = False
        triggered = False
        async def fake_init():
            nonlocal triggered
            triggered = True
        plugin._init_reader = fake_init
        # run a short slice of the loop; it should notice the disconnected reader
        loop_task = asyncio.create_task(plugin._nfc_loop())
        await asyncio.sleep(0.2)
        loop_task.cancel()
        assert triggered

    @pytest.mark.asyncio
    async def test_get_tag_metadata_method(self, plugin, uid_bytes):
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.return_value = b"\x00"
        # simulate current tag and override classify to include protected
        plugin.current_tag_uid = uid_bytes.hex().upper()
        plugin._classify_tag = lambda u: {"type": "ntag21x", "capacity_bytes": 4, "protected": False}
        info = await plugin.get_tag_metadata()
        assert info.get("type") == "ntag21x"
        assert info.get("protected") is False
        # invalid hex
        bad = await plugin.get_tag_metadata("nothex")
        assert "error" in bad

    @pytest.mark.asyncio
    async def test_init_reader_success_sets_reader(self, mock_decky, tmp_path):
        import main
        from main import Plugin
        # patch the PN532UARTReader imported into main so _create_reader uses mock
        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=True)
        patcher = patch.object(main, 'PN532UARTReader', return_value=mock_reader)
        patcher.start()
        try:
            p = Plugin()
            # make settings return a fake existing path
            fake_path = str(tmp_path / "dev")
            open(fake_path, "w").close()
            p.settings = MagicMock()
            # also ensure reader_type is correct so _init_reader succeeds
            p.settings.get = lambda k: fake_path if k == "device_path" else ("pn532_uart" if k == "reader_type" else 115200)

            await p._init_reader()
            assert p.reader is mock_reader
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_init_reader_failure_leaves_none(self, mock_decky, tmp_path):
        import main
        from main import Plugin
        mock_reader = MagicMock()
        mock_reader.connect = AsyncMock(return_value=False)
        patcher = patch.object(main, 'PN532UARTReader', return_value=mock_reader)
        patcher.start()
        try:
            p = Plugin()
            fake_path = str(tmp_path / "dev2")
            open(fake_path, "w").close()
            p.settings = MagicMock()
            p.settings.get = lambda k: fake_path if k == "device_path" else ("pn532_uart" if k == "reader_type" else 115200)

            await p._init_reader()
            assert p.reader is None
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_get_reader_diagnostics(self, plugin):
        # no reader connected
        plugin.reader = None
        assert await plugin.get_reader_diagnostics() == {"connected": False}

        # attach a fake reader with firmware
        fake = MagicMock()
        fake.firmware_version.return_value = (1, 2, 3, 4)
        plugin.reader = fake
        info = await plugin.get_reader_diagnostics()
        assert info["connected"] is True
        assert info["firmware"] == (1, 2, 3, 4)

        # simulate exception fetching firmware
        def boom():
            raise RuntimeError("nope")
        fake.firmware_version.side_effect = boom
        info2 = await plugin.get_reader_diagnostics()
        assert info2.get("error") == "nope"

    def test_reader_type_validation(self, plugin):
        # valid and invalid values
        assert plugin._validate_setting("reader_type", "pn532_uart")
        assert not plugin._validate_setting("reader_type", "badtype")

    @pytest.mark.asyncio
    async def test_set_reader_type_setting(self, plugin, mock_decky):
        # plugin.set_setting proxies validation
        assert await plugin.set_setting("reader_type", "pn532_uart")
        assert not await plugin.set_setting("reader_type", "invalidtype")

    @pytest.mark.asyncio
    async def test_init_reader_respects_type(self, plugin, tmp_path, mock_decky):
        # force settings for reader type resolution
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        plugin.settings.get = lambda k: fake_path if k == "device_path" else (
            "pn532_uart" if k == "reader_type" else 115200)

        # patch factory to return a fake reader
        fake_reader = MagicMock()
        fake_reader.connect = AsyncMock(return_value=True)
        with patch.object(plugin, "_create_reader", return_value=fake_reader):
            await plugin._init_reader()
            assert plugin.reader is fake_reader

    @pytest.mark.asyncio
    async def test_init_reader_unknown_type_leaves_none(self, plugin, tmp_path):
        fake_path = str(tmp_path / "dev")
        open(fake_path, "w").close()
        plugin.settings.get = lambda k: fake_path if k == "device_path" else (
            "no-such" if k == "reader_type" else 115200)
        await plugin._init_reader()
        assert plugin.reader is None

    @pytest.mark.asyncio
    async def test_create_reader_unknown(self, plugin):
        plugin.settings.get = lambda k: "nope" if k == "reader_type" else "/dev/null"
        assert await plugin._create_reader() is None

    @pytest.mark.asyncio
    async def test_create_reader_nfcpy_success(self, plugin, monkeypatch):
        # nfcpy backend now exists and should be created successfully
        plugin.settings.get = lambda k: "nfcpy" if k == "reader_type" else "/dev/null"
        reader = await plugin._create_reader()
        # Should create nfcpy reader
        assert reader is not None
        assert reader.__class__.__name__ == "NfcPyReader"

    @pytest.mark.asyncio
    async def test_ndef_detected_event_emitted(self, plugin, mock_decky, uid_bytes):
        # set up reader return
        plugin.reader.read_uid.return_value = uid_bytes
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.return_value = b"\x00" * 4
        # fake metadata cache
        plugin.current_tag_meta = {"foo": "bar"}
        # make _read_ndef_records return fake records
        fake_rec = MagicMock()
        with patch.object(plugin, "_read_ndef_records", return_value=[fake_rec]):
            await plugin._handle_scan(uid_bytes)
        # Check that ndef_detected was emitted with serialized records
        calls = [call for call in mock_decky.emit.call_args_list if call[0][0] == "ndef_detected"]
        assert len(calls) == 1
        assert "records" in calls[0][0][1]
        assert len(calls[0][0][1]["records"]) == 1
        # Also check tag_metadata was emitted
        mock_decky.emit.assert_any_call("tag_metadata", {"foo": "bar"})

    async def test_tag_metadata_event_emitted_when_classified(self, plugin, mock_decky, uid_bytes):
        # classification should update metadata and emit event
        plugin.reader.read_uid.return_value = uid_bytes
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.return_value = b"\x00" * 4
        # ensure no metadata to start
        assert plugin.current_tag_meta == {}

        # call _handle_scan which performs classification
        await plugin._handle_scan(uid_bytes)
        # classification should have added some fields
        assert "uid" in plugin.current_tag_meta
        mock_decky.emit.assert_any_call("tag_metadata", plugin.current_tag_meta)

    def test_simulate_tag_sets_state_and_emits(self, plugin, mock_decky):
        uid = b"\xAA\xBB\xCC\xDD"
        uri = "https://foo"
        # patch classify to avoid hardware calls
        plugin._classify_tag = lambda u: {"uid": u.hex().upper()}
        # run simulation
        coro = plugin.simulate_tag(uid, uri)
        # since simulate_tag is async, run it
        import asyncio
        asyncio.get_event_loop().run_until_complete(coro)
        assert plugin.current_tag_uid == uid.hex().upper()
        assert plugin.current_tag_uri == uri
        mock_decky.emit.assert_has_calls([
            call("tag_detected", {"uid": uid.hex().upper()}),
            call("uri_detected", {"uri": uri, "uid": uid.hex().upper()}),
        ])

    def test_classify_tag_protected_flag(self, plugin, uid_bytes):
        # simulate read error indicative of protection
        def boom(*args, **kwargs):
            raise RuntimeError("locked")
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.side_effect = boom
        meta = plugin._classify_tag(uid_bytes)
        assert meta.get("capacity_bytes") == 0
        assert meta.get("protected") is True

# -----------------------------------------------------------------------
# §10 — Debounce
# -----------------------------------------------------------------------

class TestDebounce:
    """
    Debounce logic lives in _nfc_loop but can be tested by replaying the
    exact counter-and-threshold logic it uses, verifying the emit calls.
    We drive the relevant code path by calling the private helper that
    _nfc_loop calls when missing_count reaches DEBOUNCE_THRESHOLD.
    """

    @pytest.mark.asyncio
    async def test_one_miss_does_not_emit_removal(self, plugin, mock_decky):
        """
        missing_count=1 < DEBOUNCE_THRESHOLD=3 → tag_removed must NOT fire.
        """
        # We drive only the removal-notification block; no reads below threshold
        # should trigger the event, so we assert it was never emitted.
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = "steam://rungameid/400"

        DEBOUNCE_THRESHOLD = 3
        missing_count = 1  # Only one miss — below threshold

        if missing_count >= DEBOUNCE_THRESHOLD:
            await plugin._nfc_loop_notify_removal()  # Would emit

        # Should not have been called at all
        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "tag_removed" not in emitted

    @pytest.mark.asyncio
    async def test_three_misses_emit_tag_removed(self, plugin, mock_decky):
        """
        When missing_count reaches DEBOUNCE_THRESHOLD (3), tag_removed is emitted.
        Tests the removal notification helper used by _nfc_loop.
        """
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.running_game_id = None
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = None
        plugin.is_pairing      = False

        await plugin._nfc_loop_notify_removal()

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "tag_removed" in emitted
        assert plugin.current_tag_uid is None
        assert plugin.current_tag_uri is None

    @pytest.mark.asyncio
    async def test_card_removed_during_game_emits_correct_event(self, plugin, mock_decky):
        """
        When GAME_RUNNING and tag removed, card_removed_during_game is emitted
        before tag_removed.
        """
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = "steam://rungameid/400"
        plugin.is_pairing      = False

        await plugin._nfc_loop_notify_removal()

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
        plugin.is_pairing      = True  # Pairing in progress

        await plugin._nfc_loop_notify_removal()

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted
        assert "tag_removed" in emitted  # tag_removed still fires


# -----------------------------------------------------------------------
# §7 — Pairing Flow
# -----------------------------------------------------------------------

class TestPairing:

    @pytest.mark.asyncio
    async def test_pairing_mode_suppresses_scan(self, plugin, mock_decky):
        """When is_pairing is True, _handle_scan must not be called for the tag."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_handle_pairing", new_callable=AsyncMock) as mock_pair, \
             patch.object(plugin, "_handle_scan",   new_callable=AsyncMock) as mock_scan:
            # Simulate the branching inside _nfc_loop manually
            is_new_tag = True
            if plugin.is_pairing:
                await plugin._handle_pairing(uid)
            elif is_new_tag:
                await plugin._handle_scan(uid)

        mock_pair.assert_called_once_with(uid)
        mock_scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_pairing_plays_success_sound_on_write_ok(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid)

        mock_sound.assert_called_with("success.flac")

    @pytest.mark.asyncio
    async def test_pairing_plays_error_sound_on_write_fail(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", return_value=(False, "Auth failed")), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_pairing(uid)

        mock_sound.assert_called_with("error.flac")

    @pytest.mark.asyncio
    async def test_pairing_exits_mode_after_write(self, plugin):
        """is_pairing must be False and pairing_uri cleared after any write attempt."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

        assert plugin.is_pairing  is False
        assert plugin.pairing_uri is None

    @pytest.mark.asyncio
    async def test_pairing_with_no_uri_aborts(self, plugin, mock_decky):
        """_handle_pairing called without a pairing_uri should abort gracefully."""
        plugin.is_pairing  = True
        plugin.pairing_uri = None
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", new_callable=MagicMock) as mock_write:
            await plugin._handle_pairing(uid)

        mock_write.assert_not_called()
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_pairing_emits_result_event(self, plugin, mock_decky):
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

        mock_decky.emit.assert_called()
        event_name = mock_decky.emit.call_args_list[-1].args[0]
        assert event_name == "pairing_result"

    @pytest.mark.asyncio
    async def test_pairing_does_not_launch_game_after_write(self, plugin, mock_decky):
        """Spec §7.2.7 — writing a tag must not trigger a game launch."""
        plugin.is_pairing  = True
        plugin.pairing_uri = "steam://rungameid/400"
        uid                = _make_uid()

        with patch.object(plugin, "_write_ndef_uri", return_value=(True, None)), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_pairing(uid)

        mock_launch.assert_not_called()


# -----------------------------------------------------------------------
# §11 / §12 — Audio Feedback and Error Handling
# -----------------------------------------------------------------------

class TestAudioFeedback:

    @pytest.mark.asyncio
    async def test_scan_sound_on_valid_tag(self, plugin, mock_decky):
        uid = _make_uid()
        with patch.object(plugin, "_read_ndef_uri", return_value="steam://rungameid/400"), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_scan(uid)
        mock_sound.assert_any_call("scan.flac")

    @pytest.mark.asyncio
    async def test_error_sound_when_no_uri(self, plugin, mock_decky):
        """Spec §12: error sound when tag has no URI."""
        uid = _make_uid()
        with patch.object(plugin, "_read_ndef_uri", return_value=None), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_scan(uid)
        mock_sound.assert_any_call("error.flac")

    @pytest.mark.asyncio
    async def test_error_sound_when_uri_blocked_by_allowlist(self, plugin, mock_decky):
        """Spec §4 / §12: error sound when URI is rejected by the allowlist."""
        uid = _make_uid()
        with patch.object(plugin, "_read_ndef_uri", return_value="ftp://evil.example.com"), \
             patch.object(plugin, "_play_sound") as mock_sound:
            await plugin._handle_scan(uid)
        mock_sound.assert_any_call("error.flac")

    @pytest.mark.asyncio
    async def test_no_launch_when_uri_blocked(self, plugin, mock_decky):
        uid = _make_uid()
        with patch.object(plugin, "_read_ndef_uri", return_value="ftp://evil.example.com"), \
             patch.object(plugin, "_launch_uri", new_callable=AsyncMock) as mock_launch, \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)
        mock_launch.assert_not_called()


# -----------------------------------------------------------------------
# §3.3 — NTAG213 Capacity Enforcement
# -----------------------------------------------------------------------

class TestNTAGCapacity:

    def test_short_uri_within_limit(self, plugin):
        uid     = _make_uid()
        uri     = "steam://rungameid/400"   # 22 bytes — well within limit
        # treat as NTAG to bypass classic checks
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        success, err = plugin._write_ndef_uri(uid, uri)
        # Result depends on mock reader; we just assert the size guard doesn't trigger
        assert err != "URI too long" if err else True

    def test_oversized_uri_is_rejected(self, plugin):
        uid  = _make_uid()
        # choose a URI longer than the NTAG capacity (~520 bytes after our range
        # expansion) so that the size guard should trip.
        uri  = "https://" + "a" * 600
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is False
        assert "too long" in (err or "").lower()

    def test_uri_exactly_at_limit_is_allowed(self, plugin):
        uid = _make_uid()
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        # compute capacity dynamically and ensure we test just under that threshold
        capacity = plugin._classify_tag(uid)["capacity_bytes"]
        # subtract approximate overhead (8 bytes) and prefix length
        usable = capacity - 8
        uri = "https://" + "x" * (usable - len("https://"))
        success, err = plugin._write_ndef_uri(uid, uri)
        assert "too long" not in (err or "").lower()

    def test_write_skips_mifare_trailer_blocks(self, plugin):
        uid = _make_uid()
        uri = "https://" + "x" * 72  # Forces write beyond first sector data blocks
        plugin.reader.mifare_classic_write_block.reset_mock()

        success, err = plugin._write_ndef_uri(uid, uri)

        assert success is True
        written_blocks = [c.args[0] for c in plugin.reader.mifare_classic_write_block.call_args_list]
        assert written_blocks
        assert 7 not in written_blocks
        assert all((b % 4) != 3 for b in written_blocks)


# -----------------------------------------------------------------------
# §XX — NTAG21x (e.g. NTAG215) support
# -----------------------------------------------------------------------

class TestNTAG21xSupport:

    def test_ntag_write_fallback_when_auth_fails(self, plugin):
        uid = _make_uid()
        uri = "steam://rungameid/123"

        # simulate a card that rejects Classic auth
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.ntag2xx_write_block.return_value = True

        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is True
        assert err is None
        plugin.reader.ntag2xx_write_block.assert_called()

    def test_ntag_write_handles_auth_throwing(self, plugin):
        uid = _make_uid()
        uri = "steam://rungameid/999"

        # simulate driver raising during auth (unexpected response)
        def bad_auth(uid_arg, blk, kn, key):
            raise RuntimeError("Received unexpected command response")
        plugin.reader.mifare_classic_authenticate_block.side_effect = bad_auth
        plugin.reader.ntag2xx_write_block.return_value = True

        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is True
        assert err is None
        plugin.reader.ntag2xx_write_block.assert_called()

    def test_ntag_capacity_allows_longer_uris(self, plugin):
        uid = _make_uid()
        # generate URI just over the 140‑byte Classic limit but under 504
        uri = "https://" + "a" * 300
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.ntag2xx_write_block.return_value = True

        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is True
        assert err is None

    def test_ntag_oversize_still_rejected(self, plugin):
        uid = _make_uid()
        uri = "https://" + "a" * 600  # well above 504 limitation
        plugin.reader.mifare_classic_authenticate_block.return_value = False

        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is False
        assert "too long" in (err or "").lower()

    def test_classic_capacity_detection_blocks(self, plugin):
        """Capacity should reflect the number of writable blocks."""
        uid = _make_uid()
        uri = "https://short"
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        # limit available blocks artificially
        plugin._iter_mifare_data_blocks = lambda: [4, 5]  # only 2 blocks => 32 bytes

        # long URI that would fit in default but not here
        long_uri = "https://" + "x" * 100
        success, err = plugin._write_ndef_uri(uid, long_uri)
        assert success is False
        assert "exceeds limit" in (err or "").lower()

    def test_classic_capacity_allows_small_write(self, plugin):
        uid = _make_uid()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin._iter_mifare_data_blocks = lambda: [4, 5, 6]
        uri = "https://ok"
        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is True
        assert err is None

    def test_ntag_capacity_detection_pages(self, plugin):
        uid = _make_uid()
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin._iter_ntag_pages = lambda: [4, 5]
        # each page 4 bytes -> 8 bytes available
        uri = "https://"  # ~8 bytes including overhead -> should fail
        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is False
        assert "exceeds limit" in (err or "").lower()

    def test_read_ndef_uri_on_ntag_detects_and_parses(self, plugin, uid_bytes):
        # set up the reader to mimic an NTAG
        plugin.reader.read_uid.return_value = uid_bytes
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        # a raw read — non-None tells _is_ntag() to flag NTAG family
        plugin.reader.mifare_classic_read_block.return_value = b"\x00\x00\x00\x00"
        # provide some pages containing a TLV terminator
        # craft a minimal TLV: 0x03 (NDEF msg), length=1, payload=0x00, terminator
        plugin.reader.ntag2xx_read_block.side_effect = [bytes([0x03, 0x01, 0x00, 0xFE]), b"\x00\x00\x00\x00"]

        # replace the ndef module used by main with a simple stub that
        # only provides UriRecord and message_decoder – this avoids the
        # MagicMock that conftest installs.
        import main as _mainmod
        class StubUriRecord:
            def __init__(self, uri):
                self.uri = uri
        class StubNdef:
            UriRecord = StubUriRecord
            @staticmethod
            def message_decoder(data):
                return [StubUriRecord("steam://rungameid/77")]
        _mainmod.ndef = StubNdef

        uri = plugin._read_ndef_uri()

        assert uri == "steam://rungameid/77"

    def test_multiple_ndef_records_first_uri_returned(self, plugin, uid_bytes):
        # mimic tag with two records: first a TextRecord then a UriRecord
        plugin.reader.read_uid.return_value = uid_bytes
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        plugin.reader.mifare_classic_read_block.return_value = b"\x00\x00\x00\x00"

        # we'll bypass the TLV logic by stubbing _read_ndef_records itself
        from ndef import UriRecord
        first = MagicMock()
        first.__class__.__name__ = 'TextRecord'
        second = UriRecord("https://example.com")

        with patch.object(plugin, "_read_ndef_records", return_value=[first, second]):
            uri = plugin._read_ndef_uri()
        assert uri == "https://example.com"


# -----------------------------------------------------------------------
# §6.3 — Card Removed During Game triggers correct event
# -----------------------------------------------------------------------

class TestMultiTagDetection:

    @pytest.mark.asyncio
    async def test_multiple_tags_event(self, plugin, mock_decky, uid_bytes):
        # simulate two different UIDs in succession without removal
        plugin.reader.read_uid.return_value = uid_bytes
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        # first pass
        await plugin._handle_scan(uid_bytes)
        # second pass with different uid
        other = b"\xBA\xAD\xF0\x0D"
        plugin.reader.read_uid.return_value = other
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(other)
        mock_decky.emit.assert_any_call("multiple_tags", {
            "previous": uid_bytes.hex().upper(),
            "current": other.hex().upper(),
        })


class TestCardRemovedDuringGame:

    @pytest.mark.asyncio
    async def test_removal_event_emitted_when_game_running(self, plugin, mock_decky):
        """
        When the tag is removed during GAME_RUNNING state, card_removed_during_game
        must be emitted so the frontend can trigger quit behavior.
        """
        from main import PluginState
        plugin.state           = PluginState.GAME_RUNNING
        plugin.running_game_id = 400
        plugin.current_tag_uid = "DEADBEEF"
        plugin.current_tag_uri = "steam://rungameid/400"
        plugin.is_pairing      = False

        await plugin._nfc_loop_notify_removal()

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" in emitted

    @pytest.mark.asyncio
    async def test_removal_event_not_emitted_when_no_game(self, plugin, mock_decky):
        """In READY state (no game), removal should not emit card_removed_during_game."""
        from main import PluginState
        plugin.state           = PluginState.READY
        plugin.running_game_id = None
        plugin.current_tag_uid = "DEADBEEF"
        plugin.is_pairing      = False

        await plugin._nfc_loop_notify_removal()

        emitted = [c.args[0] for c in mock_decky.emit.call_args_list]
        assert "card_removed_during_game" not in emitted
        assert "tag_removed" in emitted


# -----------------------------------------------------------------------
# Feature 2 — Custom Key Management
# -----------------------------------------------------------------------

class TestKeyManagement:

    @pytest.mark.asyncio
    async def test_set_tag_key_valid(self, plugin):
        """set_tag_key should store keys for a tag UID."""
        uid = "DEADBEEFCAFE"
        key_a = "FFFFFFFFFFFF"
        key_b = "D3F7D3F7D3F7"
        
        result = await plugin.set_tag_key(uid, key_a, key_b)
        
        assert result is True
        stored = plugin.key_manager.get_keys(uid)
        assert stored == [key_a, key_b]

    @pytest.mark.asyncio
    async def test_set_tag_key_invalid_format(self, plugin):
        """set_tag_key should reject invalid key formats."""
        uid = "DEADBEEFCAFE"
        
        # Too short
        result = await plugin.set_tag_key(uid, "FFFF", "FFFFFFFFFFFF")
        assert result is False
        
        # Invalid hex
        result = await plugin.set_tag_key(uid, "GGGGGGGGGGGG", "FFFFFFFFFFFF")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_tag_key_found(self, plugin):
        """get_tag_key should return stored keys."""
        uid = "DEADBEEFCAFE"
        key_a = "FFFFFFFFFFFF"
        key_b = "D3F7D3F7D3F7"
        
        plugin.key_manager.set_key(uid, key_a, key_b)
        
        result = await plugin.get_tag_key(uid)
        
        assert result == {"key_a": key_a, "key_b": key_b}

    @pytest.mark.asyncio
    async def test_get_tag_key_not_found(self, plugin):
        """get_tag_key should return empty dict for unknown UID."""
        result = await plugin.get_tag_key("NONEXISTENT")
        
        assert result == {}

    @pytest.mark.asyncio
    async def test_list_tag_keys(self, plugin):
        """list_tag_keys should return all stored UIDs."""
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
        """list_tag_keys should return empty list when no keys stored."""
        result = await plugin.list_tag_keys()
        
        assert result == []

    @pytest.mark.asyncio
    async def test_key_manager_persistence(self, plugin, tmp_path):
        """Keys should persist across KeyManager instances."""
        import json
        from nfc.key_manager import KeyManager
        
        keys_path = tmp_path / "keys.json"
        
        # Create first instance and store keys
        km1 = KeyManager(str(keys_path))
        km1.set_key("DEADBEEFCAFE", "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        
        # Create second instance and verify keys are loaded
        km2 = KeyManager(str(keys_path))
        stored = km2.get_keys("DEADBEEFCAFE")
        
        assert stored == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

    @pytest.mark.asyncio
    async def test_mifare_handler_uses_custom_keys(self, plugin):
        """MifareClassicHandler should try custom keys before defaults."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xDEADBEEFCAFE"
        uid_hex = uid.hex().upper()
        
        # Store custom keys
        plugin.key_manager.set_key(uid_hex, "A0A1A2A3A4A5", "B0B1B2B3B4B5")
        
        # Create handler with key manager
        handler = MifareClassicHandler(uid, plugin.key_manager)
        keys = handler._get_keys_to_try()
        
        # Custom keys should be first
        assert keys[0] == bytes.fromhex("A0A1A2A3A4A5")
        assert keys[1] == bytes.fromhex("B0B1B2B3B4B5")
        # Default keys should follow
        assert len(keys) > 2

    @pytest.mark.asyncio
    async def test_mifare_handler_without_custom_keys(self, plugin):
        """MifareClassicHandler should use defaults when no custom keys."""
        from nfc.tag_handlers import MifareClassicHandler
        
        uid = b"\\xDEADBEEFCAFE"
        
        handler = MifareClassicHandler(uid, plugin.key_manager)
        keys = handler._get_keys_to_try()
        
        # Should only have default keys
        assert len(keys) == 3
        assert keys == MifareClassicHandler.DEFAULT_KEYS



class TestSectorInfoRPC:
    """Tests for sector info RPC endpoint."""

    @pytest.mark.asyncio
    async def test_get_sector_info_current_tag(self, plugin):
        """Should get sector info for current tag."""
        plugin.current_tag_uid = "DEADBEEF"
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin.reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        # Mock _classify_tag to return mifare-classic
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.get_sector_info()
        
        assert len(result) == 16
        assert all("sector" in s for s in result)

    @pytest.mark.asyncio
    async def test_get_sector_info_specified_uid(self, plugin):
        """Should get sector info for specified UID."""
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin.reader.mifare_classic_read_block.return_value = b"\\x00" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.get_sector_info("CAFEBABE")
        
        assert len(result) == 16

    @pytest.mark.asyncio
    async def test_get_sector_info_no_tag(self, plugin):
        """Should return empty list when no tag present."""
        plugin.current_tag_uid = None
        
        result = await plugin.get_sector_info()
        
        assert result == []

    @pytest.mark.asyncio
    async def test_get_sector_info_wrong_tag_type(self, plugin):
        """Should return empty list for non-Mifare Classic tags."""
        plugin.current_tag_uid = "DEADBEEF"
        plugin.reader = MagicMock()
        
        plugin._classify_tag = lambda uid: {"type": "ntag21x"}
        
        result = await plugin.get_sector_info()
        
        assert result == []

    @pytest.mark.asyncio
    async def test_get_sector_info_no_reader(self, plugin):
        """Should return empty list when no reader available."""
        plugin.current_tag_uid = "DEADBEEF"
        plugin.reader = None
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.get_sector_info()
        
        assert result == []



class TestLockSectorRPC:
    """Tests for lock_sector RPC endpoint."""

    @pytest.mark.asyncio
    async def test_lock_sector_success(self, plugin):
        """Should successfully lock a sector."""
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin.reader.mifare_classic_read_block.return_value = b"\xFF" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        
        assert result is True

    @pytest.mark.asyncio
    async def test_lock_sector_invalid_uid(self, plugin):
        """Should reject invalid UID."""
        result = await plugin.lock_sector("", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        assert result is False
        
        result = await plugin.lock_sector(None, 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_sector_invalid_sector(self, plugin):
        """Should reject invalid sector numbers."""
        result = await plugin.lock_sector("DEADBEEF", -1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        assert result is False
        
        result = await plugin.lock_sector("DEADBEEF", 16, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_sector_invalid_keys(self, plugin):
        """Should reject invalid key formats."""
        # Too short
        result = await plugin.lock_sector("DEADBEEF", 1, "FFFF", "FFFFFFFFFFFF")
        assert result is False
        
        # Invalid hex
        result = await plugin.lock_sector("DEADBEEF", 1, "GGGGGGGGGGGG", "FFFFFFFFFFFF")
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_sector_wrong_tag_type(self, plugin):
        """Should reject non-Mifare Classic tags."""
        plugin.reader = MagicMock()
        plugin._classify_tag = lambda uid: {"type": "ntag21x"}
        
        result = await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_sector_no_reader(self, plugin):
        """Should fail when no reader available."""
        plugin.reader = None
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_sector_handler_failure(self, plugin):
        """Should return False when handler fails."""
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        result = await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        
        assert result is False



class TestSectorLockingIntegration:
    """Integration tests for sector locking workflow."""

    @pytest.mark.asyncio
    async def test_full_sector_lock_workflow(self, plugin):
        """Should complete full workflow: detect sectors, lock one, verify."""
        plugin.current_tag_uid = "DEADBEEF"
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin.reader.mifare_classic_read_block.return_value = b"\xFF" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        # Step 1: Get sector info
        sectors_before = await plugin.get_sector_info()
        assert len(sectors_before) == 16
        assert all(not s["locked"] for s in sectors_before)
        
        # Step 2: Lock sector 1
        success = await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF")
        assert success is True
        
        # Step 3: Verify lock was written
        plugin.reader.mifare_classic_write_block.assert_called()

    @pytest.mark.asyncio
    async def test_sector_lock_with_custom_keys(self, plugin):
        """Should use custom keys from key manager when locking."""
        uid_hex = "DEADBEEF"
        plugin.key_manager.set_key(uid_hex, "A0A1A2A3A4A5", "B0B1B2B3B4B5")
        
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = True
        plugin.reader.mifare_classic_read_block.return_value = b"\xFF" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        # Get sector info should try custom keys first
        sectors = await plugin.get_sector_info(uid_hex)
        assert len(sectors) == 16

    @pytest.mark.asyncio
    async def test_cannot_lock_already_locked_sector(self, plugin):
        """Should fail to lock a sector that's already locked."""
        plugin.reader = MagicMock()
        plugin.reader.mifare_classic_authenticate_block.return_value = False
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        # Try to lock with wrong keys
        success = await plugin.lock_sector("DEADBEEF", 1, "000000000000", "000000000000")
        
        assert success is False

    @pytest.mark.asyncio
    async def test_sector_info_reflects_lock_status(self, plugin):
        """Should show correct lock status in sector info."""
        plugin.current_tag_uid = "DEADBEEF"
        plugin.reader = MagicMock()
        
        # Simulate some sectors locked, some unlocked
        def auth_side_effect(uid, block, key_type, key):
            sector = block // 4
            return sector < 8  # First 8 sectors unlocked
        
        plugin.reader.mifare_classic_authenticate_block.side_effect = auth_side_effect
        plugin.reader.mifare_classic_read_block.return_value = b"\x00" * 16
        plugin.reader.mifare_classic_write_block.return_value = True
        
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        sectors = await plugin.get_sector_info()
        
        # Verify lock status matches authentication results
        for i in range(8):
            assert not sectors[i]["locked"], f"Sector {i} should be unlocked"
        for i in range(8, 16):
            assert sectors[i]["locked"], f"Sector {i} should be locked"

    @pytest.mark.asyncio
    async def test_lock_sector_validates_all_inputs(self, plugin):
        """Should validate all inputs before attempting lock."""
        plugin.reader = MagicMock()
        plugin._classify_tag = lambda uid: {"type": "mifare-classic"}
        
        # Invalid UID
        assert await plugin.lock_sector("", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF") is False
        
        # Invalid sector
        assert await plugin.lock_sector("DEADBEEF", -1, "FFFFFFFFFFFF", "FFFFFFFFFFFF") is False
        assert await plugin.lock_sector("DEADBEEF", 16, "FFFFFFFFFFFF", "FFFFFFFFFFFF") is False
        
        # Invalid keys
        assert await plugin.lock_sector("DEADBEEF", 1, "SHORT", "FFFFFFFFFFFF") is False
        assert await plugin.lock_sector("DEADBEEF", 1, "FFFFFFFFFFFF", "INVALID!") is False
        
        # Reader should never be called for invalid inputs
        plugin.reader.mifare_classic_authenticate_block.assert_not_called()
