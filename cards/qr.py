"""QR code and printable card generation.

Geometry is fixed rather than configurable, because the numbers were derived
from what a cheap webcam can actually resolve and a "make it smaller" option
would only ever produce codes that fail to scan.

**Module count.** Every realistic payload — ``steam://rungameid/220``, a
7-digit app id, and even a non-Steam shortcut's 20-digit game id — encodes to
29x29 modules at error-correction level Q. One fixed size covers a whole
library, which is what makes a single card template possible.

**Error correction Q** (25% recoverable) rather than the L default: these get
printed on home printers, stuck to boxes, scuffed, and read in the evening by a
webcam with a plastic lens.

**Physical size.** Modelling a 720p webcam at ~60 degrees horizontal field of
view, resolvable detail is roughly 110/d pixels per mm at d cm. A decoder needs
2 pixels per module at the absolute floor, 3 to be reliable, 4+ to survive poor
light and poor optics. At 35mm the data area gives 4.5 px/module at 30cm and
still 2.7 at 50cm, so 35mm is the target — deliberately generous, because the
failure mode of "slightly too small" is a code that only works in daylight.

**Integer module scaling.** The QR is rendered at a whole number of pixels per
module and never resampled. Scaling a small bitmap up softens module edges,
which spends the error-correction headroom on our own rendering rather than on
the scuffs it was chosen for.
"""

import os
from typing import Optional, Tuple

PRINT_DPI = 300

# 4x6 inches. Steam's vertical capsule is 600x900 — exactly 2:3 — so this is
# the print size that needs no cropping, and every photo lab and home printer
# already handles it.
CARD_WIDTH_MM = 101.6
CARD_HEIGHT_MM = 152.4

QR_EC_LEVEL = "Q"
# 14 px/module at 300 dpi puts the 29-module data area at 34.4mm, the nearest
# integer scaling to the 35mm target.
QR_MODULE_PX = 14
# The quiet zone is part of the symbol, not decoration: without 4 modules of
# clear margin a decoder cannot find the finder patterns against a busy
# background.
QR_QUIET_MODULES = 4

# Where Steam caches vertical capsule art. The layout changed between client
# versions — newer builds nest per-appid directories — and the @2x variants are
# 1200x1800, which is 300dpi at this card size where the plain ones are 150.
_ART_CANDIDATES = (
    "{home}/.local/share/Steam/appcache/librarycache/{appid}/library_600x900_2x.jpg",
    "{home}/.local/share/Steam/appcache/librarycache/{appid}/library_600x900.jpg",
    "{home}/.local/share/Steam/appcache/librarycache/{appid}_library_600x900_2x.jpg",
    "{home}/.local/share/Steam/appcache/librarycache/{appid}_library_600x900.jpg",
    "{home}/.steam/steam/appcache/librarycache/{appid}/library_600x900_2x.jpg",
    "{home}/.steam/steam/appcache/librarycache/{appid}_library_600x900.jpg",
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
)


class CardError(Exception):
    """Raised when a card cannot be produced. The message is shown to the user."""


def mm_to_px(mm: float, dpi: int = PRINT_DPI) -> int:
    """Millimetres to pixels at print resolution."""
    return int(round(mm / 25.4 * dpi))


def _load_font(size_px: int):
    """A bold font at the requested pixel size.

    Falls back to Pillow's built-in font, which since 10.1 can be sized — so a
    Deck with no DejaVu still produces a legible card rather than 11px text.
    """
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size_px)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size_px)
    except TypeError:      # Pillow < 10.1
        return ImageFont.load_default()


def qr_image(uri: str, module_px: int = QR_MODULE_PX, quiet_modules: int = QR_QUIET_MODULES):
    """Render ``uri`` as a QR code, quiet zone included.

    Returns a PIL image whose side is ``(modules + 2 * quiet) * module_px``
    pixels, black on white, with no resampling anywhere.
    """
    if not uri:
        raise CardError("No URI to encode")

    try:
        import zxingcpp
        from PIL import Image
    except ImportError as e:
        raise CardError(f"QR generation needs zxing-cpp and Pillow: {e}") from e

    barcode = zxingcpp.create_barcode(
        uri, zxingcpp.BarcodeFormat.QRCode, ec_level=QR_EC_LEVEL
    )
    # add_quiet_zones=False so the margin is ours: zxing's default zone is in
    # module units too, but doing it here keeps the returned size predictable
    # from the constants above, which is what the card layout is built on.
    raw = zxingcpp.write_barcode_to_image(barcode, scale=1, add_quiet_zones=False)
    # zxing-cpp's Python bindings have already changed shape once in this
    # project's lifetime (write_barcode deprecated, the returned Image losing
    # .width), so accept either a PIL image or anything array-like.
    modules = (raw if isinstance(raw, Image.Image) else Image.fromarray(raw)).convert("L")

    side = modules.size[0]
    scaled = modules.resize(
        (side * module_px, side * module_px), resample=Image.NEAREST
    )

    quiet_px = quiet_modules * module_px
    canvas = Image.new("L", (scaled.size[0] + 2 * quiet_px,) * 2, 255)
    canvas.paste(scaled, (quiet_px, quiet_px))
    return canvas


def find_art(appid: str, home: Optional[str] = None) -> Optional[str]:
    """Path to Steam's cached vertical capsule for ``appid``, if it has one."""
    home = home or os.path.expanduser("~")
    for template in _ART_CANDIDATES:
        path = template.format(home=home, appid=appid)
        if os.path.exists(path):
            return path
    return None


def render_front(appid: str, title: str, home: Optional[str] = None,
                 dpi: int = PRINT_DPI):
    """The art side: Steam's capsule at full bleed, or a titled fallback.

    No QR here. A 45mm code block is 44% of a 101.6mm-wide cover — it does not
    coexist with the artwork, it replaces it. Real game boxes put the barcode on
    the back for the same reason.
    """
    from PIL import Image, ImageDraw

    size = (mm_to_px(CARD_WIDTH_MM, dpi), mm_to_px(CARD_HEIGHT_MM, dpi))
    art_path = find_art(appid, home)

    if art_path:
        art = Image.open(art_path).convert("RGB")
        # The capsule is already 2:3, so this is a resize rather than a crop —
        # but cover-fit anyway in case Steam ever caches a different ratio.
        scale = max(size[0] / art.size[0], size[1] / art.size[1])
        resized = art.resize(
            (max(1, int(art.size[0] * scale)), max(1, int(art.size[1] * scale))),
            resample=Image.LANCZOS,
        )
        left = (resized.size[0] - size[0]) // 2
        top = (resized.size[1] - size[1]) // 2
        return resized.crop((left, top, left + size[0], top + size[1]))

    card = Image.new("RGB", size, (23, 26, 33))
    draw = ImageDraw.Draw(card)
    font = _load_font(mm_to_px(9, dpi))
    _draw_centred_wrapped(draw, title or f"App {appid}", font, card.size,
                          y=size[1] // 2, fill=(255, 255, 255), dpi=dpi)
    return card


def render_back(uri: str, title: str, appid: str, dpi: int = PRINT_DPI):
    """The code side: QR, title, app id.

    White background and generous margins because the quiet zone only works if
    nothing crowds it.
    """
    from PIL import Image, ImageDraw

    size = (mm_to_px(CARD_WIDTH_MM, dpi), mm_to_px(CARD_HEIGHT_MM, dpi))
    card = Image.new("RGB", size, (255, 255, 255))

    module_px = max(1, int(round(QR_MODULE_PX * dpi / PRINT_DPI)))
    qr = qr_image(uri, module_px=module_px).convert("RGB")
    if qr.size[0] > size[0]:
        raise CardError("QR code is wider than the card")

    qr_x = (size[0] - qr.size[0]) // 2
    qr_y = int(size[1] * 0.22)
    card.paste(qr, (qr_x, qr_y))

    draw = ImageDraw.Draw(card)
    title_font = _load_font(mm_to_px(6, dpi))
    _draw_centred_wrapped(
        draw, title or f"App {appid}", title_font, size,
        y=qr_y + qr.size[1] + mm_to_px(10, dpi), fill=(20, 20, 20), dpi=dpi,
    )

    footer_font = _load_font(mm_to_px(3.5, dpi))
    footer = f"{uri}"
    _draw_centred(draw, footer, footer_font, size,
                  y=size[1] - mm_to_px(12, dpi), fill=(120, 120, 120))
    return card


def _text_width(draw, text, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except AttributeError:                      # very old Pillow
        return font.getsize(text)[0]


def _draw_centred(draw, text, font, size, y, fill):
    draw.text(((size[0] - _text_width(draw, text, font)) // 2, y),
              text, font=font, fill=fill)


def _draw_centred_wrapped(draw, text, font, size, y, fill, dpi=PRINT_DPI):
    """Centre text, wrapping to the card width.

    Game titles run long — "Vampire Survivors: Ode to Castlevania" is not an
    edge case — and a single clipped line looks like a bug.
    """
    max_width = size[0] - 2 * mm_to_px(10, dpi)
    words = (text or "").split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    line_height = int(font.size * 1.25) if hasattr(font, "size") else mm_to_px(7, dpi)
    for i, line in enumerate(lines[:3]):
        _draw_centred(draw, line, font, size, y + i * line_height, fill)


def card_filename(appid: str, title: str, side: str) -> str:
    """A filename that sorts and reads well in a file manager."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (title or ""))
    safe = " ".join(safe.split())[:60].strip("_ ")
    # A title of only punctuation sanitises to underscores, which reads as a
    # broken filename rather than a game.
    if not any(c.isalnum() for c in safe):
        safe = f"App {appid}"
    return f"{safe} ({appid}) {side}.png"


def save_card(uri: str, title: str, appid: str, out_dir: str,
              home: Optional[str] = None, dpi: int = PRINT_DPI,
              owner: Optional[Tuple[int, int]] = None) -> dict:
    """Write both sides to ``out_dir`` and return their paths.

    ``owner`` is a (uid, gid) applied to everything written. The plugin runs as
    root so that it can mount disks, which means files land in the user's home
    owned by root and cannot be deleted from the desktop — passing the owner of
    the destination fixes that.
    """
    os.makedirs(out_dir, exist_ok=True)

    front = render_front(appid, title, home=home, dpi=dpi)
    back = render_back(uri, title, appid, dpi=dpi)

    paths = {}
    for side, image in (("front", front), ("back", back)):
        path = os.path.join(out_dir, card_filename(appid, title, side))
        image.save(path, "PNG", dpi=(dpi, dpi))
        paths[side] = path

    if owner:
        for path in (out_dir, *paths.values()):
            try:
                os.chown(path, owner[0], owner[1])
            except OSError:
                pass

    return paths
