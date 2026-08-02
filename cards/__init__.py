"""Printable game cards — the generated counterpart to physical trigger media.

A QR code is the only trigger medium that costs nothing to produce: an NFC tag
or a floppy has to be written to, one at a time, with the hardware present,
whereas a code is derived from the app id and can be printed as often as you
like. So this package is not about pairing — there is nothing to write — it is
about getting a scannable code off the Deck and onto something physical.
"""

from cards.qr import (  # noqa: F401
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    PRINT_DPI,
    QR_EC_LEVEL,
    QR_MODULE_PX,
    QR_QUIET_MODULES,
    CardError,
    card_filename,
    mm_to_px,
    qr_image,
    render_back,
    render_front,
    save_card,
)
