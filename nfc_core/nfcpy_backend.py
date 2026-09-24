"""nfcpy reader backend.

This module provides support for NFC readers via the nfcpy library,
which supports a wide range of readers including ACR122U, SCL3711, and others.

``import nfc`` in this file means nfcpy.  It did not always: the project's own
NFC package was called ``nfc`` too, so this import found that instead and
every nfcpy call raised ``AttributeError`` into a bare ``except``.  The
project package is now ``nfc_core`` so the two can coexist.
"""

from typing import Optional, Tuple

from nfc_core import tag_identity
from nfc_core.reader import Reader, TargetInfo

# nfcpy target strings, in the order they are swept each poll.  The old code
# sensed only '106A', so a Type B card or a FeliCa tag was invisible to this
# backend however well nfcpy supported it.
_SENSE_TARGETS = ('106A', '106B', '212F', '424F')

_TARGET_PROTOCOL = {
    '106A': tag_identity.PROTO_106A,
    '106B': tag_identity.PROTO_106B,
    '212F': tag_identity.PROTO_212F,
    '424F': tag_identity.PROTO_424F,
}


def _normalise_device_path(path: Optional[str]) -> str:
    """Turn a device path into the connection string nfcpy expects.

    nfcpy does not take a bare device node: a serial reader is addressed as
    ``tty:USB0:pn532``, not ``/dev/ttyUSB0``.  Passing the raw path — which is
    what the NFC source settings hold, because the PN532 backend needs it in
    that form — made ``ContactlessFrontend`` raise on every attempt.
    """
    if not path:
        return 'usb'
    if ':' in path:
        # Already an nfcpy connection string (usb, usb:04e6:5591, tty:...).
        return path
    if path.startswith('/dev/tty'):
        return f'tty:{path[len("/dev/tty"):]}:pn532'
    if path.startswith('/dev/'):
        return f'tty:{path[len("/dev/"):]}:pn532'
    return path


class NfcPyReader(Reader):
    """NFC reader using nfcpy library."""

    def __init__(self, device_path: Optional[str] = None, logger=None):
        self.device_path = _normalise_device_path(device_path)
        self.logger = logger
        self._clf = None
        self._target = None
        self._last_target: Optional[TargetInfo] = None

    async def connect(self) -> bool:
        try:
            import nfc

            # Open contactless frontend
            self._clf = nfc.ContactlessFrontend(self.device_path)

            if self._clf:
                if self.logger:
                    self.logger.info(f"Connected to nfcpy reader: {self._clf}")
                return True
            return False
        except Exception as e:
            if self.logger:
                self.logger.error(
                    f"nfcpy connect failed for {self.device_path!r}: {e}"
                )
            return False

    def close(self) -> None:
        if self._clf:
            try:
                self._clf.close()
            except Exception:
                pass
        self._clf = None
        self._target = None

    def is_connected(self) -> bool:
        return self._clf is not None

    def firmware_version(self) -> Optional[Tuple[int, int, int, int]]:
        """nfcpy doesn't expose firmware version directly."""
        return None

    @property
    def supports_target_info(self) -> bool:
        return True

    def last_target(self) -> Optional[TargetInfo]:
        return self._last_target

    def read_target(self, timeout: float = 0.2) -> Optional[TargetInfo]:
        """Sense every supported protocol in one pass and report the activation data.

        nfcpy's ``sense`` accepts several targets and tries each in turn, so
        covering Type B and FeliCa alongside Type A costs one call rather than
        four, and the SENS_RES / SEL_RES it hands back identify the tag family
        exactly — no probing required.
        """
        if not self._clf:
            return None

        try:
            import nfc

            targets = [nfc.clf.RemoteTarget(t) for t in _SENSE_TARGETS]
            target = self._clf.sense(
                *targets,
                iterations=max(1, int(timeout * 10)),
                interval=0.1,
            )
        except Exception:
            return None

        if not target:
            self._target = None
            self._last_target = None
            return None

        self._target = target
        info = _target_info(target)
        self._last_target = info
        return info

    def read_uid(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID using nfcpy sense."""
        target = self.read_target(timeout=timeout)
        return target.uid if target else None

    def release_target(self) -> None:
        """Forget the activated tag so the next sense re-detects it."""
        self._target = None

    def read_uid_iso14443b(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read UID from ISO-14443B tag."""
        if not self._clf:
            return None
        
        try:
            import nfc
            
            # Sense for ISO-14443B tags
            target = self._clf.sense(
                nfc.clf.RemoteTarget('106B'),
                iterations=int(timeout * 10),
                interval=0.1
            )
            
            if target and hasattr(target, 'identifier'):
                self._target = target
                return target.identifier
        except Exception:
            pass
        
        return None

    def transceive(self, data: bytes, timeout: float = 0.1) -> Optional[bytes]:
        """Send raw command via nfcpy exchange."""
        if not self._clf or not self._target:
            return None
        
        try:
            response = self._clf.exchange(data, timeout=timeout)
            return response if response else None
        except Exception:
            return None

    def ntag2xx_read_block(self, block: int) -> Optional[bytes]:
        """Read NTAG block using READ command."""
        if not self._clf or not self._target:
            return None
        
        try:
            # NTAG READ command: 0x30 + block number
            cmd = bytes([0x30, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            if response and len(response) >= 4:
                return response[:4]
        except Exception:
            pass
        
        return None

    def ntag2xx_write_block(self, block: int, data: bytes) -> bool:
        """Write NTAG block using WRITE command."""
        if not self._clf or not self._target or len(data) != 4:
            return False
        
        try:
            # NTAG WRITE command: 0xA2 + block number + 4 bytes data
            cmd = bytes([0xA2, block]) + data
            response = self._clf.exchange(cmd, timeout=0.1)
            
            # ACK is 0x0A for successful write
            return response == bytes([0x0A]) if response else False
        except Exception:
            return False

    def mifare_classic_authenticate_block(self, uid: bytes, block: int, key_type: int, key: bytes) -> bool:
        """Authenticate Mifare Classic block."""
        if not self._clf or not self._target or len(key) != 6:
            return False
        
        try:
            import nfc
            
            # nfcpy uses Tag object for Mifare Classic operations
            if hasattr(self._target, 'authenticate'):
                # key_type: 0x60 = Key A, 0x61 = Key B
                key_name = 'A' if key_type == 0x60 else 'B'
                return self._target.authenticate(block, key_name, key)
            
            # Fallback: manual authentication via exchange
            # AUTH command: 0x60/0x61 + block + key
            cmd = bytes([key_type, block]) + key
            response = self._clf.exchange(cmd, timeout=0.1)
            return response is not None
        except Exception:
            return False

    def mifare_classic_read_block(self, block: int) -> Optional[bytes]:
        """Read Mifare Classic block."""
        if not self._clf or not self._target:
            return None
        
        try:
            # Mifare Classic READ command: 0x30 + block number
            cmd = bytes([0x30, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            if response and len(response) >= 16:
                return response[:16]
        except Exception:
            pass
        
        return None

    def mifare_classic_write_block(self, block: int, data: bytes) -> bool:
        """Write Mifare Classic block."""
        if not self._clf or not self._target or len(data) != 16:
            return False
        
        try:
            # Mifare Classic WRITE command: 0xA0 + block number
            cmd = bytes([0xA0, block])
            response = self._clf.exchange(cmd, timeout=0.1)
            
            # Check for ACK (0x0A)
            if response != bytes([0x0A]):
                return False
            
            # Send data
            response = self._clf.exchange(data, timeout=0.1)
            return response == bytes([0x0A]) if response else False
        except Exception:
            return False

    def SAM_configuration(self):
        """Compatibility method - nfcpy doesn't need SAM configuration."""
        pass

    def read_passive_target(self, baud_rate: int = 0, timeout: float = 0.2) -> Optional[bytes]:
        """Compatibility method for PN532-style API."""
        if baud_rate == 3:
            # ISO-14443B
            return self.read_uid_iso14443b(timeout)
        else:
            # ISO-14443A (default)
            return self.read_uid(timeout)


def _target_info(target) -> Optional[TargetInfo]:
    """Build a :class:`TargetInfo` from an nfcpy ``RemoteTarget``.

    nfcpy exposes the raw activation bytes per protocol: ``sens_res`` /
    ``sel_res`` / ``sdd_res`` for Type A, ``sensb_res`` for Type B and
    ``sensf_res`` for FeliCa.  Carrying them through means the nfcpy backend
    classifies tags from the same evidence as the PN532 one.
    """
    brty = getattr(target, 'brty', None)
    protocol = _TARGET_PROTOCOL.get(brty, tag_identity.PROTO_106A)

    identifier = getattr(target, 'identifier', None)
    sak = None
    atqa = None

    sel_res = getattr(target, 'sel_res', None)
    if sel_res:
        try:
            sak = sel_res[0]
        except (IndexError, TypeError):
            sak = None

    sens_res = getattr(target, 'sens_res', None)
    if sens_res and len(sens_res) >= 2:
        try:
            # nfcpy reports SENS_RES least significant byte first.
            atqa = (sens_res[1] << 8) | sens_res[0]
        except (IndexError, TypeError):
            atqa = None

    if identifier is None:
        # Type B reports its PUPI inside SENSB_RES; FeliCa its IDm.
        sensb = getattr(target, 'sensb_res', None)
        sensf = getattr(target, 'sensf_res', None)
        if sensb and len(sensb) >= 5:
            identifier = bytes(sensb[1:5])
        elif sensf and len(sensf) >= 9:
            identifier = bytes(sensf[1:9])

    if identifier is None:
        return None

    return TargetInfo(
        uid=bytes(identifier), atqa=atqa, sak=sak, protocol=protocol,
    )
