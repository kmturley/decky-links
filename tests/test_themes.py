"""Theme discovery — a theme is a folder someone downloaded.

Which is the whole reason these tests exist. The plugin reads files it did not
write, from two directories, one of which is inside the user's Documents; the
ids and filenames become paths; and the format is hand-edited, so malformed is
the normal case rather than the exceptional one.
"""

import base64
import json
import os
import re

import pytest

from decky_links import themes


THEME = """<script type="application/json" id="decky-theme">
{"name": "Test Theme", "blurb": "A test", "sounds": {"scan": "tick.flac"}}
</script>
<style>.t { color: red }</style>
<template data-scene="ready"><div>Ready</div></template>
"""


@pytest.fixture
def theme_root(tmp_path, monkeypatch):
    """A user theme directory, standing in for ~/Documents/decky-links/themes."""
    root = tmp_path / "themes"
    root.mkdir()
    monkeypatch.setattr(themes, "user_theme_dir", lambda: str(root))
    monkeypatch.setattr(themes, "_plugin_theme_dirs", lambda: [])
    return root


def _write(root, name, html=THEME):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "theme.html").write_text(html)
    return d


class TestListing:

    def test_finds_a_theme_and_reads_its_manifest(self, theme_root):
        _write(theme_root, "retro")
        found = themes.list_themes()
        assert [t["id"] for t in found] == ["retro"]
        assert found[0]["name"] == "Test Theme"
        assert found[0]["blurb"] == "A test"

    def test_a_folder_without_theme_html_is_not_a_theme(self, theme_root):
        (theme_root / "notes").mkdir()
        (theme_root / "notes" / "readme.txt").write_text("hello")
        assert themes.list_themes() == []

    def test_a_missing_manifest_still_lists(self, theme_root):
        """A half-written theme should appear in the picker so its author can
        select it and see what is wrong, rather than vanish silently."""
        _write(theme_root, "wip", html="<template data-scene='ready'>x</template>")
        found = themes.list_themes()
        assert found[0]["id"] == "wip"
        assert found[0]["name"] == "wip"

    def test_a_broken_manifest_does_not_take_the_list_down(self, theme_root):
        _write(theme_root, "broken", html='<script type="application/json" id="decky-theme">{oh no</script>')
        _write(theme_root, "fine")
        assert {t["id"] for t in themes.list_themes()} == {"broken", "fine"}

    def test_a_name_that_is_not_an_id_is_skipped(self, theme_root):
        _write(theme_root, "Not An Id")
        assert themes.list_themes() == []

    def test_missing_directories_are_not_an_error(self, tmp_path, monkeypatch):
        """Nobody has a themes folder until they make one."""
        monkeypatch.setattr(themes, "user_theme_dir", lambda: str(tmp_path / "nope"))
        monkeypatch.setattr(themes, "_plugin_theme_dirs", lambda: [])
        assert themes.list_themes() == []


class TestUserThemesShadowShippedOnes:

    def test_the_user_copy_wins(self, tmp_path, monkeypatch):
        """The cheapest way to tweak a shipped theme: copy it out, edit it,
        keep the name."""
        user = tmp_path / "user"
        shipped = tmp_path / "shipped"
        for root, name in ((user, "Mine"), (shipped, "Theirs")):
            d = root / "dos"
            d.mkdir(parents=True)
            (d / "theme.html").write_text(
                '<script type="application/json" id="decky-theme">'
                + json.dumps({"name": name}) + "</script>"
            )
        monkeypatch.setattr(themes, "user_theme_dir", lambda: str(user))
        monkeypatch.setattr(themes, "_plugin_theme_dirs", lambda: [str(shipped)])

        listed = themes.list_themes()
        assert len(listed) == 1
        assert listed[0]["name"] == "Mine"
        assert listed[0]["builtin"] is False
        assert themes.read_theme("dos")["manifest"]["name"] == "Mine"


class TestReading:

    def test_returns_the_markup(self, theme_root):
        _write(theme_root, "retro")
        assert "<template data-scene=\"ready\">" in themes.read_theme("retro")["html"]

    def test_an_unknown_theme_is_none(self, theme_root):
        assert themes.read_theme("nope") is None

    def test_an_id_that_is_a_path_is_refused(self, theme_root):
        """The id becomes a directory name, so it is checked as strictly as
        any other untrusted string that becomes a path."""
        for bad in ("../../etc", "..", "/etc", "a/b", ""):
            assert themes.read_theme(bad) is None

    def test_a_huge_file_is_refused(self, theme_root):
        """A theme is markup. Anything this size is something else."""
        _write(theme_root, "big", html="x" * (themes.MAX_HTML_BYTES + 1))
        assert themes.read_theme("big") is None
        assert themes.list_themes() == []


class TestAssets:

    def _with_sound(self, root, data=b"\x00\x01\x02"):
        d = _write(root, "retro")
        (d / "sounds").mkdir()
        (d / "sounds" / "tick.flac").write_bytes(data)
        return d

    def test_returns_base64(self, theme_root):
        self._with_sound(theme_root)
        assert base64.b64decode(themes.read_asset("retro", "tick.flac")) == b"\x00\x01\x02"

    def test_a_traversing_name_is_refused(self, theme_root):
        self._with_sound(theme_root)
        for bad in ("../theme.html", "../../etc/passwd", "/etc/passwd", ""):
            assert themes.read_asset("retro", bad) is None

    def test_a_symlink_out_of_the_folder_is_refused(self, theme_root):
        """A theme is a folder someone downloaded, and a symlink inside it is
        exactly how a read of "a sound" becomes a read of anything else."""
        d = self._with_sound(theme_root)
        secret = theme_root.parent / "secret.txt"
        secret.write_text("password")
        os.symlink(secret, d / "sounds" / "escape.flac")
        assert themes.read_asset("retro", "escape.flac") is None

    def test_a_missing_asset_is_none(self, theme_root):
        self._with_sound(theme_root)
        assert themes.read_asset("retro", "nothing.flac") is None

    def test_an_oversized_asset_is_refused(self, theme_root):
        self._with_sound(theme_root, data=b"\x00" * (themes.MAX_ASSET_BYTES + 1))
        assert themes.read_asset("retro", "tick.flac") is None


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DIR = os.path.join(REPO, "assets", "themes")

# Every scene a theme may draw. IN_GAME is deliberately absent: gamescope owns
# the screen once a game paints, so the layer never renders it. Mirrors the
# Scene enum in src/lib/presentation.ts.
SCENES = ("ready", "ambient", "reading", "launching", "error", "locked")

# Everything VisualsLayer substitutes. A theme asking for {clock} instead of
# {time} renders the word in braces on screen, which is the sort of typo only a
# test catches — nothing errors.
PLACEHOLDERS = {"title", "drive", "time"}

SHIPPED = sorted(
    entry for entry in os.listdir(SHIPPED_DIR)
    if os.path.isfile(os.path.join(SHIPPED_DIR, entry, themes.THEME_FILE))
)


def _shipped(theme_id):
    with open(os.path.join(SHIPPED_DIR, theme_id, themes.THEME_FILE)) as f:
        return f.read()


@pytest.mark.parametrize("theme_id", SHIPPED)
class TestTheShippedThemes:
    """The themes that ship are data now, so they can be wrong in ways a
    compiled module could not be — a misspelled sound key, a scene name that
    matches nothing, a file that was never added. All of those fail silently at
    runtime, so they are checked here, against the real files, for every theme
    in assets/themes rather than a named one: a theme added later inherits
    every check below without anybody remembering to ask for it.
    """

    def test_it_is_discoverable(self, theme_id):
        assert themes._ID.match(theme_id), "folder name is not a valid theme id"
        assert themes.read_theme(theme_id) is not None

    def test_its_manifest_parses_and_names_itself(self, theme_id):
        manifest = themes._manifest(_shipped(theme_id))
        assert manifest.get("name"), "a theme with no name is a picker entry reading 'dos'"
        assert manifest.get("blurb")

    def test_every_sound_it_names_exists(self, theme_id):
        """A theme naming a file it does not ship is a silent event — the one
        failure that looks exactly like working."""
        sounds = os.path.join(SHIPPED_DIR, theme_id, "sounds")
        manifest = themes._manifest(_shipped(theme_id))
        named = set(manifest.get("sounds", {}).values())
        for scene in manifest.get("scenes", {}).values():
            named.update(v for k, v in scene.items() if k.endswith("Sound"))
        assert named, "a theme with no sound at all is probably a mistake"
        for name in named:
            assert os.path.isfile(os.path.join(sounds, name)), name

    def test_it_only_overrides_sounds_the_plugin_asks_for(self, theme_id):
        """The keys under "sounds" are the plugin's event names, not the
        theme's own. One that is not on the list is never looked up, so the
        theme quietly keeps the default sound."""
        import main

        manifest = themes._manifest(_shipped(theme_id))
        assert set(manifest.get("sounds", {})) <= set(main.ALLOWED_SOUNDS)

    def test_its_scene_settings_name_real_scenes(self, theme_id):
        manifest = themes._manifest(_shipped(theme_id))
        assert set(manifest.get("scenes", {})) <= set(SCENES)

    def test_it_draws_every_scene(self, theme_id):
        """Not required of a theme — a missing scene falls back to standby —
        but the ones that ship should show what a complete theme looks like."""
        html = _shipped(theme_id)
        for scene in SCENES:
            assert f'data-scene="{scene}"' in html, scene

    def test_it_only_asks_for_placeholders_that_exist(self, theme_id):
        used = set(re.findall(r"\{(\w+)\}", _shipped(theme_id)))
        assert used <= PLACEHOLDERS, f"unknown placeholder(s): {used - PLACEHOLDERS}"


def test_more_than_one_theme_ships():
    """The format is for other people to write in, and one example is not a
    format — a second theme is what proves nothing about the first was
    special-cased."""
    assert len(SHIPPED) >= 2
