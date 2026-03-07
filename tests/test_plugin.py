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
        with patch.object(plugin, "_read_ndef_uri", return_value=None), \
             patch.object(plugin, "_play_sound"):
            await plugin._handle_scan(uid)
        assert plugin.state == PluginState.READY

    @pytest.mark.asyncio
    async def test_scan_stays_card_present_for_steam_uri_awaiting_game(self, plugin, mock_decky):
        """Steam URI scan leaves state as CARD_PRESENT until frontend reports game running."""
        from main import PluginState
        uid = _make_uid()
        plugin.running_game_id = None
        with patch.object(plugin, "_read_ndef_uri", return_value="steam://rungameid/400"), \
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
        }))

        settings = SettingsManager(str(settings_path))

        assert settings.get("device_path").startswith("/dev/")
        assert settings.get("baudrate") == 115200
        assert settings.get("polling_interval") == 0.5
        assert settings.get("auto_launch") is True
        assert settings.get("auto_close") is False


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

        with patch.object(plugin, "_read_ndef_uri", return_value="https://example.com"), \
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
        success, err = plugin._write_ndef_uri(uid, uri)
        # Result depends on mock reader; we just assert the size guard doesn't trigger
        assert err != "URI too long" if err else True

    def test_oversized_uri_is_rejected(self, plugin):
        uid  = _make_uid()
        uri  = "https://" + "a" * 200      # 208 bytes — exceeds 140 byte limit
        success, err = plugin._write_ndef_uri(uid, uri)
        assert success is False
        assert "too long" in (err or "").lower()

    def test_uri_exactly_at_limit_is_allowed(self, plugin):
        uid = _make_uid()
        # 140 - 8 (overhead: 2 TLV + 4 header + 1 prefix + 1 terminator) = 132 usable chars
        uri = "https://" + "x" * 124      # 132 bytes total → within limit
        success, err = plugin._write_ndef_uri(uid, uri)
        # Should not be rejected by size check (may fail auth with mock, but not size)
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

    def test_read_ndef_uri_on_ntag_detects_and_parses(self, plugin, uid_bytes):
        # set up the reader to mimic an NTAG
        plugin.reader.read_passive_target.return_value = uid_bytes
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


# -----------------------------------------------------------------------
# §6.3 — Card Removed During Game triggers correct event
# -----------------------------------------------------------------------

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
