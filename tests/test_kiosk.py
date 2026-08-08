"""
test_kiosk.py — kid mode: the lock, and what it actually stops.

The lock is only worth anything if it holds at the RPC surface. Hiding the
panel's buttons is presentation; the panel is not the only way to reach a
plugin's RPCs, so every test in TestLockedRpcs calls the backend directly and
asserts it refuses.

The other half is the master key itself: recognised before any URI branch, so
it never launches and never reads as a blank tag; committed only once it is
physically written; and never paired over, because a game written onto the key
leaves a device that can be locked with a key that no longer exists.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from decky_links import master_key


def _load_event(uri, source_type=None, source_id="nfc:/dev/ttyUSB0", media_id="04AABBCC"):
    from sources.base import MediaEvent, MediaEventKind, SourceType
    return MediaEvent(
        kind=MediaEventKind.LOAD,
        source_type=source_type or SourceType.NFC,
        source_id=source_id,
        media_id=media_id,
        uri=uri,
        payload={},
    )


def _writable_nfc_source(plugin, write_result=(True, None)):
    """Swap the fixture's real NfcSource for one whose write we control.

    ``source_type`` matters: SourceManager.replace() keys off it, and a
    stand-in without one is registered *alongside* the real source, which then
    wins the pairing lookup and performs a write against a mock reader.
    """
    from sources.base import SourceType
    src = MagicMock()
    src.source_id = "nfc:/dev/ttyUSB0"
    src.source_type = SourceType.NFC
    src.can_write.return_value = True
    src.write_uri = AsyncMock(return_value=write_result)
    plugin.source_manager.replace(src)
    return src


def _register(plugin, token=None):
    """Put a key on the device, as a completed registration would."""
    token = token or master_key.mint_token()
    plugin.settings.set_kiosk("master_key_hash", master_key.hash_token(token))
    plugin.settings.set_kiosk("master_key_label", "NFC tag")
    return token


def _emitted(mock_decky, name):
    return [c.args[1] for c in mock_decky.emit.call_args_list if c.args[0] == name]


# ── What the lock stops ───────────────────────────────────────────────────────

class TestLockedRpcs:
    """Every one of these is refused in the backend, not in the panel."""

    @pytest.fixture
    def locked(self, plugin):
        _register(plugin)
        plugin.settings.set_kiosk("locked", True)
        return plugin

    @pytest.mark.asyncio
    async def test_start_pairing_is_refused(self, locked):
        assert await locked.start_pairing("steam://rungameid/400") is False
        assert locked.is_pairing is False

    @pytest.mark.asyncio
    async def test_set_setting_is_refused(self, locked):
        assert await locked.set_setting("auto_launch", False) is False
        assert locked.settings.get("auto_launch") is True

    @pytest.mark.asyncio
    async def test_set_source_setting_is_refused(self, locked):
        assert await locked.set_source_setting("nfc", "enabled", False) is False

    @pytest.mark.asyncio
    async def test_format_media_is_refused(self, locked):
        result = await locked.format_media("/dev/sda")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_set_tag_key_is_refused(self, locked):
        assert await locked.set_tag_key("04AABBCC", "FFFFFFFFFFFF", "FFFFFFFFFFFF") is False

    @pytest.mark.asyncio
    async def test_lock_sector_is_refused(self, locked):
        assert await locked.lock_sector("04AABBCC", 1, "FFFFFFFFFFFF", "FFFFFFFFFFFF") is False

    @pytest.mark.asyncio
    async def test_registering_another_key_is_refused(self, locked):
        """Otherwise the lock could be reopened by registering a new key."""
        assert await locked.register_master_key() is False

    @pytest.mark.asyncio
    async def test_clearing_the_key_is_refused(self, locked):
        assert await locked.clear_master_key() is False
        assert locked.settings.get_kiosk("master_key_hash") != ""

    @pytest.mark.asyncio
    async def test_storing_a_pin_is_refused(self, locked):
        assert await locked.set_family_view_pin("1234") is False

    @pytest.mark.asyncio
    async def test_the_panel_cannot_unlock(self, locked):
        """A panel button that undid the lock would mean the key protects
        nothing. Unlocking is the master key's job, or Steam's PIN prompt."""
        assert await locked.set_kiosk_locked(False) is False
        assert locked.locked is True

    @pytest.mark.asyncio
    async def test_games_still_launch(self, locked, mock_decky):
        """Kid mode restricts writing, not playing. Which games are allowed is
        Family View's answer, and it is enforced in the frontend."""
        await locked._handle_media_load(_load_event("steam://rungameid/400"))
        assert [e for e in _emitted(mock_decky, "uri_detected")
                if e.get("uri") == "steam://rungameid/400"]


# ── Locking and unlocking ─────────────────────────────────────────────────────

class TestLocking:

    @pytest.mark.asyncio
    async def test_locking_needs_a_registered_key(self, plugin):
        """A lock whose key does not exist is a device nobody can get back
        into — the panel offers no unlock button by design."""
        assert await plugin.set_kiosk_locked(True) is False
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_locks_from_the_panel_once_a_key_exists(self, plugin):
        _register(plugin)
        assert await plugin.set_kiosk_locked(True) is True
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_locking_cancels_an_armed_pairing(self, plugin):
        """A pairing armed before the lock came down would write the next
        medium presented, which is what the lock exists to stop."""
        _register(plugin)
        await plugin.start_pairing("steam://rungameid/400")
        assert plugin.is_pairing is True
        await plugin.set_kiosk_locked(True)
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_the_pin_never_appears_in_the_state_rpc(self, plugin):
        """The panel needs to know a PIN exists, never what it is."""
        _register(plugin)
        await plugin.set_family_view_pin("4321")
        state = await plugin.get_kiosk_state()
        assert state["has_pin"] is True
        assert "4321" not in str(state)

    @pytest.mark.asyncio
    async def test_the_key_hash_never_appears_in_the_state_rpc(self, plugin):
        token = _register(plugin)
        state = await plugin.get_kiosk_state()
        assert state["has_master_key"] is True
        assert master_key.hash_token(token) not in str(state)

    @pytest.mark.asyncio
    async def test_unlock_event_carries_the_pin_for_the_frontend(self, plugin, mock_decky):
        """Only the frontend can call Steam, so the PIN travels exactly once,
        at the moment it is needed."""
        token = _register(plugin)
        await plugin.set_family_view_pin("4321")
        await plugin.set_kiosk_locked(True)
        mock_decky.emit.reset_mock()

        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))

        events = _emitted(mock_decky, "kiosk_lock")
        assert events and events[-1]["locked"] is False
        assert events[-1]["pin"] == "4321"

    @pytest.mark.asyncio
    async def test_lock_event_does_not_carry_the_pin(self, plugin, mock_decky):
        """Locking Family View needs no secret, so nothing is handed over."""
        token = _register(plugin)
        await plugin.set_family_view_pin("4321")
        mock_decky.emit.reset_mock()

        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))

        events = _emitted(mock_decky, "kiosk_lock")
        assert events and events[-1]["locked"] is True
        assert "pin" not in events[-1]


# ── The launch rule ───────────────────────────────────────────────────────────

class TestOnlyMediaLaunchedGamesRun:
    """Kid mode's answer to "which games may run", and why it needs no list.

    Steam cannot answer it for us: Family View is the only per-account
    restriction that applies to the account holding the library, and Steam no
    longer offers to set it up on accounts that never had it — Steam Families
    replaced it, and those controls only bind *child* accounts.

    So the rule is the plugin's own: the Deck plays what you hand it. The
    allowlist is the box of tags and disks, which means there is nothing to
    maintain and nothing to drift.
    """

    @pytest.fixture
    def locked(self, plugin):
        _register(plugin)
        plugin.settings.set_kiosk("locked", True)
        return plugin

    @pytest.mark.asyncio
    async def test_a_game_started_from_the_library_is_restricted(self, locked, mock_decky):
        await locked.set_running_game(400)
        assert _emitted(mock_decky, "restricted_game") == [{"appid": 400}]

    @pytest.mark.asyncio
    async def test_a_game_a_medium_launched_is_allowed(self, locked, mock_decky):
        await locked._handle_media_load(_load_event("steam://rungameid/400"))
        await locked.set_running_game(400)
        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_a_presented_medium_vouches_without_having_launched_it(
        self, locked, mock_decky,
    ):
        """With auto-launch off the plugin only opens the game's page and the
        user presses Play, so the launch has no attribution at all. The disk is
        still in the drive, and that is the thing kid mode is really asking
        about."""
        locked.settings.set("auto_launch", False)
        await locked._handle_media_load(_load_event("steam://run/620"))
        locked._registry.clear_launch()

        await locked.set_running_game(620)

        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_a_different_game_than_the_medium_names_is_restricted(
        self, locked, mock_decky,
    ):
        locked.settings.set("auto_launch", False)
        await locked._handle_media_load(_load_event("steam://run/620"))
        locked._registry.clear_launch()

        await locked.set_running_game(400)

        assert _emitted(mock_decky, "restricted_game") == [{"appid": 400}]

    @pytest.mark.asyncio
    async def test_a_shortcut_medium_matches_its_running_app_id(self, locked, mock_decky):
        """The medium carries a gameID64; Steam reports the app id. Comparing
        the two as strings would restrict every non-Steam game."""
        from decky_links import uri as uri_rules
        gameid64 = str(((0x80000001 | 0x80000000) << 32) | 0x02000000)
        locked.settings.set("auto_launch", False)
        await locked._handle_media_load(_load_event(f"steam://rungameid/{gameid64}"))
        locked._registry.clear_launch()

        await locked.set_running_game(int(uri_rules.launch_appid(f"steam://rungameid/{gameid64}")))

        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_nothing_is_restricted_while_unlocked(self, plugin, mock_decky):
        await plugin.set_running_game(400)
        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_a_game_already_running_is_left_alone_when_the_lock_comes_down(
        self, plugin, mock_decky,
    ):
        """Locking mid-session must not kill the game being played — that is
        lost progress for whoever is holding the Deck."""
        _register(plugin)
        await plugin.set_running_game(400)
        mock_decky.emit.reset_mock()

        await plugin.set_kiosk_locked(True)
        await plugin.set_running_game(400)   # the frontend re-reports it

        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_the_game_exiting_is_not_a_restriction(self, locked, mock_decky):
        await locked.set_running_game(None)
        assert _emitted(mock_decky, "restricted_game") == []


# ── The key, presented ────────────────────────────────────────────────────────

class TestMasterKeyPresented:

    @pytest.mark.asyncio
    async def test_toggles_the_lock_on(self, plugin):
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_toggles_the_lock_off_again(self, plugin):
        """One physical object, both directions — which is all a single tag
        can express."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_never_launches(self, plugin, mock_decky):
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert all(e.get("uri") is None for e in _emitted(mock_decky, "uri_detected"))

    @pytest.mark.asyncio
    async def test_the_token_never_reaches_the_frontend(self, plugin, mock_decky):
        """It is the credential. Nothing emitted may carry it."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert token not in str(mock_decky.emit.call_args_list)

    @pytest.mark.asyncio
    async def test_the_token_never_reaches_the_media_registry(self, plugin):
        """get_active_media is polled every five seconds by the panel."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert token not in str(await plugin.get_active_media())

    @pytest.mark.asyncio
    async def test_registry_marks_it_as_a_master_medium(self, plugin):
        """So the panel labels the row rather than offering to pair over it."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert [m for m in await plugin.get_active_media() if m.get("master")]

    @pytest.mark.asyncio
    async def test_an_unregistered_key_does_not_unlock(self, plugin):
        _register(plugin)
        plugin.settings.set_kiosk("locked", True)
        stranger = master_key.uri_for(master_key.mint_token())
        await plugin._handle_media_load(_load_event(stranger))
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_an_unregistered_key_says_so(self, plugin, mock_decky):
        """A key that stopped being recognised looks exactly like a reader
        that stopped reading, unless it is named."""
        _register(plugin)
        stranger = master_key.uri_for(master_key.mint_token())
        await plugin._handle_media_load(_load_event(stranger))
        events = _emitted(mock_decky, "uri_detected")
        assert events and events[-1]["master"] is True
        assert events[-1]["authorized"] is False

    @pytest.mark.asyncio
    async def test_a_master_medium_is_not_reported_as_blank(self, plugin, mock_decky):
        """It carries no URI by design. Reported blank, it would get an error
        sound and a Pair button offering to overwrite the key."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert all(not e.get("blank") for e in _emitted(mock_decky, "uri_detected"))

    @pytest.mark.asyncio
    async def test_does_not_forget_a_running_game(self, plugin):
        """Same rule as any other unusable medium: presenting one is not a
        reason to drop out of GAME_RUNNING and lose auto-close."""
        from main import PluginState
        token = _register(plugin)
        plugin.running_game_id = 400
        plugin.state = PluginState.GAME_RUNNING
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert plugin.state == PluginState.GAME_RUNNING


# ── Registration ──────────────────────────────────────────────────────────────

class TestRegistration:

    @pytest.mark.asyncio
    async def test_arms_pairing_with_a_master_payload(self, plugin):
        assert await plugin.register_master_key() is True
        assert plugin.is_pairing is True
        assert master_key.parse_token(plugin.pairing_uri) is not None

    @pytest.mark.asyncio
    async def test_the_payload_is_not_launchable(self, plugin):
        """A control token is not something a tapped card may launch, so it
        must fail the allowlist the launch path uses."""
        from decky_links import uri as uri_rules
        await plugin.register_master_key()
        assert uri_rules.is_valid(plugin.pairing_uri) is False

    @pytest.mark.asyncio
    async def test_key_is_committed_only_after_the_write_succeeds(self, plugin):
        """Recording it when the button was pressed would lock the device to a
        key the write then failed to put on any medium."""
        _writable_nfc_source(plugin, write_result=(False, "write failed"))

        await plugin.register_master_key("nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert plugin.settings.get_kiosk("master_key_hash") == ""

    @pytest.mark.asyncio
    async def test_key_is_committed_when_the_write_succeeds(self, plugin):
        _writable_nfc_source(plugin)

        await plugin.register_master_key("nfc:/dev/ttyUSB0")
        token = master_key.parse_token(plugin.pairing_uri)
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert master_key.matches(token, plugin.settings.get_kiosk("master_key_hash"))

    @pytest.mark.asyncio
    async def test_registering_again_replaces_the_old_key(self, plugin):
        _writable_nfc_source(plugin)
        old = _register(plugin)

        await plugin.register_master_key("nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert master_key.matches(old, plugin.settings.get_kiosk("master_key_hash")) is False

    @pytest.mark.asyncio
    async def test_pairing_a_game_over_the_master_key_is_refused(self, plugin, mock_decky):
        """The medium would keep working as a game tag while the registered
        hash stayed behind — a device lockable by a key that no longer is one."""
        token = _register(plugin)
        source = _writable_nfc_source(plugin)

        # Twice: presenting the key toggles the lock, and pairing is refused
        # outright while locked — so this test needs the registry to know the
        # medium is the key while the device is *un*locked.
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        await plugin._handle_media_load(_load_event(master_key.uri_for(token)))
        assert plugin.locked is False

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        source.write_uri.assert_not_awaited()
        results = _emitted(mock_decky, "pairing_result")
        assert results and results[-1]["success"] is False

    @pytest.mark.asyncio
    async def test_an_unrecognised_master_medium_can_still_be_paired(self, plugin):
        """Someone else's key, or ours after it was replaced, is just a medium
        with a stale payload. Refusing to pair it left it unusable for good,
        with no way back from the panel."""
        _register(plugin)
        source = _writable_nfc_source(plugin)
        stranger = master_key.uri_for(master_key.mint_token())

        await plugin._handle_media_load(_load_event(stranger))
        assert plugin.locked is False, "an unknown key must not have locked it"

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        source.write_uri.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pairing_a_game_clears_the_master_flag(self, plugin):
        """Otherwise the row keeps reading "Master key" over a medium that now
        holds a game, until it is removed and presented again."""
        _register(plugin)
        _writable_nfc_source(plugin)
        stranger = master_key.uri_for(master_key.mint_token())

        await plugin._handle_media_load(_load_event(stranger))
        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        media = await plugin.get_active_media()
        assert media and media[0]["master"] is False
        assert media[0]["uri"] == "steam://rungameid/400"
