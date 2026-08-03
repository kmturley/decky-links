"""The plugin state machine (Spec §5–§9): the states, and the rules for moving
between them.

The rules were ten inline conditionals spread across four handlers in
``main.py``. Each was locally reasonable and the whole was not checkable —
nothing showed you the machine, so answering "what happens if a game is running
and a blank disk goes in" meant tracing three methods by hand. That question
had a wrong answer, and it survived precisely because there was no place to
look at all the transitions side by side. See ``after_unusable_media``.

Every rule takes the current state plus the facts that bear on the decision and
returns the state to be in. They never mutate: ``Plugin._set_state`` is still
the only thing that changes state, so there is exactly one owner. That is why
this is a module of functions rather than a ``StateMachine`` object — two
objects each half-owning the state would be worse than the ten conditionals
were.

What deliberately stays in the handlers is the *ordering* of effects: pairing is
checked before URI inspection, and a launch is claimed before ``uri_detected``
is emitted, because the frontend races us. Ordering only exists in an effectful
sequence — there is nothing to extract, and moving the conditions away from the
comments explaining the ordering would make both harder to follow.
"""

from enum import Enum


class PluginState(Enum):
    """Plugin state machine (Spec §5).

    State transitions:
    - IDLE → READY: any source became available
    - READY → CARD_PRESENT: media presented on any source
    - CARD_PRESENT → READY: last medium removed (no game running)
    - CARD_PRESENT → GAME_RUNNING: Game launched (auto_launch enabled)
    - GAME_RUNNING → READY: Game exited (via set_running_game)
    - GAME_RUNNING → READY: launching medium removed (after card_removed_during_game)
    - Any state → IDLE: every source became unavailable

    Key invariants:
    - Media is tracked per source (MediaRegistry), not in one global slot
    - Only the medium that launched a game may quit it (MediaRegistry origin)
    - No auto-relaunch: requires the medium to be physically re-presented
    - Game state is authoritative from frontend (Router.MainRunningApp)

    The names are NFC-flavoured for historical reasons; CARD_PRESENT means
    "some medium is loaded on some source", which includes a disk in a drive.
    """
    IDLE         = "IDLE"          # No source available to trigger anything
    READY        = "READY"         # At least one source up, no media, no game
    CARD_PRESENT = "CARD_PRESENT"  # Media loaded, URI parsed, awaiting launch decision
    GAME_RUNNING = "GAME_RUNNING"  # A game is running; its launching medium is locked


# ── Transitions ───────────────────────────────────────────────────────────────

def after_source_connected(current: PluginState) -> PluginState:
    """A source came up.

    IDLE means "nothing can trigger a launch", not "no NFC reader" — a Deck
    with only a floppy drive attached used to sit in IDLE indefinitely. Every
    other state is already at least as advanced as READY, so leave it.
    """
    if current == PluginState.IDLE:
        return PluginState.READY
    return current


def after_source_disconnected(
    current: PluginState,
    *,
    any_source_available: bool,
    any_media_present: bool,
) -> PluginState:
    """A source went away.

    IDLE only when nothing at all is left to trigger with: losing the NFC
    reader while a floppy drive is still connected is not idle.
    """
    if not any_source_available:
        return PluginState.IDLE
    if current == PluginState.CARD_PRESENT and not any_media_present:
        return PluginState.READY
    return current


def after_media_presented(current: PluginState, *, game_running: bool) -> PluginState:
    """A medium was presented; its URI has not been inspected yet.

    A running game outranks this. Presenting a second medium — a disk going
    into the drive while a tag is already running something — must not drop out
    of GAME_RUNNING, because the removal handler tests for that state before it
    will close the game.
    """
    if game_running:
        return PluginState.GAME_RUNNING
    return PluginState.CARD_PRESENT


def after_unusable_media(current: PluginState, *, game_running: bool) -> PluginState:
    """The medium is blank, unreadable, or its URI failed the allowlist.

    ``game_running`` is the fix for a real bug. This used to return READY
    unconditionally, so inserting a blank disk while a game was running left the
    plugin in READY — and removing the tag that had actually launched the game
    then silently did nothing, because ``_handle_media_unload`` only acts in
    GAME_RUNNING. Auto-close stayed broken until the game exited by hand, with
    nothing in the log to say why.

    A medium we cannot read is not a reason to forget that a game is running.
    """
    if game_running:
        return PluginState.GAME_RUNNING
    return PluginState.READY


def after_launch_blocked(current: PluginState) -> PluginState:
    """A valid URI arrived while a game was already running (Spec §8.1).

    Not a launch, but the state should say what is true.
    """
    return PluginState.GAME_RUNNING


def after_media_removed(current: PluginState, *, any_media_present: bool) -> PluginState:
    """A medium was removed (Spec §6.3, §6.6).

    GAME_RUNNING is left alone: whether the game should end is the removal
    handler's decision, made from launch attribution, and it reports the outcome
    back through ``set_running_game``. IDLE is left alone because no source is
    available to be holding anything.
    """
    if current in (PluginState.GAME_RUNNING, PluginState.IDLE):
        return current
    return PluginState.CARD_PRESENT if any_media_present else PluginState.READY


def after_game_exited(current: PluginState, *, any_media_present: bool) -> PluginState:
    """The frontend reported no running game (Spec §6.4).

    Back to CARD_PRESENT when a medium is still presented somewhere, otherwise
    READY. Only from GAME_RUNNING — a report of "no game" when we never thought
    one was running changes nothing.
    """
    if current != PluginState.GAME_RUNNING:
        return current
    return PluginState.CARD_PRESENT if any_media_present else PluginState.READY
