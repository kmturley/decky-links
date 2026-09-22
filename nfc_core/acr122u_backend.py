"""ACR122U reader backend using PC/SC interface.

This module provides direct support for ACR122U USB NFC readers via the
PC/SC (pcsclite) interface, bypassing nfcpy for lower-level control.

The central thing to understand about PC/SC is that a *connection* is to a
**card**, not to a reader.  The previous implementation called
``createConnection().connect()`` once, at plugin start, and held the result
forever.  That has two consequences, both of which users saw:

* If no card was on the reader at start-up — the normal case — ``connect()``
  raised, ``ACR122UReader.connect()`` returned False, and the reader was
  reported as absent until the plugin was restarted with a card in place.
* Once a card was removed, the handle became invalid and every later
  ``transmit`` failed, so the reader never recovered.

Card connections are now made and dropped per card, which is also what makes
removal detectable: a card that will not connect is a card that is not there.
"""

from typing import Optional, Tuple

from nfc_core import tag_identity
from nfc_core.reader import (
    Reader,
    ReaderProtocolError,
    ReaderTransportError,
    TargetInfo,
)

# ── ACR122U pseudo-APDUs (ACR122U API v2.04) ───────────────────────────
_APDU_GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]
_APDU_GET_FIRMWARE = [0xFF, 0x00, 0x48, 0x00, 0x00]
_SW_OK = (0x90, 0x00)

# PC/SC contactless storage-card ATR, per the PC/SC part 3 supplement:
#   3B 8F 80 01 80 4F 0C A0 00 00 03 06 SS NN NN RR RR RR RR TCK
# Bytes 13-14 are the card name, which the PC/SC driver derived from the
# ATQA/SAK it saw during activation.  Mapping it back to a representative SAK
# recovers the same family information the PN532 backend reads directly, so
# both backends classify tags the same way and neither has to probe for it.
_ATR_CARD_NAME_OFFSET = 13
_ATR_MIN_LENGTH = 15
_ATR_RID = (0xA0, 0x00, 0x00, 0x03, 0x06)
_ATR_RID_OFFSET = 7

# PC/SC card name -> representative SAK.
_CARD_NAME_TO_SAK = {
    0x0001: 0x08,   # MIFARE Classic 1K
    0x0002: 0x18,   # MIFARE Classic 4K
    0x0003: 0x00,   # MIFARE Ultralight / NTAG
    0x0026: 0x09,   # MIFARE Mini
    0x003A: 0x00,   # MIFARE Ultralight C
    0x0036: 0x00,   # MIFARE Ultralight EV1 48b
    0x0037: 0x00,   # MIFARE Ultralight EV1 128b
    0xFF28: 0x20,   # JCOP 30 (ISO 14443-4)
}


class ACR122UReader(Reader):
    """ACR122U reader using PC/SC smartcard interface."""

    def __init__(self, logger=None):
        self.logger = logger
        # The reader device itself, found once and kept.
        self._device = None
        # The connection to whatever card is currently on it, or None.
        self._connection = None
        self._atr = None
        self._last_target: Optional[TargetInfo] = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Find the ACR122U. Does not require a card to be present."""
        try:
            from smartcard.System import readers

            reader_list = readers()
            if not reader_list:
                if self.logger:
                    self.logger.error("No PC/SC readers found")
                return False

            # Find ACR122U reader
            acr_reader = None
            for r in reader_list:
                if "ACR122" in str(r):
                    acr_reader = r
                    break

            if not acr_reader:
                if self.logger:
                    self.logger.error("ACR122U reader not found")
                return False

            self._device = acr_reader
            if self.logger:
                self.logger.info(f"Connected to ACR122U: {acr_reader}")
            return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"ACR122U connect failed: {e}")
            return False

    def close(self) -> None:
        self._release_card()
        self._device = None
        self._last_target = None

    def is_connected(self) -> bool:
        # The reader, not the card. A reader with nothing on it is working
        # perfectly well; it just has nothing to report.
        return self._device is not None or self._connection is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """ACR122U firmware version via GET DATA command."""
        data = self._transmit(_APDU_GET_FIRMWARE, need_card=False)
        if data and len(data) >= 4:
            return tuple(data[:4])
        return None

    # ── Card connection management ──────────────────────────────────────

    def _release_card(self) -> None:
        """Drop the current card connection, if any."""
        if self._connection:
            try:
                self._connection.disconnect()
            except Exception:
                pass
        self._connection = None
        self._atr = None

    def _ensure_card(self) -> bool:
        """Connect to the card on the reader, returning False if there is none.

        "No card" is an ordinary, expected answer here — it is how absence is
        detected — so it must not be logged as an error or escalated into a
        reader failure.
        """
        if self._connection is not None:
            return True
        if self._device is None:
            return False
        try:
            connection = self._device.createConnection()
            connection.connect()
        except Exception:
            # NoCardException and friends. Nothing on the reader.
            return False
        self._connection = connection
        try:
            self._atr = connection.getATR()
        except Exception:
            self._atr = None
        return True

    def release_target(self) -> None:
        """Drop the card connection so the next poll re-detects presence."""
        self._release_card()

    def recover(self) -> None:
        """Resynchronise by dropping the card connection."""
        self._release_card()

    def _transmit(self, apdu, need_card: bool = True) -> Optional[list]:
        """Send an APDU, returning the data on success or ``None`` otherwise.

        A transmit failure invalidates the card connection: in PC/SC that is
        what a removed card looks like, and holding on to the dead handle is
        what made the old implementation stop working permanently the first
        time a card was lifted.
        """
        if need_card and not self._ensure_card():
            return None
        if self._connection is None:
            return None
        try:
            data, sw1, sw2 = self._connection.transmit(list(apdu))
        except Exception as e:
            self._release_card()
            if self.logger:
                self.logger.debug(f"ACR122U transmit failed: {e}")
            return None
        if (sw1, sw2) != _SW_OK:
            return None
        return data

    # ── Detection ───────────────────────────────────────────────────────

    @property
    def supports_target_info(self) -> bool:
        return True

    def last_target(self) -> Optional[TargetInfo]:
        return self._last_target

    def read_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Detect the card on the reader and identify its family from the ATR."""
        if not self._ensure_card():
            self._last_target = None
            return None

        data = self._transmit(_APDU_GET_UID)
        if not data:
            # Connected but the UID query failed: the card is in a bad state.
            # Drop it so the next poll starts cleanly rather than reporting a
            # phantom presence.
            self._release_card()
            self._last_target = None
            return None

        target = TargetInfo(
            uid=bytes(data),
            sak=_sak_from_atr(self._atr),
            protocol=tag_identity.PROTO_106A,
        )
        self._last_target = target
        return target

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID via APDU."""
        data = self._transmit(_APDU_GET_UID)
        return bytes(data) if data else None

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read ISO-14443B UID.

        PC/SC presents a Type B card through the same GET UID pseudo-APDU, so
        there is nothing separate to do — but the ATR says which it was, and
        only a Type B card's UID is returned here.
        """
        if not self._ensure_card():
            return None
        if _atr_standard(self._atr) != 0x03:
            return None
        return self.read_uid(timeout=timeout)

    def transceive(self, data: bytes, timeout: float = 0.1) -> Optional[bytes]:
        """Send raw APDU."""
        cmd = [0xFF, 0x00, 0x00, 0x00, len(data)] + list(data)
        response = self._transmit(cmd)
        return bytes(response) if response is not None else None

    def get_version(self) -> Optional[bytes]:
        """NTAG / Ultralight GET_VERSION, wrapped in the direct-transmit APDU.

        The ACR122U is a PN532 behind a PC/SC front end, so the chip's
        InCommunicateThru is reachable through the pseudo-APDU escape. A
        failure here is ordinary — original Ultralights NAK the command — and
        the caller falls back to the capability container.
        """
        response = self.transceive(bytes([0xD4, 0x42, 0x60]))
        # Expected: D5 43 <status> <8 version bytes>
        if not response or len(response) < 11:
            return None
        if response[0] != 0xD5 or response[2] != 0x00:
            return None
        return bytes(response[3:11])

    # ── Tag commands ────────────────────────────────────────────────────

    def ntag2xx_read_block(self, block: int) -> Optional[bytes]:
        """Read NTAG block via APDU."""
        data = self._transmit([0xFF, 0xB0, 0x00, block & 0xFF, 0x04])
        return bytes(data) if data else None

    def ntag2xx_write_block(self, block: int, data: bytes) -> bool:
        """Write NTAG block via APDU."""
        if len(data) != 4:
            return False
        cmd = [0xFF, 0xD6, 0x00, block & 0xFF, 0x04] + list(data)
        return self._transmit(cmd) is not None

    def mifare_classic_authenticate_block(
        self, uid: bytes, block: int, key_type: int, key: bytes
    ) -> bool:
        """Authenticate Mifare Classic block."""
        if len(key) != 6:
            return False
        # Load the key into volatile key slot 0.
        if self._transmit([0xFF, 0x82, 0x00, 0x00, 0x06] + list(key)) is None:
            return False
        # General Authenticate: version, 0x00, block, key type, key slot.
        key_num = 0x60 if key_type == 0x60 else 0x61
        cmd = [0xFF, 0x86, 0x00, 0x00, 0x05,
               0x01, 0x00, block & 0xFF, key_num, 0x00]
        return self._transmit(cmd) is not None

    def mifare_classic_read_block(self, block: int) -> Optional[bytes]:
        """Read Mifare Classic block."""
        data = self._transmit([0xFF, 0xB0, 0x00, block & 0xFF, 0x10])
        return bytes(data) if data else None

    def mifare_classic_write_block(self, block: int, data: bytes) -> bool:
        """Write Mifare Classic block."""
        if len(data) != 16:
            return False
        cmd = [0xFF, 0xD6, 0x00, block & 0xFF, 0x10] + list(data)
        return self._transmit(cmd) is not None


# ── ATR parsing ─────────────────────────────────────────────────────────


def _atr_is_picc(atr) -> bool:
    """True when this ATR is the PC/SC synthetic one for a contactless card."""
    if not atr or len(atr) < _ATR_MIN_LENGTH:
        return False
    rid = tuple(atr[_ATR_RID_OFFSET:_ATR_RID_OFFSET + len(_ATR_RID)])
    return rid == _ATR_RID


def _atr_standard(atr) -> Optional[int]:
    """The card standard byte (0x03 = ISO 14443-A, 0x03+ per the supplement)."""
    if not _atr_is_picc(atr):
        return None
    return atr[12]


def _sak_from_atr(atr) -> Optional[int]:
    """Recover a representative SAK from the PC/SC card name in the ATR."""
    if not _atr_is_picc(atr):
        return None
    name = (atr[_ATR_CARD_NAME_OFFSET] << 8) | atr[_ATR_CARD_NAME_OFFSET + 1]
    return _CARD_NAME_TO_SAK.get(name)
