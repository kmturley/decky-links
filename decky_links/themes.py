"""Theme discovery: a theme is a folder, and mostly it is one file.

A theme used to be a TypeScript module compiled into the plugin's bundle,
which meant that making one required a checkout, a toolchain and a release.
The whole point of themes is that other people write them, so the format is
now data the plugin reads at runtime:

    themes/<id>/
        theme.html      the manifest, the styles and every screen
        sounds/*.flac   optional

One file plus a folder of audio. The HTML carries its own metadata in a JSON
block and each screen in a ``<template>``, so there is no second file to keep
in step with the first, and no build step between editing a theme and seeing
it. A theme with no sounds is one file.

Two directories are searched, in order:

1. ``~/Documents/decky-links/themes`` — where someone's own themes go. Same
   place the printable cards are written, because a user should have one
   folder for this plugin rather than a folder per feature.
2. The plugin's own ``assets/themes`` — what ships. Searched second so a user
   can shadow a built-in theme by using its id, which is the cheapest possible
   way to tweak one: copy it out, edit, keep the name.

Nothing here writes. Themes are read-only to the plugin, which is why none of
these functions is refused while the device is locked: reading a file the user
put there themselves gives away nothing that presenting the key would not.
"""

import base64
import json
import os
import re
from typing import Any, Dict, List, Optional

# An id is a directory name, and is used to build a path, so it is checked as
# strictly as any other untrusted string that becomes a path.
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Asset names are equally untrusted — a theme is a folder someone downloaded.
_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MANIFEST_RE = re.compile(
    r'<script[^>]+id=["\']decky-theme["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# A theme is markup, not media. The cap is generous for hand-written HTML and
# nowhere near enough to be a way of loading something else through this door.
MAX_HTML_BYTES = 512 * 1024

# One sound. Big enough for a few seconds of FLAC, small enough that a theme
# cannot quietly ship a film.
MAX_ASSET_BYTES = 4 * 1024 * 1024

THEME_FILE = "theme.html"


def user_theme_dir() -> str:
    return os.path.expanduser("~/Documents/decky-links/themes")


def _plugin_theme_dirs() -> List[str]:
    """Where the shipped themes are, in both layouts they can be in.

    The packaging CLI zips a fixed allowlist that excludes a top-level
    ``assets``, so the build vendors it into ``py_modules``. A development
    checkout has it at the top level. Check both rather than guessing which
    one this is.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        os.path.join(here, "assets", "themes"),
        os.path.join(here, "py_modules", "assets", "themes"),
    ]


def search_dirs() -> List[str]:
    return [user_theme_dir(), *_plugin_theme_dirs()]


def _theme_path(theme_id: str) -> Optional[str]:
    """The directory holding this theme, or None.

    First match wins, so a user theme shadows a shipped one with the same id.
    """
    if not _ID.match(theme_id or ""):
        return None
    for root in search_dirs():
        candidate = os.path.join(root, theme_id)
        if os.path.isfile(os.path.join(candidate, THEME_FILE)):
            return candidate
    return None


def _manifest(html: str) -> Dict[str, Any]:
    """The JSON block inside a theme's HTML.

    Inside the HTML rather than beside it so a theme is one file: two files
    that must agree is two files that can disagree, and the disagreement would
    be silent.
    """
    match = MANIFEST_RE.search(html)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _read_html(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_HTML_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def list_themes() -> List[Dict[str, Any]]:
    """Every theme found, with just enough of each to draw a picker.

    Deliberately does not return the markup. The panel lists themes far more
    often than it renders one, and a list that carried every theme's whole
    body would grow with the number installed for no benefit.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for root in search_dirs():
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            if entry in found or not _ID.match(entry):
                continue
            html = _read_html(os.path.join(root, entry, THEME_FILE))
            if html is None:
                continue
            manifest = _manifest(html)
            found[entry] = {
                "id": entry,
                "name": str(manifest.get("name") or entry),
                "blurb": str(manifest.get("blurb") or ""),
                # Which directory it came from, so the panel can say "yours"
                # and a user can tell a shadowing theme from the original.
                "builtin": root != user_theme_dir(),
            }
    return list(found.values())


def read_theme(theme_id: str) -> Optional[Dict[str, Any]]:
    """A theme's markup and manifest, ready to render."""
    path = _theme_path(theme_id)
    if path is None:
        return None
    html = _read_html(os.path.join(path, THEME_FILE))
    if html is None:
        return None
    return {"id": theme_id, "html": html, "manifest": _manifest(html)}


def read_asset(theme_id: str, name: str) -> Optional[str]:
    """One file from a theme's ``sounds`` folder, base64 encoded.

    Base64 over the existing RPC channel rather than a URL, because only the
    plugin's own ``dist`` is served over HTTP and a user's theme is not in it.
    The frontend turns this back into a blob and plays it.

    Both the id and the name are pattern-checked above, and the result is
    checked again with realpath: a symlink inside a downloaded theme folder is
    exactly the kind of thing that turns a read of "a sound" into a read of
    anything the plugin's user can open.
    """
    path = _theme_path(theme_id)
    if path is None or not _ASSET.match(name or ""):
        return None

    sounds = os.path.realpath(os.path.join(path, "sounds"))
    target = os.path.realpath(os.path.join(sounds, name))
    if target != sounds and not target.startswith(sounds + os.sep):
        return None
    try:
        if os.path.getsize(target) > MAX_ASSET_BYTES:
            return None
        with open(target, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None
