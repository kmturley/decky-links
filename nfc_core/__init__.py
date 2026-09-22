"""NFC module for Decky Links plugin.

This module provides NFC reader abstractions and tag handlers for the
Decky Links plugin.
"""

from nfc_core.reader import Reader, PN532UARTReader
from nfc_core.key_manager import KeyManager

__all__ = [
    'Reader',
    'PN532UARTReader',
    'KeyManager',
]
