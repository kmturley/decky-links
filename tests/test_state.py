"""
test_state.py — the state machine, as a table.

The rules used to be ten inline conditionals across four handlers in main.py.
Asking "what happens if a game is running and a blank disk goes in" meant
tracing three methods by hand — and that question had a wrong answer, which is
the point of this file existing.

Every test here is a pure function call. None of them needs a Plugin, a reader,
or an event loop.
"""
import pytest

from decky_links import state
from decky_links.state import PluginState as S


IDLE, READY, CARD, GAME = S.IDLE, S.READY, S.CARD_PRESENT, S.GAME_RUNNING
ALL_STATES = (IDLE, READY, CARD, GAME)


# ── Source lifecycle ──────────────────────────────────────────────────────────

class TestSourceConnected:

    @pytest.mark.parametrize("current,expected", [
        (IDLE, READY),
        (READY, READY),
        (CARD, CARD),
        (GAME, GAME),
    ])
    def test_only_idle_advances(self, current, expected):
        assert state.after_source_connected(current) == expected

    def test_a_floppy_drive_alone_is_enough_to_leave_idle(self):
        """IDLE means "nothing can trigger a launch", not "no NFC reader". A
        Deck with only a floppy drive attached used to sit in IDLE forever."""
        assert state.after_source_connected(IDLE) == READY

    def test_connecting_a_source_never_disturbs_a_running_game(self):
        assert state.after_source_connected(GAME) == GAME


class TestSourceDisconnected:

    @pytest.mark.parametrize("current", ALL_STATES)
    def test_losing_the_last_source_is_idle_from_anywhere(self, current):
        assert state.after_source_disconnected(
            current, any_source_available=False, any_media_present=False
        ) == IDLE

    def test_losing_one_of_several_sources_is_not_idle(self):
        """Losing the NFC reader while a floppy drive is still connected is not
        idle."""
        assert state.after_source_disconnected(
            READY, any_source_available=True, any_media_present=False
        ) == READY

    def test_card_present_falls_back_when_its_medium_went_with_the_source(self):
        assert state.after_source_disconnected(
            CARD, any_source_available=True, any_media_present=False
        ) == READY

    def test_card_present_holds_when_another_source_still_has_media(self):
        assert state.after_source_disconnected(
            CARD, any_source_available=True, any_media_present=True
        ) == CARD


# ── Media presentation ────────────────────────────────────────────────────────

class TestMediaPresented:

    def test_presenting_media_when_idle_or_ready_is_card_present(self):
        for current in (READY, CARD):
            assert state.after_media_presented(current, game_running=False) == CARD

    def test_a_running_game_outranks_a_new_medium(self):
        """A disk going into the drive while a tag is already running something
        must not drop out of GAME_RUNNING — the removal handler tests for that
        state before it will close the game."""
        assert state.after_media_presented(GAME, game_running=True) == GAME


class TestUnusableMedia:
    """Blank, unreadable, or blocked by the allowlist."""

    def test_unusable_media_returns_to_ready(self):
        assert state.after_unusable_media(CARD, game_running=False) == READY

    def test_a_blank_disk_does_not_forget_that_a_game_is_running(self):
        """The bug this function exists for.

        This returned READY unconditionally, so inserting a blank disk while a
        game was running left the plugin in READY — and removing the tag that
        had actually launched the game then silently did nothing, because
        _handle_media_unload only acts in GAME_RUNNING. Auto-close stayed broken
        until the game exited by hand, with nothing in the log to say why.
        """
        assert state.after_unusable_media(READY, game_running=True) == GAME
        assert state.after_unusable_media(CARD, game_running=True) == GAME
        assert state.after_unusable_media(GAME, game_running=True) == GAME


class TestLaunchBlocked:

    def test_a_valid_uri_during_a_game_says_a_game_is_running(self):
        """Spec §8.1 — not a launch, but the state should say what is true."""
        assert state.after_launch_blocked(CARD) == GAME


# ── Removal and game exit ─────────────────────────────────────────────────────

class TestMediaRemoved:

    def test_last_medium_removed_returns_to_ready(self):
        assert state.after_media_removed(CARD, any_media_present=False) == READY

    def test_one_of_several_media_removed_stays_card_present(self):
        """A storage eject must not blank the NFC tag the panel is showing."""
        assert state.after_media_removed(CARD, any_media_present=True) == CARD

    def test_game_running_is_left_to_the_removal_handler(self):
        """Whether the game ends is decided from launch attribution, not from
        the fact that *a* medium went away, and the outcome comes back through
        set_running_game."""
        for present in (True, False):
            assert state.after_media_removed(GAME, any_media_present=present) == GAME

    def test_idle_is_left_alone(self):
        """No source is available to be holding anything."""
        assert state.after_media_removed(IDLE, any_media_present=False) == IDLE


class TestGameExited:

    def test_exit_with_media_still_present_is_card_present(self):
        """Spec §6.4. No auto-relaunch — that requires the medium to be
        physically re-presented — but the panel should still show the medium."""
        assert state.after_game_exited(GAME, any_media_present=True) == CARD

    def test_exit_with_nothing_presented_is_ready(self):
        assert state.after_game_exited(GAME, any_media_present=False) == READY

    @pytest.mark.parametrize("current", [IDLE, READY, CARD])
    def test_no_game_reported_when_none_was_running_changes_nothing(self, current):
        """The frontend reports game state on a timer, so "no game" arrives
        repeatedly while nothing is running."""
        assert state.after_game_exited(current, any_media_present=True) == current


# ── Invariants across the whole table ─────────────────────────────────────────

class TestInvariants:

    def test_every_rule_returns_a_real_state(self):
        """A rule returning None would silently skip the transition, since
        _set_state treats "same as current" as a no-op."""
        results = [
            state.after_source_connected(IDLE),
            state.after_source_disconnected(CARD, any_source_available=True, any_media_present=False),
            state.after_media_presented(READY, game_running=False),
            state.after_unusable_media(CARD, game_running=False),
            state.after_launch_blocked(CARD),
            state.after_media_removed(CARD, any_media_present=False),
            state.after_game_exited(GAME, any_media_present=False),
        ]
        assert all(isinstance(r, S) for r in results)

    def test_nothing_leaves_game_running_while_a_game_is_running(self):
        """The invariant the bug broke: only set_running_game(None) and the
        removal handler may end GAME_RUNNING. No amount of media activity on
        other sources may do it."""
        assert state.after_media_presented(GAME, game_running=True) == GAME
        assert state.after_unusable_media(GAME, game_running=True) == GAME
        assert state.after_launch_blocked(GAME) == GAME
        assert state.after_media_removed(GAME, any_media_present=True) == GAME
        assert state.after_source_connected(GAME) == GAME

    def test_rules_do_not_mutate_anything(self):
        """They take a state and return a state. There is one owner of the
        current state — Plugin._set_state — and it is not this module."""
        before = CARD
        state.after_media_presented(before, game_running=True)
        state.after_unusable_media(before, game_running=True)
        assert before == CARD
