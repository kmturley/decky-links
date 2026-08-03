"""The plugin's trust boundary, tested on its own.

This is the whole of what decides whether a URI arriving on a tag, a disk, a
QR code or an MQTT message may be acted on. It used to be a method on Plugin,
so exercising it meant constructing settings, a key manager and six media
sources to evaluate a string.
"""

import pytest

from decky_links import uri


class TestSteamUris:

    @pytest.mark.parametrize("value", [
        "steam://run/220",
        "steam://rungameid/220",
        "steam://rungameid/12086264277099347968",   # real shortcut gameID64
        "steam://run/1",
        "steam://run/4294967295",
    ])
    def test_launch_endpoints_allowed(self, value):
        assert uri.is_valid(value)

    @pytest.mark.parametrize("value", [
        # steam:// has many verbs; a card on a table may only launch.
        "steam://install/220",
        "steam://uninstall/220",
        "steam://open/console",
        "steam://flushconfig",
        "steam://nav/games",
    ])
    def test_other_steam_verbs_blocked(self, value):
        assert not uri.is_valid(value)

    @pytest.mark.parametrize("value", [
        "steam://run/",
        "steam://run/abc",
        "steam://run/22a0",
        "steam://run/12345678901",   # 11 digits, wider than a uint32
        "steam://run/-1",
        "steam://run/../../etc",
    ])
    def test_malformed_app_ids_blocked(self, value):
        assert not uri.is_valid(value)

    def test_trailing_path_after_app_id_allowed(self):
        assert uri.is_valid("steam://rungameid/220/extra")

    @pytest.mark.parametrize("appid", [2814052691, 3183009179, 2147483649])
    def test_non_steam_shortcut_gameid64_allowed(self, appid):
        """steam://run/ does not launch non-Steam shortcuts at all, so the
        panel builds a gameID64 instead. Checking it against the uint32
        app-id pattern rejected every shortcut, and pairing one failed inside
        start_pairing with only a log line to say why.

        Mirrors shortcutAppIdToGameId64 in src/lib/steamIds.ts — if that
        formula ever changes, this fails.
        """
        game_id = ((appid | 0x80000000) << 32) | 0x02000000
        assert uri.is_valid(f"steam://rungameid/{game_id}")

    def test_run_endpoint_never_takes_a_gameid64(self):
        """steam://run/ really does take an app id. Widening both endpoints
        would have been the lazy fix."""
        game_id = ((2814052691 | 0x80000000) << 32) | 0x02000000
        assert not uri.is_valid(f"steam://run/{game_id}")

    @pytest.mark.parametrize("value", [
        "12345678901234567890",   # 20 digits, but not a gameID64
        "99999999999999999999",
        "18446744073709551615",   # uint64 max: wrong low word
        "12086264277099347969",   # a real one with the low word disturbed
    ])
    def test_arbitrary_long_numbers_are_not_gameids(self, value):
        """The structure is checked, not the digit count — 'any 20 digits'
        would hand Steam numbers it would do something unpredictable with."""
        assert not uri.is_valid(f"steam://rungameid/{value}")

    def test_shortcut_flag_is_required(self):
        """Right type bits, but no shortcut flag in the high word."""
        without_flag = (2814052691 & 0x7FFFFFFF) << 32 | 0x02000000
        assert not uri.is_shortcut_gameid64(str(without_flag))

    def test_appid_zero_rejected(self):
        assert not uri.is_valid("steam://run/0")
        assert not uri.is_valid("steam://rungameid/0")


class TestHttps:

    @pytest.mark.parametrize("value", [
        "https://store.steampowered.com/app/220",
        "https://example.com",
        "https://8.8.8.8/",
        "https://sub.domain.example.org/a/b?c=d#e",
    ])
    def test_public_https_allowed(self, value):
        assert uri.is_valid(value)

    @pytest.mark.parametrize("scheme", [
        "http://example.com",        # plaintext
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://example.com",
        "steam://run/220 https://x.com",
    ])
    def test_other_schemes_blocked(self, scheme):
        assert not uri.is_valid(scheme)


class TestLocalHostsAreBlocked:
    """The check this replaced compared three exact strings against netloc,
    which carries the port — so even 'localhost:8080' went through."""

    @pytest.mark.parametrize("host", [
        "localhost", "foo.localhost",
        "127.0.0.1", "127.0.0.2", "127.255.255.254",
        "[::1]", "[::ffff:127.0.0.1]", "[::]",
        "0.0.0.0",
        "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.0.1",
        "169.254.169.254",
        "nas.local", "host.internal",
    ])
    def test_local_blocked(self, host):
        assert uri.is_local_host(host.strip("[]"))
        assert not uri.is_valid(f"https://{host}/x")

    @pytest.mark.parametrize("host", [
        "example.com", "8.8.8.8", "1.1.1.1", "23.55.1.1",
    ])
    def test_public_not_local(self, host):
        assert not uri.is_local_host(host)

    def test_port_does_not_defeat_the_check(self):
        assert not uri.is_valid("https://localhost:8080/admin")
        assert not uri.is_valid("https://127.0.0.1:1337/")

    def test_credentials_do_not_defeat_the_check(self):
        assert not uri.is_valid("https://user:pw@127.0.0.1/")

    def test_public_hostname_is_judged_on_the_literal(self):
        """Documented limit: this checks what is written, not what DNS would
        return. Stated so nobody reads it as rebinding protection."""
        assert uri.is_valid("https://totally-public.example.com/")


class TestBounds:

    def test_over_length_rejected(self):
        assert not uri.is_valid("https://example.com/" + "a" * uri.MAX_URI_LENGTH)

    def test_at_the_limit_accepted(self):
        base = "https://example.com/"
        assert uri.is_valid(base + "a" * (uri.MAX_URI_LENGTH - len(base)))

    @pytest.mark.parametrize("value", ["", None, 123, [], {}, b"steam://run/220"])
    def test_non_strings_and_empty_rejected(self, value):
        assert not uri.is_valid(value)


class TestReasons:
    """The reason is what tells a blocked card apart from a broken reader in
    the log, so it has to say which of the two happened."""

    def test_reason_absent_when_valid(self):
        assert uri.validate("steam://run/220") == (True, None)

    @pytest.mark.parametrize("value,fragment", [
        ("steam://run/abc", "app id"),
        ("https://127.0.0.1/", "local"),
        ("http://example.com", "allowlist"),
        ("", "empty"),
        ("https://example.com/" + "a" * 3000, "longer than"),
    ])
    def test_reason_identifies_the_problem(self, value, fragment):
        ok, reason = uri.validate(value)
        assert not ok
        assert fragment in reason


class TestAppIds:
    """Used beyond URI parsing: appid is interpolated into a filesystem path
    when rendering card art, in a process running as root."""

    @pytest.mark.parametrize("value", ["220", "1", 220, "4294967295"])
    def test_valid(self, value):
        assert uri.is_valid_appid(value)

    @pytest.mark.parametrize("value", [
        "", None, "../../etc/passwd", "220/../..", "abc", "220; rm -rf /", "-1",
    ])
    def test_invalid(self, value):
        assert not uri.is_valid_appid(value)


class TestSteamGamesAreUnaffected:
    """Pinned separately from the shortcut work.

    Widening rungameid to admit shortcut gameID64s must not change anything
    about ordinary Steam games, in either URI form. These values are what the
    old validator accepted, so a failure here means a regression, not a new
    rule.
    """

    @pytest.mark.parametrize("appid", [
        "1",
        "220",          # Half-Life 2
        "1174180",      # Red Dead Redemption 2
        "4294967295",   # uint32 max
    ])
    @pytest.mark.parametrize("prefix", ["steam://run/", "steam://rungameid/"])
    def test_ordinary_app_ids_still_launch_by_either_form(self, prefix, appid):
        assert uri.is_valid(f"{prefix}{appid}")

    @pytest.mark.parametrize("appid", ["4294967296", "12345678901", "abc", "-1", ""])
    @pytest.mark.parametrize("prefix", ["steam://run/", "steam://rungameid/"])
    def test_things_that_were_rejected_still_are(self, prefix, appid):
        assert not uri.is_valid(f"{prefix}{appid}")
