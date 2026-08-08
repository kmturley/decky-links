"""
test_master_key.py — the token that makes a medium an admin key.

Small module, and every test here is about one of two questions: is this a
master payload at all, and is it *the* master payload. Confusing the two is how
a stranger's tag would unlock the device, and how a malformed one would reach
the hash comparison.
"""
import pytest

from decky_links import master_key


class TestMinting:

    def test_token_is_32_hex_characters(self):
        token = master_key.mint_token()
        assert len(token) == 32
        int(token, 16)  # raises if it is not hex

    def test_tokens_differ_between_calls(self):
        """A key that repeats is a key every install shares."""
        assert master_key.mint_token() != master_key.mint_token()

    def test_uri_round_trips_through_parse(self):
        token = master_key.mint_token()
        assert master_key.parse_token(master_key.uri_for(token)) == token


class TestParsing:

    def test_recognises_a_master_payload(self):
        assert master_key.parse_token("decky-links://master/" + "a" * 32) == "a" * 32

    def test_uppercase_token_is_normalised(self):
        """Media round-trips can change case; the token is hex either way."""
        assert master_key.parse_token("decky-links://master/" + "A" * 32) == "a" * 32

    @pytest.mark.parametrize("uri", [
        "steam://rungameid/400",
        "https://example.com",
        "decky-links://something-else/" + "a" * 32,
        "decky-links://master/",
        "decky-links://master/" + "a" * 31,     # too short
        "decky-links://master/" + "a" * 33,     # too long
        "decky-links://master/" + "z" * 32,     # not hex
        "",
        None,
        1234,
    ])
    def test_rejects_everything_else(self, uri):
        assert master_key.parse_token(uri) is None

    def test_a_game_uri_is_never_a_master_payload(self):
        """The two must not be confusable: one launches, one unlocks."""
        assert master_key.parse_token("steam://run/400") is None


class TestMatching:

    def test_the_registered_token_matches(self):
        token = master_key.mint_token()
        assert master_key.matches(token, master_key.hash_token(token)) is True

    def test_another_token_does_not(self):
        stored = master_key.hash_token(master_key.mint_token())
        assert master_key.matches(master_key.mint_token(), stored) is False

    def test_hash_is_case_insensitive_on_the_stored_side(self):
        token = master_key.mint_token()
        assert master_key.matches(token, master_key.hash_token(token).upper()) is True

    def test_no_registered_key_never_matches(self):
        """Without this, the empty string hashes to a fixed value that a
        crafted payload could carry — every device with no key registered
        would accept the same one."""
        assert master_key.matches(master_key.mint_token(), "") is False

    def test_empty_token_never_matches(self):
        assert master_key.matches("", master_key.hash_token("")) is False
