"""NFC module for Decky Links plugin.

This module provides NFC reader abstractions and tag handlers for the
Decky Links plugin.
"""

from nfc.reader import Reader, PN532UARTReader
from nfc.key_manager import KeyManager
from nfc.signature_manager import SignatureManager
from nfc.signature_record import SignatureRecord

__all__ = [
    'Reader',
    'PN532UARTReader',
    'KeyManager',
    'SignatureManager',
    'SignatureRecord',
]
