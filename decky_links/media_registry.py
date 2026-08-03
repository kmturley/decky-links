"""What media is presented where, and which medium started the running game.

Three bare attributes on Plugin held this: ``_active_media``,
``_launch_origin`` and ``_pending_launch_origin``. The rules connecting them
are subtle and were expressed as dict operations spread across five methods —
which is how the attribution bug got in, where the frontend re-reporting the
same running app wiped the origin and left auto-close refusing to quit
anything because every game had been "launched by None".

The invariants, in one place:

- One medium per source. A tag on the reader and a disk in the drive are
  simultaneously present; two tags on one reader are not.
- Only the medium that launched a game may quit it. Ejecting a floppy must not
  close a game started by tapping a card.
- A launch is claimed *before* it happens and confirmed after, because the
  frontend reports the running game as soon as RunGame returns — possibly
  before the backend would otherwise have recorded who caused it.
- Attribution survives the frontend re-reporting the same game, and is
  dropped when a genuinely different game appears with nobody claiming it.
"""

from typing import Any, Dict, List, Optional


class MediaRegistry:
    """The authoritative record of presented media and launch attribution."""

    def __init__(self):
        # source_id -> medium. One entry per source.
        self._media: Dict[str, Dict[str, Any]] = {}
        self._launch_origin: Optional[Dict[str, str]] = None
        self._pending_launch_origin: Optional[Dict[str, str]] = None

    # ── Media ──────────────────────────────────────────────────────────

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        return self._media.get(source_id)

    def all(self) -> List[Dict[str, Any]]:
        return list(self._media.values())

    def any_present(self) -> bool:
        return bool(self._media)

    def put(self, source_id: str, medium: Dict[str, Any]) -> None:
        self._media[source_id] = medium

    def remove(self, source_id: str) -> bool:
        """Drop the medium on this source. True if there was one."""
        return self._media.pop(source_id, None) is not None

    def first_of_type(self, source_type: str) -> Optional[Dict[str, Any]]:
        """Any medium from a given kind of source.

        Used by the pairing sync, which knows it wrote to *an* NFC tag but not
        which source id reported it.
        """
        return next(
            (m for m in self._media.values() if m.get("source_type") == source_type),
            None,
        )

    # ── Launch attribution ─────────────────────────────────────────────

    @property
    def launch_origin(self) -> Optional[Dict[str, str]]:
        return self._launch_origin

    def claim_launch(self, source_id: str, media_id: str) -> None:
        """Record that this medium is about to cause a launch.

        Claimed before the frontend is told to launch, not after. The frontend
        calls set_running_game as soon as RunGame returns, and that call can
        arrive first — it would then find no pending origin, attribute the
        game to nothing, and removing the medium would silently fail to quit
        it. Ordering is the whole point.
        """
        self._pending_launch_origin = {"source_id": source_id, "media_id": media_id}

    def confirm_launch(self, appid, previous_appid) -> None:
        """Bind a running game to whichever medium claimed it.

        A launch the user started by hand has no pending claim and is
        attributed to nothing, which correctly means no medium can quit it.

        Only a *new* claim, or a genuinely different game, may change the
        attribution. The frontend reports the running game repeatedly, and
        unconditionally taking the (now empty) pending claim wiped the
        attribution immediately after setting it.
        """
        if self._pending_launch_origin is not None:
            self._launch_origin = self._pending_launch_origin
            self._pending_launch_origin = None
        elif appid != previous_appid:
            self._launch_origin = None

    def clear_launch(self) -> None:
        self._launch_origin = None
        self._pending_launch_origin = None

    def launched_by(self, source_id: str, media_id: str) -> bool:
        """Whether this exact medium started the running game."""
        origin = self._launch_origin
        return (
            origin is not None
            and origin.get("source_id") == source_id
            and origin.get("media_id") == media_id
        )

    def drop_origin_for_source(self, source_id: str) -> bool:
        """Release a claim held by a source that has gone away.

        Hardware that has been unplugged cannot still be holding the medium
        that started the game, and leaving the claim in place means nothing
        can ever quit it.
        """
        if self._launch_origin and self._launch_origin.get("source_id") == source_id:
            self._launch_origin = None
            return True
        return False

    def reset(self) -> None:
        self._media.clear()
        self.clear_launch()
