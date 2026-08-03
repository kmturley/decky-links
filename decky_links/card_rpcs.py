"""Printable-card RPCs, as plain functions.

Generating a QR and rendering a two-sided card share nothing with the state
machine, the media sources or the NFC reader — they need a URI and a place to
write. They sat on Plugin only because every RPC did.

Kept as functions taking what they need rather than a class: there is no state
here, and the ``decky`` module is passed in because it exists only inside the
plugin loader's process.
"""

import asyncio
import base64
import io
import os
import traceback

from decky_links import uri as uri_rules


def output_dir(decky) -> str:
    """Where saved cards go.

    Under the user's home rather than the plugin's settings directory: these
    exist to be copied off the Deck and printed, so they have to be somewhere
    a person can find in a file manager.
    """
    return os.path.join(user_home(decky), "Documents", "decky-links")


def user_home(decky) -> str:
    return getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~")


def owner(decky):
    """``(uid, gid)`` of the user's home, or None.

    The plugin runs as root so it can mount disks, so anything it writes into
    the user's home is root-owned and cannot be deleted from the desktop. Only
    relevant when we actually are root.
    """
    if os.geteuid() != 0:
        return None
    try:
        info = os.stat(user_home(decky))
        return (info.st_uid, info.st_gid)
    except OSError:
        return None


async def qr_preview(decky, uri: str, module_px: int = 6):
    """A QR for ``uri`` as a PNG data URI, for display in the panel.

    Rendered at a small integer module size rather than by scaling a print
    image down — the same no-resampling rule that applies to print applies on
    screen, and a soft QR photographs badly.

    Available whether or not the camera trigger is switched on: printing codes
    before owning a webcam is a reasonable thing to do.
    """
    ok, reason = uri_rules.validate(uri)
    if not ok:
        decky.logger.warning(f"QR preview refused: {reason}")
        return {"ok": False, "error": "Invalid URI"}

    try:
        from cards import qr_image
        image = qr_image(uri, module_px=max(1, min(int(module_px), 20)))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {
            "ok": True,
            "data_uri": f"data:image/png;base64,{encoded}",
            "size": image.size[0],
        }
    except Exception as e:
        decky.logger.error(f"QR preview failed: {e}")
        return {"ok": False, "error": str(e)}


async def save_card(decky, print_dpi, uri: str, title: str = "", appid: str = ""):
    """Write a two-sided printable card and return where it went.

    Front is Steam's vertical capsule, back carries the QR — a 45mm code block
    is 44% of a 101.6mm cover, so it cannot share a side with the art.
    """
    ok, reason = uri_rules.validate(uri)
    if not ok:
        decky.logger.warning(f"save_game_card refused: {reason}")
        return {"ok": False, "error": "Invalid URI"}

    # appid is interpolated into a filesystem path by cards.find_art, and this
    # process is root. `uri` is validated above and `title` is sanitised for
    # the filename, but appid reached the path builder untouched — a traversal
    # value would have read an arbitrary file and rendered it into a PNG the
    # caller gets back. Empty stays allowed: it means "no art".
    appid = str(appid or "")
    if appid and not uri_rules.is_valid_appid(appid):
        decky.logger.warning(f"save_game_card: rejected app id {appid!r}")
        return {"ok": False, "error": "Invalid app id"}

    try:
        from cards import save_card as render_card
        out_dir = output_dir(decky)
        paths = await asyncio.to_thread(
            render_card, uri, title, appid, out_dir, user_home(decky),
            print_dpi, owner(decky),
        )
        decky.logger.info(f"Saved card for {appid or uri} to {out_dir}")
        return {"ok": True, "dir": out_dir, "paths": paths}
    except Exception as e:
        decky.logger.error(f"Saving card failed: {e}")
        decky.logger.error(traceback.format_exc())
        return {"ok": False, "error": str(e)}
