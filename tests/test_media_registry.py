"""Media presence and launch attribution.

These rules were three bare dicts on Plugin manipulated across five methods,
so testing them meant driving whole media events through a constructed plugin.
The attribution bug that motivated extracting this — the frontend re-reporting
the same running app wiping the origin, leaving auto-close refusing to quit
anything because every game had been "launched by None" — is the kind of thing
that is obvious here and was not obvious there.
"""

import pytest

from decky_links.media_registry import MediaRegistry


@pytest.fixture
def reg():
    return MediaRegistry()


class TestOneMediumPerSource:
    """A tag on the reader and a disk in the drive are simultaneously present;
    two tags on one reader are not."""

    def test_put_and_get(self, reg):
        reg.put("nfc:0", {"media_id": "AABB"})
        assert reg.get("nfc:0")["media_id"] == "AABB"

    def test_second_medium_replaces_the_first_on_that_source(self, reg):
        reg.put("nfc:0", {"media_id": "AABB"})
        reg.put("nfc:0", {"media_id": "CCDD"})
        assert reg.get("nfc:0")["media_id"] == "CCDD"
        assert len(reg.all()) == 1

    def test_sources_do_not_collide(self, reg):
        reg.put("nfc:0", {"media_id": "AABB", "source_type": "nfc"})
        reg.put("storage:udev", {"media_id": "/dev/sda", "source_type": "storage"})
        assert len(reg.all()) == 2

    def test_remove_reports_whether_there_was_media(self, reg):
        reg.put("nfc:0", {"media_id": "AABB"})
        assert reg.remove("nfc:0") is True
        assert reg.remove("nfc:0") is False

    def test_any_present(self, reg):
        assert reg.any_present() is False
        reg.put("nfc:0", {"media_id": "AABB"})
        assert reg.any_present() is True

    def test_first_of_type(self, reg):
        reg.put("storage:udev", {"media_id": "/dev/sda", "source_type": "storage"})
        reg.put("nfc:0", {"media_id": "AABB", "source_type": "nfc"})
        assert reg.first_of_type("nfc")["media_id"] == "AABB"
        assert reg.first_of_type("camera") is None


class TestLaunchAttribution:

    def test_a_claimed_launch_is_bound_on_confirmation(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        assert reg.launch_origin == {"source_id": "nfc:0", "media_id": "AABB"}

    def test_a_repeated_report_of_the_same_game_keeps_the_origin(self, reg):
        """The frontend reports the running game repeatedly. Unconditionally
        taking the now-empty pending claim wiped the attribution a second
        after setting it, and auto-close then refused to quit anything."""
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        reg.confirm_launch(220, 220)
        reg.confirm_launch(220, 220)
        assert reg.launch_origin == {"source_id": "nfc:0", "media_id": "AABB"}

    def test_a_hand_launched_game_is_attributed_to_nothing(self, reg):
        """Correct: no medium may quit a game the user started themselves."""
        reg.confirm_launch(220, None)
        assert reg.launch_origin is None

    def test_switching_to_another_game_with_no_claim_clears_attribution(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        reg.confirm_launch(440, 220)
        assert reg.launch_origin is None

    def test_a_new_claim_takes_over(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        reg.claim_launch("storage:udev", "/dev/sda")
        reg.confirm_launch(440, 220)
        assert reg.launch_origin["source_id"] == "storage:udev"

    def test_clear_launch_drops_both_claim_and_origin(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        reg.claim_launch("nfc:0", "CCDD")
        reg.clear_launch()
        assert reg.launch_origin is None
        reg.confirm_launch(660, 220)
        assert reg.launch_origin is None


class TestOnlyTheLaunchingMediumMayQuit:
    """Ejecting a floppy must not close a game started by tapping a card."""

    def test_the_launching_medium_matches(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        assert reg.launched_by("nfc:0", "AABB") is True

    @pytest.mark.parametrize("source_id,media_id", [
        ("storage:udev", "AABB"),   # right medium id, wrong source
        ("nfc:0", "CCDD"),          # right source, different medium
        ("camera:0", "/dev/sda"),   # neither
    ])
    def test_other_media_do_not_match(self, reg, source_id, media_id):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        assert reg.launched_by(source_id, media_id) is False

    def test_nothing_matches_when_unattributed(self, reg):
        assert reg.launched_by("nfc:0", "AABB") is False


class TestDisconnectReleasesTheClaim:
    """Hardware that has been unplugged cannot still be holding the medium
    that started the game; leaving the claim means nothing can ever quit it."""

    def test_claim_dropped_for_the_departing_source(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        assert reg.drop_origin_for_source("nfc:0") is True
        assert reg.launch_origin is None

    def test_other_sources_leave_the_claim_alone(self, reg):
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        assert reg.drop_origin_for_source("storage:udev") is False
        assert reg.launch_origin is not None


class TestReset:

    def test_reset_clears_everything(self, reg):
        reg.put("nfc:0", {"media_id": "AABB"})
        reg.claim_launch("nfc:0", "AABB")
        reg.confirm_launch(220, None)
        reg.reset()
        assert reg.all() == []
        assert reg.launch_origin is None
