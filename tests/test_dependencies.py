"""
test_dependencies.py — verify every third-party package in requirements.txt
can be imported in the local environment before a build is attempted.

These tests catch missing packages (like adafruit_pn532, ndef) that would
only surface as runtime errors on the Steam Deck after deployment.

Each test imports the package the same way the plugin code does, so the
module name here must match the actual import statement in the source, not
necessarily the PyPI package name (e.g. PyPI: ndeflib → import ndef).
"""
import importlib
import pytest


def _can_import(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


# ── Core NFC stack ────────────────────────────────────────────────────────────

def test_serial_importable():
    """pyserial — required by PN532UARTReader (nfc/reader.py)."""
    assert _can_import("serial"), (
        "pyserial not installed. Run: pip install pyserial  "
        "(or rebuild with build.sh to bundle it in py_modules/)"
    )


def test_adafruit_pn532_importable():
    """adafruit-circuitpython-pn532 — PN532 chip driver (nfc/reader.py)."""
    assert _can_import("adafruit_pn532"), (
        "adafruit_pn532 not installed. Run: pip install adafruit-circuitpython-pn532"
    )


def test_adafruit_pn532_uart_importable():
    """Specifically the UART sub-module used in PN532UARTReader."""
    assert _can_import("adafruit_pn532.uart"), (
        "adafruit_pn532.uart not importable — adafruit-circuitpython-pn532 may be incomplete"
    )


def test_ndef_importable():
    """ndeflib (import name: ndef) — NDEF encoding/decoding (nfc_source.py, main.py)."""
    assert _can_import("ndef"), (
        "ndef not installed. Run: pip install ndeflib  "
        "(PyPI package is 'ndeflib', but the import name is 'ndef')"
    )


# ── Cryptography ──────────────────────────────────────────────────────────────

def test_cryptography_importable():
    """cryptography — key management and signing (nfc/key_manager.py, nfc/signature_manager.py)."""
    assert _can_import("cryptography"), (
        "cryptography not installed. Run: pip install cryptography"
    )


def test_cryptography_fernet_importable():
    """Specifically the Fernet sub-module used by KeyManager."""
    assert _can_import("cryptography.fernet"), (
        "cryptography.fernet not importable — cryptography may be incomplete"
    )


# ── Optional / gracefully-degrading sources ───────────────────────────────────

def test_pyudev_importable():
    """pyudev — Linux udev monitor for StorageSource (Linux-only; degrades on macOS)."""
    assert _can_import("pyudev"), (
        "pyudev not installed. Run: pip install pyudev"
    )


def test_paho_mqtt_importable():
    """paho-mqtt — MQTT broker client for MqttSource."""
    assert _can_import("paho.mqtt.client"), (
        "paho-mqtt not installed. Run: pip install paho-mqtt"
    )


def test_pyzbar_importable():
    """pyzbar — QR code decoding for CameraSource."""
    assert _can_import("pyzbar"), (
        "pyzbar not installed. Run: pip install pyzbar"
    )


def test_pillow_importable():
    """Pillow (PIL) — image handling for CameraSource."""
    assert _can_import("PIL"), (
        "Pillow not installed. Run: pip install Pillow"
    )


def test_nfcpy_importable():
    """nfcpy — optional alternative NFC reader backend (nfc/nfcpy_backend.py)."""
    assert _can_import("nfc"), (
        "nfcpy not installed. Run: pip install nfcpy"
    )
