"""Tag identification from ISO 14443 activation data.

Classifying a tag by *trying* to authenticate it — the approach this plugin
used to take — is destructive.  A failed MIFARE authentication makes the
PN532 drop the target, so every probe after the first one talks to a card
that is no longer selected.  That is why only the first key in a key list
could ever succeed, and why an NTAG probed for MIFARE Classic came back
looking like a DESFire.

Every ISO 14443-3 Type A tag announces what it is during anticollision, for
free, before any command is sent to it: ATQA (SENS_RES) and SAK (SEL_RES).
This module turns those two values into a tag family.  It touches no
hardware and has no imports beyond the standard library, so it is directly
testable.

References: NXP AN10833 (MIFARE type identification), NXP UM0701-02 (PN532
user manual), NXP NTAG21x and MF0ULx1 datasheets.
"""

from typing import Any, Dict, Optional

# ── Protocols a reader can report ───────────────────────────────────────
PROTO_106A = "106A"      # ISO 14443-3 Type A (MIFARE, NTAG, DESFire, HCE)
PROTO_106B = "106B"      # ISO 14443-3 Type B
PROTO_212F = "212F"      # FeliCa 212 kbps
PROTO_424F = "424F"      # FeliCa 424 kbps
PROTO_JEWEL = "jewel"    # Innovision Topaz / Jewel

# ── Tag family names, as reported to the UI ─────────────────────────────
TYPE_UNKNOWN = "unknown"
TYPE_NTAG = "ntag21x"
TYPE_ULTRALIGHT = "ultralight"
TYPE_CLASSIC = "mifare-classic"
TYPE_CLASSIC_MINI = "mifare-mini"
TYPE_PLUS = "mifare-plus"
TYPE_DESFIRE = "desfire"
TYPE_ISO14443_4 = "iso14443-4"
TYPE_ISO14443B = "iso14443b"
TYPE_FELICA = "felica"
TYPE_JEWEL = "jewel"

# Families this plugin can read and write NDEF on.
NDEF_CAPABLE = frozenset({TYPE_NTAG, TYPE_ULTRALIGHT, TYPE_CLASSIC, TYPE_CLASSIC_MINI})

# Families that use the NTAG/Ultralight 4-byte page protocol rather than the
# MIFARE Classic 16-byte block protocol.
PAGE_ADDRESSED = frozenset({TYPE_NTAG, TYPE_ULTRALIGHT})


# ── SAK (SEL_RES) table ─────────────────────────────────────────────────
# Exact matches take priority; anything unlisted falls through to the bitwise
# rules in classify_sak, which are defined by ISO 14443-3 itself and so stay
# correct for parts that did not exist when this table was written.
_SAK_TABLE = {
    0x00: (TYPE_ULTRALIGHT, "MIFARE Ultralight or NTAG", 0),
    0x01: (TYPE_CLASSIC, "MIFARE TNP3xxx", 1024),
    0x08: (TYPE_CLASSIC, "MIFARE Classic 1K", 1024),
    0x09: (TYPE_CLASSIC_MINI, "MIFARE Mini 0.3K", 320),
    0x10: (TYPE_PLUS, "MIFARE Plus 2K (SL2)", 2048),
    0x11: (TYPE_PLUS, "MIFARE Plus 4K (SL2)", 4096),
    0x18: (TYPE_CLASSIC, "MIFARE Classic 4K", 4096),
    0x20: (TYPE_ISO14443_4, "ISO 14443-4 (DESFire, MIFARE Plus SL3, or phone)", 0),
    0x28: (TYPE_CLASSIC, "SmartMX with MIFARE Classic 1K emulation", 1024),
    0x38: (TYPE_CLASSIC, "SmartMX with MIFARE Classic 4K emulation", 4096),
    0x88: (TYPE_CLASSIC, "Infineon MIFARE Classic 1K", 1024),
    0x98: (TYPE_CLASSIC, "Gemplus MPCOS", 4096),
    0xB8: (TYPE_CLASSIC, "Gemplus MPCOS", 4096),
}

# SAK bit 6 (0x40) — ISO 18092 (NFCIP-1 / peer-to-peer) compliant.
_SAK_BIT_NFCIP1 = 0x40
# SAK bit 5 (0x20) — ISO 14443-4 compliant (supports APDUs / RATS).
_SAK_BIT_ISO14443_4 = 0x20
# SAK bit 3 (0x08) — UID not complete, another cascade level follows.  A
# reader should never hand us one of these; if it does, the UID is a fragment.
_SAK_BIT_CASCADE = 0x04


def classify_sak(sak: int, atqa: Optional[int] = None) -> Dict[str, Any]:
    """Map a SAK (and optionally ATQA) onto a tag family.

    Returns a dict with ``type``, ``label`` and ``capacity_bytes``.  The
    capacity is the tag's *total* memory as advertised by its family, not the
    usable NDEF area — callers that need the latter read the capability
    container or GET_VERSION.
    """
    known = _SAK_TABLE.get(sak)
    if known:
        tag_type, label, capacity = known
        # ATQA 0x0344 with SAK 0x20 is specifically a DESFire; the generic
        # 14443-4 label is true but unhelpful when we can do better.
        if tag_type == TYPE_ISO14443_4 and atqa == 0x0344:
            return {"type": TYPE_DESFIRE, "label": "MIFARE DESFire", "capacity_bytes": 0}
        return {"type": tag_type, "label": label, "capacity_bytes": capacity}

    # Unlisted SAK: fall back to what ISO 14443-3 guarantees about the bits.
    if sak & _SAK_BIT_CASCADE:
        return {
            "type": TYPE_UNKNOWN,
            "label": "incomplete UID (cascade level not resolved)",
            "capacity_bytes": 0,
        }
    if sak & _SAK_BIT_ISO14443_4:
        return {
            "type": TYPE_ISO14443_4,
            "label": "ISO 14443-4 smartcard",
            "capacity_bytes": 0,
        }
    if sak & _SAK_BIT_NFCIP1:
        return {
            "type": TYPE_UNKNOWN,
            "label": "NFCIP-1 peer-to-peer target",
            "capacity_bytes": 0,
        }
    return {"type": TYPE_UNKNOWN, "label": f"unrecognised SAK 0x{sak:02X}", "capacity_bytes": 0}


# ── NTAG / Ultralight GET_VERSION ───────────────────────────────────────
# GET_VERSION (command 0x60) returns 8 bytes:
#   0: fixed header (0x00)
#   1: vendor ID (0x04 = NXP)
#   2: product type   (0x03 = Ultralight, 0x04 = NTAG)
#   3: product subtype
#   4: major version
#   5: minor version
#   6: storage size
#   7: protocol type
# The original MIFARE Ultralight (MF0ICU1) predates the command and NAKs it,
# which is itself identifying information.
_VENDOR_NXP = 0x04
_PRODUCT_ULTRALIGHT = 0x03
_PRODUCT_NTAG = 0x04

# storage_size -> (model name, user bytes).  The encoded size is a power-of-two
# bracket, not the real figure: 0x0F means "between 128 and 256 bytes", and the
# NTAG213 that reports it actually has 144.  Only the real numbers are useful,
# so they are tabulated rather than computed.
_NTAG_STORAGE = {
    0x0F: ("NTAG213", 144),
    0x11: ("NTAG215", 504),
    0x13: ("NTAG216", 888),
}
_ULTRALIGHT_STORAGE = {
    0x0B: ("MIFARE Ultralight EV1 (MF0UL11)", 48),
    0x0E: ("MIFARE Ultralight EV1 (MF0UL21)", 128),
}

# Original MIFARE Ultralight: 16 pages total, pages 4-15 usable.
ULTRALIGHT_C1_USER_BYTES = 48


def parse_version(version: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Decode a GET_VERSION response into model name and user capacity.

    Returns ``None`` when the response is absent, truncated, or from a vendor
    whose layout we do not know — in which case the caller falls back to the
    capability container.
    """
    if not version or len(version) < 8:
        return None
    if version[1] != _VENDOR_NXP:
        return None

    product_type = version[2]
    storage = version[6]

    if product_type == _PRODUCT_NTAG:
        model, user_bytes = _NTAG_STORAGE.get(storage, (None, 0))
        if model is None:
            # An NTAG we do not have tabulated. The bracket still bounds it:
            # take the lower power of two, which under-promises rather than
            # writing past the end of the tag.
            user_bytes = 1 << (storage >> 1)
            model = f"NTAG (storage 0x{storage:02X})"
        return {
            "type": TYPE_NTAG,
            "label": model,
            "user_bytes": user_bytes,
            "user_pages": user_bytes // 4,
        }

    if product_type == _PRODUCT_ULTRALIGHT:
        model, user_bytes = _ULTRALIGHT_STORAGE.get(storage, (None, 0))
        if model is None:
            user_bytes = 1 << (storage >> 1)
            model = f"MIFARE Ultralight EV1 (storage 0x{storage:02X})"
        return {
            "type": TYPE_ULTRALIGHT,
            "label": model,
            "user_bytes": user_bytes,
            "user_pages": user_bytes // 4,
        }

    return None


# ── Random / non-stable UIDs ────────────────────────────────────────────
# ISO 14443-3 reserves single-size UIDs beginning 0x08 for randomly generated
# identifiers.  DESFire configured for privacy and virtually every phone doing
# host card emulation present one, and it differs on every tap.  Pairing such a
# tag by UID can never work, so it has to be recognised rather than stored.
RANDOM_UID_PREFIX = 0x08


def is_random_uid(uid: bytes) -> bool:
    """True when this UID is randomly generated and will differ next tap."""
    return len(uid) == 4 and len(uid) > 0 and uid[0] == RANDOM_UID_PREFIX


def uid_is_plausible(uid: Optional[bytes]) -> bool:
    """True when ``uid`` is a length ISO 14443-3 anticollision can produce.

    Single, double and triple cascade give 4, 7 and 10 bytes; FeliCa reports an
    8-byte IDm.  Anything else is a framing error being read as identity, which
    is how a corrupt frame used to become a brand new tag.
    """
    return bool(uid) and len(uid) in (4, 7, 8, 10)
