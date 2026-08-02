"""
test_cards.py — printable card generation.

zxing-cpp has no wheel for the local dev Python, so the encoder is stubbed and
these tests cover the parts that are ours: geometry, layout, art discovery,
filenames and ownership. The encoder itself is verified for real against
linux/amd64 python:3.11 — the runtime the Deck actually uses — where a rendered
card decodes back to its URI down to 1.9 px per module.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

from PIL import Image

from cards.qr import (
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    PRINT_DPI,
    QR_MODULE_PX,
    QR_QUIET_MODULES,
    CardError,
    card_filename,
    find_art,
    mm_to_px,
    qr_image,
    render_back,
    render_front,
    save_card,
)

# A real QR of "steam://rungameid/220" at EC Q is 29x29; every realistic
# payload lands on the same version, which is what lets one template serve a
# whole library.
MODULES = 29


def _stub_zxing(monkeypatch, modules=MODULES):
    """A zxingcpp whose encoder returns a checkerboard of the right size."""
    stub = MagicMock()
    stub.BarcodeFormat.QRCode = "QRCode"

    image = Image.new("L", (modules, modules))
    image.putdata([0 if (x + y) % 2 else 255
                   for y in range(modules) for x in range(modules)])
    stub.write_barcode_to_image.return_value = image
    monkeypatch.setitem(sys.modules, "zxingcpp", stub)
    return stub


class TestGeometry:

    def test_mm_to_px_at_print_resolution(self):
        assert mm_to_px(25.4) == PRINT_DPI
        assert mm_to_px(CARD_WIDTH_MM) == 1200
        assert mm_to_px(CARD_HEIGHT_MM) == 1800

    def test_card_is_exactly_four_by_six_inches(self):
        """Steam's vertical capsule is 600x900 — exactly 2:3 — so a 4x6 print
        needs no cropping and any photo printer handles it."""
        assert mm_to_px(CARD_WIDTH_MM) / mm_to_px(CARD_HEIGHT_MM) == pytest.approx(2 / 3)

    def test_qr_data_area_is_close_to_35mm(self):
        """35mm is the size the sizing maths asked for: 4.4 px/module at 30cm
        on a 720p webcam, still 2.6 at 50cm."""
        data_mm = MODULES * QR_MODULE_PX / PRINT_DPI * 25.4
        assert 34.0 <= data_mm <= 35.5


class TestQrImage:

    def test_includes_the_quiet_zone(self, monkeypatch):
        """Four clear modules each side are part of the symbol, not decoration:
        without them a decoder cannot find the finder patterns."""
        _stub_zxing(monkeypatch)
        img = qr_image("steam://rungameid/220")
        expected = (MODULES + 2 * QR_QUIET_MODULES) * QR_MODULE_PX
        assert img.size == (expected, expected)

    def test_quiet_zone_is_white(self, monkeypatch):
        _stub_zxing(monkeypatch)
        img = qr_image("steam://rungameid/220")
        assert img.getpixel((0, 0)) == 255
        assert img.getpixel((img.size[0] - 1, img.size[1] - 1)) == 255

    def test_modules_are_scaled_by_whole_pixels(self, monkeypatch):
        """Resampling softens module edges, which spends the error-correction
        headroom on our own rendering instead of on the scuffs it is for."""
        _stub_zxing(monkeypatch)
        img = qr_image("steam://rungameid/220", module_px=10, quiet_modules=0)
        # Every pixel in a module must equal the module's value — no blending.
        for mx, my in ((0, 0), (1, 0), (5, 7)):
            block = [
                img.getpixel((mx * 10 + dx, my * 10 + dy))
                for dx in range(10) for dy in range(10)
            ]
            assert len(set(block)) == 1

    def test_empty_uri_is_refused(self):
        with pytest.raises(CardError):
            qr_image("")

    def test_missing_encoder_is_reported_clearly(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "zxingcpp", None)
        with pytest.raises(CardError) as excinfo:
            qr_image("steam://rungameid/220")
        assert "zxing-cpp" in str(excinfo.value)


class TestArtDiscovery:

    def test_prefers_the_2x_capsule(self, tmp_path):
        """The plain capsule is 600x900, which is only 150 dpi at 4x6. The @2x
        variant is 1200x1800 — exactly 300."""
        cache = tmp_path / ".local/share/Steam/appcache/librarycache/220"
        cache.mkdir(parents=True)
        (cache / "library_600x900.jpg").write_bytes(b"x")
        (cache / "library_600x900_2x.jpg").write_bytes(b"x")
        assert find_art("220", home=str(tmp_path)).endswith("library_600x900_2x.jpg")

    def test_finds_the_flat_layout_too(self, tmp_path):
        """Older Steam clients did not nest per-appid directories."""
        cache = tmp_path / ".local/share/Steam/appcache/librarycache"
        cache.mkdir(parents=True)
        (cache / "220_library_600x900.jpg").write_bytes(b"x")
        assert find_art("220", home=str(tmp_path)) is not None

    def test_returns_none_when_absent(self, tmp_path):
        assert find_art("220", home=str(tmp_path)) is None


class TestRendering:

    def test_back_is_card_sized_and_contains_the_code(self, monkeypatch):
        _stub_zxing(monkeypatch)
        back = render_back("steam://rungameid/220", "Half-Life 2", "220")
        assert back.size == (1200, 1800)
        assert back.getpixel((5, 5)) == (255, 255, 255)     # white margin

    def test_front_falls_back_when_there_is_no_art(self, tmp_path):
        """An uninstalled or non-Steam game still gets a usable card."""
        front = render_front("220", "Half-Life 2", home=str(tmp_path))
        assert front.size == (1200, 1800)

    def test_front_uses_the_art_when_present(self, tmp_path):
        cache = tmp_path / ".local/share/Steam/appcache/librarycache/220"
        cache.mkdir(parents=True)
        Image.new("RGB", (600, 900), (200, 30, 30)).save(cache / "library_600x900.jpg")
        front = render_front("220", "Half-Life 2", home=str(tmp_path))
        assert front.size == (1200, 1800)
        r, g, b = front.getpixel((600, 900))
        assert r > 150 and g < 80

    def test_a_long_title_wraps_rather_than_clipping(self, monkeypatch):
        _stub_zxing(monkeypatch)
        long_title = "Vampire Survivors: Ode to Castlevania Deluxe Edition"
        back = render_back("steam://rungameid/220", long_title, "220")
        assert back.size == (1200, 1800)


class TestSaving:

    def test_writes_both_sides(self, monkeypatch, tmp_path):
        _stub_zxing(monkeypatch)
        paths = save_card("steam://rungameid/220", "Half-Life 2", "220",
                          str(tmp_path / "out"), home=str(tmp_path))
        assert set(paths) == {"front", "back"}
        for path in paths.values():
            assert os.path.getsize(path) > 0
            assert Image.open(path).size == (1200, 1800)

    def test_filenames_are_readable_and_safe(self):
        name = card_filename("220", "Half-Life 2: Episode One / Two", "back")
        assert name.startswith("Half-Life 2")
        assert "/" not in name
        assert name.endswith("(220) back.png")

    def test_a_title_of_only_punctuation_still_gets_a_filename(self):
        assert card_filename("220", "///", "front") == "App 220 (220) front.png"

    def test_ownership_is_applied(self, monkeypatch, tmp_path):
        """The plugin runs as root so it can mount disks, so files land in the
        user's home owned by root and cannot be deleted from the desktop."""
        _stub_zxing(monkeypatch)
        chowned = []
        monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.append((p, u, g)))
        save_card("steam://rungameid/220", "HL2", "220", str(tmp_path / "out"),
                  home=str(tmp_path), owner=(1000, 1000))
        assert len(chowned) == 3                     # the directory and both files
        assert all(c[1:] == (1000, 1000) for c in chowned)

    def test_ownership_failure_does_not_lose_the_cards(self, monkeypatch, tmp_path):
        """Cards the user can see but not delete beat no cards at all."""
        _stub_zxing(monkeypatch)
        monkeypatch.setattr(os, "chown", MagicMock(side_effect=PermissionError))
        paths = save_card("steam://rungameid/220", "HL2", "220",
                          str(tmp_path / "out"), home=str(tmp_path), owner=(0, 0))
        assert os.path.exists(paths["back"])
