"""
test_restricted.py — restricted mode: the lock, and what it actually stops.

The lock is only worth anything if it holds at the RPC surface. Hiding the
panel's buttons is presentation; the panel is not the only way to reach a
plugin's RPCs, so every test in TestLockedRpcs calls the backend directly and
asserts it refuses.

**The lock is derived, not stored.** A key registered and present means
unlocked; registered and absent means locked. That is why so many tests here
lock a device simply by registering a key and presenting nothing: there is no
lock flag to set, and no way for one to disagree with the medium in the drive.

The other half is the key itself: recognised before any URI branch, so it never
launches and never reads as a blank tag; committed only once it is physically
written; and never paired over, because a game written onto the key leaves a
device that can be locked with a key that no longer exists.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from decky_links import restricted_key


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
    """Register a key, as a completed registration would.

    With no medium presented this also *locks* the device, because that is what
    the lock is: a registered key that is not here.
    """
    token = token or restricted_key.mint_token()
    plugin.settings.set_restricted("key_hash", restricted_key.hash_token(token))
    plugin.settings.set_restricted("key_label", "NFC tag")
    return token


async def _present_key(plugin, token, **kwargs):
    """Put the key on a reader, which unlocks."""
    await plugin._handle_media_load(_load_event(restricted_key.uri_for(token), **kwargs))


def _unload_event(source_id="nfc:/dev/ttyUSB0", media_id="04AABBCC"):
    from sources.base import MediaEvent, MediaEventKind, SourceType
    return MediaEvent(
        kind=MediaEventKind.UNLOAD,
        source_type=SourceType.NFC,
        source_id=source_id,
        media_id=media_id,
        uri=None,
        payload={},
    )


async def _register_by_presenting(plugin):
    """Register a key the way the panel does: arm, then present the medium.

    The whole sequence matters here — arming alone records nothing, and it is
    the presentation that both writes the token and puts the medium in the
    registry, which is what makes the freshly-made key count as present.
    """
    source = _writable_nfc_source(plugin)
    await plugin.register_key("nfc:/dev/ttyUSB0")
    await plugin._handle_media_load(_load_event(""))
    return source


def _emitted(mock_decky, name):
    return [c.args[1] for c in mock_decky.emit.call_args_list if c.args[0] == name]


# ── What the lock stops ───────────────────────────────────────────────────────

class TestLockedRpcs:
    """Every one of these is refused in the backend, not in the panel."""

    @pytest.fixture
    def locked(self, plugin):
        """A key registered and not present — which is all "locked" means."""
        _register(plugin)
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
        assert await locked.register_key() is False

    @pytest.mark.asyncio
    async def test_clearing_the_key_is_refused(self, locked):
        assert await locked.disable_key() is False
        assert locked.settings.get_restricted("key_hash") != ""

    @pytest.mark.asyncio
    async def test_storing_a_pin_is_refused(self, locked):
        assert await locked.set_family_view_pin("1234") is False

    @pytest.mark.asyncio
    async def test_there_is_no_rpc_that_unlocks(self, locked):
        """The only way out is the key. The lock is derived from what is
        present, so there is nothing for an RPC to set."""
        assert not hasattr(locked, "set_restricted_locked")
        assert locked.locked is True

    @pytest.mark.asyncio
    async def test_games_still_launch(self, locked, mock_decky):
        """Restricted mode restricts writing, not playing — which games may
        run is the launch rule's business, tested below."""
        await locked._handle_media_load(_load_event("steam://rungameid/400"))
        assert [e for e in _emitted(mock_decky, "uri_detected")
                if e.get("uri") == "steam://rungameid/400"]


# ── Locking and unlocking ─────────────────────────────────────────────────────

class TestLocking:
    """The lock is where the key is.

    There is no lock flag and no lock button. Every state below is reached by
    putting the key somewhere or taking it away, which is the whole reason the
    plugin and the panel cannot end up disagreeing about whether the Deck is
    locked.
    """

    @pytest.mark.asyncio
    async def test_no_key_means_no_restrictions(self, plugin):
        """Restricted mode is off until a key exists, so a fresh install is
        not a device its owner has to unlock."""
        assert plugin.key_registered is False
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_a_registered_key_that_is_absent_is_a_locked_device(self, plugin):
        _register(plugin)
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_presenting_the_key_unlocks(self, plugin):
        token = _register(plugin)
        await _present_key(plugin, token)
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_removing_the_key_locks_again(self, plugin):
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin._handle_media_unload(_unload_event())
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_presenting_it_twice_does_not_lock_it_again(self, plugin):
        """It is presence, not a toggle. A source re-reporting the medium it
        is holding — which storage does on a rearm — must not flip the lock."""
        token = _register(plugin)
        await _present_key(plugin, token)
        await _present_key(plugin, token)
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_removing_some_other_medium_does_not_lock(self, plugin):
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin._handle_media_unload(_unload_event(source_id="storage:udev"))
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_a_restart_with_the_key_out_comes_up_locked(self, plugin):
        """Nothing is remembered, so nothing has to be trusted: an empty
        registry is a key that is not here."""
        _register(plugin)
        plugin._registry.reset()
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_locking_cancels_an_armed_pairing(self, plugin):
        """A pairing armed before the lock came down would write the next
        medium presented, which is what the lock exists to stop."""
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin.start_pairing("steam://rungameid/400")
        assert plugin.is_pairing is True

        await plugin._handle_media_unload(_unload_event())

        assert plugin.locked is True
        assert plugin.is_pairing is False

    @pytest.mark.asyncio
    async def test_the_lock_is_announced_once_per_change(self, plugin, mock_decky):
        token = _register(plugin)
        await _present_key(plugin, token)
        await _present_key(plugin, token)
        assert len(_emitted(mock_decky, "restricted_lock")) == 1

    @pytest.mark.asyncio
    async def test_the_pin_never_appears_in_the_state_rpc(self, plugin):
        """The panel needs to know a PIN exists, never what it is."""
        token = _register(plugin)
        await _present_key(plugin, token)     # configuring needs to be unlocked
        await plugin.set_family_view_pin("4321")
        state = await plugin.get_restricted_state()
        assert state["has_pin"] is True
        assert "4321" not in str(state)

    @pytest.mark.asyncio
    async def test_the_key_hash_never_appears_in_the_state_rpc(self, plugin):
        token = _register(plugin)
        state = await plugin.get_restricted_state()
        assert state["has_key"] is True
        assert restricted_key.hash_token(token) not in str(state)

    @pytest.mark.asyncio
    async def test_unlock_event_carries_the_pin_for_the_frontend(self, plugin, mock_decky):
        """Only the frontend can call Steam, so the PIN travels exactly once,
        at the moment it is needed."""
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin.set_family_view_pin("4321")
        await plugin._handle_media_unload(_unload_event())
        mock_decky.emit.reset_mock()

        await _present_key(plugin, token)

        events = _emitted(mock_decky, "restricted_lock")
        assert events and events[-1]["locked"] is False
        assert events[-1]["pin"] == "4321"

    @pytest.mark.asyncio
    async def test_lock_event_does_not_carry_the_pin(self, plugin, mock_decky):
        """Locking Family View needs no secret, so nothing is handed over."""
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin.set_family_view_pin("4321")
        mock_decky.emit.reset_mock()

        await plugin._handle_media_unload(_unload_event())

        events = _emitted(mock_decky, "restricted_lock")
        assert events and events[-1]["locked"] is True
        assert "pin" not in events[-1]


# ── The launch rule ───────────────────────────────────────────────────────────

class TestOnlyMediaLaunchedGamesRun:
    """Restricted mode's answer to "which games may run", and why it needs no list.

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
        """A key registered and not present — which is all "locked" means."""
        _register(plugin)
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
        still in the drive, and that is the thing restricted mode is really asking
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
        """Taking the key out mid-session must not kill the game being played
        — that is lost progress for whoever is holding the Deck."""
        token = _register(plugin)
        await _present_key(plugin, token)
        await plugin.set_running_game(400)
        mock_decky.emit.reset_mock()

        await plugin._handle_media_unload(_unload_event())
        await plugin.set_running_game(400)   # the frontend re-reports it

        assert plugin.locked is True
        assert _emitted(mock_decky, "restricted_game") == []

    @pytest.mark.asyncio
    async def test_the_game_exiting_is_not_a_restriction(self, locked, mock_decky):
        await locked.set_running_game(None)
        assert _emitted(mock_decky, "restricted_game") == []


# ── The key, presented ────────────────────────────────────────────────────────

class TestKeyPresented:

    @pytest.mark.asyncio
    async def test_never_launches(self, plugin, mock_decky):
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert all(e.get("uri") is None for e in _emitted(mock_decky, "uri_detected"))

    @pytest.mark.asyncio
    async def test_the_token_never_reaches_the_frontend(self, plugin, mock_decky):
        """It is the credential. Nothing emitted may carry it."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert token not in str(mock_decky.emit.call_args_list)

    @pytest.mark.asyncio
    async def test_the_token_never_reaches_the_media_registry(self, plugin):
        """get_active_media is polled every five seconds by the panel."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert token not in str(await plugin.get_active_media())

    @pytest.mark.asyncio
    async def test_registry_marks_it_as_a_key_medium(self, plugin):
        """So the panel labels the row rather than offering to pair over it."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert [m for m in await plugin.get_active_media() if m.get("key")]

    @pytest.mark.asyncio
    async def test_an_unregistered_key_does_not_unlock(self, plugin):
        _register(plugin)
        stranger = restricted_key.uri_for(restricted_key.mint_token())
        await plugin._handle_media_load(_load_event(stranger))
        assert plugin.locked is True

    @pytest.mark.asyncio
    async def test_an_unregistered_key_says_so(self, plugin, mock_decky):
        """A key that stopped being recognised looks exactly like a reader
        that stopped reading, unless it is named."""
        _register(plugin)
        stranger = restricted_key.uri_for(restricted_key.mint_token())
        await plugin._handle_media_load(_load_event(stranger))
        events = _emitted(mock_decky, "uri_detected")
        assert events and events[-1]["key"] is True
        assert events[-1]["authorized"] is False

    @pytest.mark.asyncio
    async def test_a_key_medium_is_not_reported_as_blank(self, plugin, mock_decky):
        """It carries no URI by design. Reported blank, it would get an error
        sound and a Pair button offering to overwrite the key."""
        token = _register(plugin)
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert all(not e.get("blank") for e in _emitted(mock_decky, "uri_detected"))

    @pytest.mark.asyncio
    async def test_does_not_forget_a_running_game(self, plugin):
        """Same rule as any other unusable medium: presenting one is not a
        reason to drop out of GAME_RUNNING and lose auto-close."""
        from main import PluginState
        token = _register(plugin)
        plugin.running_game_id = 400
        plugin.state = PluginState.GAME_RUNNING
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert plugin.state == PluginState.GAME_RUNNING


# ── Registration ──────────────────────────────────────────────────────────────

class TestRegistration:

    @pytest.mark.asyncio
    async def test_arms_pairing_with_a_key_payload(self, plugin):
        assert await plugin.register_key() is True
        assert plugin.is_pairing is True
        assert restricted_key.parse_token(plugin.pairing_uri) is not None

    @pytest.mark.asyncio
    async def test_the_payload_is_not_launchable(self, plugin):
        """A control token is not something a tapped card may launch, so it
        must fail the allowlist the launch path uses."""
        from decky_links import uri as uri_rules
        await plugin.register_key()
        assert uri_rules.is_valid(plugin.pairing_uri) is False

    @pytest.mark.asyncio
    async def test_key_is_committed_only_after_the_write_succeeds(self, plugin):
        """Recording it when the button was pressed would lock the device to a
        key the write then failed to put on any medium."""
        _writable_nfc_source(plugin, write_result=(False, "write failed"))

        await plugin.register_key("nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert plugin.settings.get_restricted("key_hash") == ""

    @pytest.mark.asyncio
    async def test_key_is_committed_when_the_write_succeeds(self, plugin):
        _writable_nfc_source(plugin)

        await plugin.register_key("nfc:/dev/ttyUSB0")
        token = restricted_key.parse_token(plugin.pairing_uri)
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert restricted_key.matches(token, plugin.settings.get_restricted("key_hash"))

    @pytest.mark.asyncio
    async def test_registering_again_replaces_the_old_key(self, plugin):
        _writable_nfc_source(plugin)
        old = _register(plugin)
        # Replacing a key happens while unlocked, which means the old one is
        # present — here on a second trigger, so the new one goes to the tag.
        await _present_key(plugin, old, source_id="storage:udev", media_id="/dev/sda1")

        await plugin.register_key("nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        assert restricted_key.matches(old, plugin.settings.get_restricted("key_hash")) is False

    @pytest.mark.asyncio
    async def test_pairing_a_game_over_the_key_is_refused(self, plugin, mock_decky):
        """The medium would keep working as a game tag while the registered
        hash stayed behind — a device lockable by a key that no longer is one."""
        token = _register(plugin)
        source = _writable_nfc_source(plugin)

        # Twice: presenting the key toggles the lock, and pairing is refused
        # outright while locked — so this test needs the registry to know the
        # medium is the key while the device is *un*locked.
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        await plugin._handle_media_load(_load_event(restricted_key.uri_for(token)))
        assert plugin.locked is False

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        source.write_uri.assert_not_awaited()
        results = _emitted(mock_decky, "pairing_result")
        assert results and results[-1]["success"] is False

    @pytest.mark.asyncio
    async def test_an_unrecognised_key_medium_can_still_be_paired(self, plugin):
        """Someone else's key, or ours after it was replaced, is just a medium
        with a stale payload. Refusing to pair it left it unusable for good,
        with no way back from the panel."""
        token = _register(plugin)
        source = _writable_nfc_source(plugin)
        # Our own key in the drive, so the device is unlocked; a stranger's on
        # the reader, which is the medium being paired.
        await _present_key(plugin, token, source_id="storage:udev", media_id="/dev/sda1")
        stranger = restricted_key.uri_for(restricted_key.mint_token())

        await plugin._handle_media_load(_load_event(stranger))
        assert plugin.locked is False, "an unknown key must not have locked it"

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        source.write_uri.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pairing_a_game_clears_the_key_flag(self, plugin):
        """Otherwise the row keeps reading "Key" over a medium that now
        holds a game, until it is removed and presented again."""
        token = _register(plugin)
        _writable_nfc_source(plugin)
        await _present_key(plugin, token, source_id="storage:udev", media_id="/dev/sda1")
        stranger = restricted_key.uri_for(restricted_key.mint_token())

        await plugin._handle_media_load(_load_event(stranger))
        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_pairing("04AABBCC", source_id="nfc:/dev/ttyUSB0")

        paired = next(m for m in await plugin.get_active_media()
                      if m["source_id"] == "nfc:/dev/ttyUSB0")
        assert paired["key"] is False
        assert paired["uri"] == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_a_cancelled_registration_cannot_be_committed_later(self, plugin):
        """The worst failure this feature has: a token left pending after a
        cancel was committed by whichever pairing succeeded next, registering
        a key whose token is on no medium at all — a locked device with a key
        that cannot exist."""
        source = _writable_nfc_source(plugin)
        await plugin.register_key("nfc:/dev/ttyUSB0")
        await plugin.cancel_pairing()

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_media_load(_load_event(""))

        source.write_uri.assert_awaited_once_with(
            "04AABBCC", "steam://rungameid/400", ""
        )
        assert plugin.key_registered is False

    @pytest.mark.asyncio
    async def test_arming_a_game_pairing_drops_a_pending_key(self, plugin):
        """Same failure by the other route: the user starts a registration,
        changes their mind and pairs a game instead without cancelling."""
        _writable_nfc_source(plugin)
        await plugin.register_key("nfc:/dev/ttyUSB0")

        await plugin.start_pairing("steam://rungameid/400", "nfc:/dev/ttyUSB0")
        await plugin._handle_media_load(_load_event(""))

        assert plugin.key_registered is False

    @pytest.mark.asyncio
    async def test_the_key_goes_to_the_trigger_it_was_asked_for(self, plugin):
        """The panel targets the row the user pressed. Untargeted, the key went
        to whichever source the backend read first — with a tag on the reader
        and a stick in a drive, there was no way to say which."""
        from sources.base import SourceType
        chosen = MagicMock()
        chosen.source_id = "storage:udev"
        chosen.source_type = SourceType.STORAGE
        chosen.can_write.return_value = True
        chosen.write_uri = AsyncMock(return_value=(True, None))
        plugin.source_manager.replace(chosen)
        other = _writable_nfc_source(plugin)

        await plugin.register_key("storage:udev")
        await plugin._handle_media_load(
            _load_event("", source_id="storage:udev", media_id="/dev/sda1")
        )

        chosen.write_uri.assert_awaited_once()
        other.write_uri.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_trigger_that_cannot_be_written_is_refused(self, plugin):
        assert await plugin.register_key("camera:/dev/video0") is False

    @pytest.mark.asyncio
    async def test_registering_switches_restricted_mode_on(self, plugin):
        """The key is the switch. There is no separate "enable" step to forget
        — and none to be in a different state from the key."""
        await _register_by_presenting(plugin)
        assert plugin.key_registered is True

    @pytest.mark.asyncio
    async def test_registering_leaves_the_device_unlocked(self, plugin):
        """The key was just written, so it is right there — locking the user
        out at the moment they made the key would be absurd."""
        await _register_by_presenting(plugin)
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_taking_the_new_key_away_locks_it(self, plugin):
        await _register_by_presenting(plugin)
        await plugin._handle_media_unload(_unload_event())
        assert plugin.locked is True


# ── Disabling the key ─────────────────────────────────────────────────────────

class TestDisableKey:
    """Switching restricted mode off, which also wipes the medium.

    Both halves or neither: a medium still carrying a token the device has
    forgotten reads as "Unknown key" forever, and a registered hash with no
    medium is a lock with no key.
    """

    async def _armed_and_unlocked(self, plugin, erase_result=(True, None)):
        source = _writable_nfc_source(plugin)
        source.erase = AsyncMock(return_value=erase_result)
        token = _register(plugin)
        await _present_key(plugin, token)
        return source

    @pytest.mark.asyncio
    async def test_switches_restricted_mode_off(self, plugin):
        await self._armed_and_unlocked(plugin)
        assert await plugin.disable_key() is True
        assert plugin.key_registered is False
        assert plugin.locked is False

    @pytest.mark.asyncio
    async def test_erases_the_medium(self, plugin):
        source = await self._armed_and_unlocked(plugin)
        await plugin.disable_key()
        source.erase.assert_awaited_once_with("04AABBCC")

    @pytest.mark.asyncio
    async def test_the_medium_stops_reading_as_a_key(self, plugin):
        await self._armed_and_unlocked(plugin)
        await plugin.disable_key()
        media = await plugin.get_active_media()
        assert media and media[0]["key"] is False

    @pytest.mark.asyncio
    async def test_a_failed_erase_leaves_the_key_registered(self, plugin):
        """Otherwise the feature is off while the stick still looks like a
        key, and nothing has told the user which of the two is true."""
        await self._armed_and_unlocked(plugin, erase_result=(False, "read-only"))
        assert await plugin.disable_key() is False
        assert plugin.key_registered is True

    @pytest.mark.asyncio
    async def test_refused_when_the_key_is_not_present(self, plugin):
        """Which is the same thing as being locked — this is belt and braces
        for the case where the registry has lost it some other way."""
        _writable_nfc_source(plugin)
        _register(plugin)
        assert await plugin.disable_key() is False
        assert plugin.key_registered is True
