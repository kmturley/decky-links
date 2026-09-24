"""NDEF TLV framing and URI extraction.

The NFC Forum Type 2 (NTAG, Ultralight) and Type 4 data areas do not hold a
bare NDEF message.  They hold a sequence of TLV blocks, of which the NDEF
message is one.  The previous implementation assumed the message began at the
very first byte with ``03 <len>``, which is true of a freshly formatted tag
and false of several common ones:

* Tags with a Lock Control (``0x01``) or Memory Control (``0x02``) TLV ahead
  of the message — normal on NTAG216 and on any tag formatted by a tool that
  describes its lock bytes.
* Messages longer than 254 bytes, which use the three-byte length form
  ``03 FF <hi> <lo>``.
* Tags padded with NULL (``0x00``) TLVs, which are legal anywhere.

It also stopped reading at the first ``0xFE`` byte found *anywhere* in a
block, including inside UTF-8 payload, truncating longer URIs mid-record.
This module walks the TLV structure properly instead.

Reference: NFC Forum Type 2 Tag Operation Specification §2.3.
"""

import re
from typing import Iterator, List, Optional, Tuple

TLV_NULL = 0x00
TLV_LOCK_CONTROL = 0x01
TLV_MEMORY_CONTROL = 0x02
TLV_NDEF_MESSAGE = 0x03
TLV_PROPRIETARY = 0xFD
TLV_TERMINATOR = 0xFE

# Length byte 0xFF introduces a two-byte big-endian length.
_LONG_LENGTH_MARKER = 0xFF
# Above this, a single length byte cannot be used.
_MAX_SHORT_LENGTH = 0xFE


def iter_tlvs(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """Yield ``(tag, value)`` for each TLV block in ``data``.

    Stops cleanly at a terminator TLV or at the end of the buffer.  A
    malformed or truncated block ends iteration rather than raising: the data
    came off a tag over an error-prone RF link, and half a message read is
    a normal occurrence rather than an exceptional one.
    """
    i = 0
    n = len(data)
    while i < n:
        tag = data[i]

        if tag == TLV_TERMINATOR:
            return
        if tag == TLV_NULL:
            # NULL TLV carries neither length nor value.
            i += 1
            continue

        # Every other TLV has a length field.
        if i + 1 >= n:
            return
        length = data[i + 1]
        if length == _LONG_LENGTH_MARKER:
            if i + 3 >= n:
                return
            length = (data[i + 2] << 8) | data[i + 3]
            value_start = i + 4
        else:
            value_start = i + 2

        value_end = value_start + length
        if value_end > n:
            # Truncated value — hand back what we have for the NDEF TLV, since
            # a partial message may still decode to a usable first record.
            yield tag, bytes(data[value_start:n])
            return

        yield tag, bytes(data[value_start:value_end])
        i = value_end


def find_ndef_message(data: bytes) -> Optional[bytes]:
    """Return the NDEF message payload from a Type 2 data area, or None."""
    if not data:
        return None
    for tag, value in iter_tlvs(data):
        if tag == TLV_NDEF_MESSAGE:
            return value
    return None


def encode_tlv(message: bytes) -> bytearray:
    """Wrap an NDEF message in an NDEF TLV plus terminator.

    Uses the three-byte length form when the message needs it, which the
    previous encoder never did — so any URI over 254 bytes was written with a
    truncated length byte and read back as garbage.
    """
    out = bytearray([TLV_NDEF_MESSAGE])
    if len(message) > _MAX_SHORT_LENGTH:
        out.append(_LONG_LENGTH_MARKER)
        out.append((len(message) >> 8) & 0xFF)
        out.append(len(message) & 0xFF)
    else:
        out.append(len(message))
    out.extend(message)
    out.append(TLV_TERMINATOR)
    return out


def tlv_overhead(message_length: int) -> int:
    """Bytes the TLV framing adds around a message of this length."""
    return (4 if message_length > _MAX_SHORT_LENGTH else 2) + 1


def is_terminated(data: bytes) -> bool:
    """True when ``data`` contains a complete, properly terminated TLV run.

    Used to decide whether more pages need reading.  Unlike a bare ``0xFE in
    block`` test this understands TLV structure, so a ``0xFE`` byte inside a
    URI does not end the read early.
    """
    i = 0
    n = len(data)
    while i < n:
        tag = data[i]
        if tag == TLV_TERMINATOR:
            return True
        if tag == TLV_NULL:
            i += 1
            continue
        if i + 1 >= n:
            return False
        length = data[i + 1]
        if length == _LONG_LENGTH_MARKER:
            if i + 3 >= n:
                return False
            length = (data[i + 2] << 8) | data[i + 3]
            i = i + 4 + length
        else:
            i = i + 2 + length
    return False


# ── Record decoding ─────────────────────────────────────────────────────

# NDEF URI record well-known type 'U'.
_URI_RECORD_TYPE = 0x55

# Abbreviations the URI record's first payload byte stands in for, from the
# NFC Forum URI Record Type Definition.  The old fallback path ignored this
# byte entirely, so a tag written with the standard 0x03 prefix came back as
# "steam://..." with a stray leading byte, or as "://..." with the scheme
# silently eaten.
URI_PREFIXES = (
    "", "http://www.", "https://www.", "http://", "https://", "tel:",
    "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://",
    "sftp://", "smb://", "nfs://", "ftp://", "dav://", "news:",
    "telnet://", "imap:", "rtsp://", "urn:", "pop:", "sip:", "sips:",
    "tftp:", "btspp://", "btl2cap://", "btgoep://", "tcpobex://",
    "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:",
    "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
)

_URI_PATTERN = re.compile(rb"([a-zA-Z][a-zA-Z0-9+.\-]{0,31}://[^\x00\xfe\s]{1,2048})")


def uri_record_size(uri: str) -> int:
    """Bytes an NDEF URI record for ``uri`` occupies, prefix abbreviation included.

    Computed from the URI rather than from an encoded message, so a capacity
    check never depends on the encoder having succeeded.  Writing a tag we
    have not measured is how a URI ends up half-written across a tag too
    small to hold it.
    """
    encoded = uri.encode("utf-8")
    prefix_len = 0
    for candidate in URI_PREFIXES[1:]:
        if uri.startswith(candidate) and len(candidate) > prefix_len:
            prefix_len = len(candidate)
    payload_len = 1 + (len(encoded) - prefix_len)
    # flags(1) + type length(1) + payload length(1 short | 4 long) + type(1)
    header = 3 + (1 if payload_len <= _MAX_SHORT_LENGTH else 4)
    return header + payload_len


def uri_storage_size(uri: str) -> int:
    """Total bytes on the tag for ``uri``: the record plus its TLV framing."""
    record = uri_record_size(uri)
    return record + tlv_overhead(record)


def decode_uri_payload(payload: bytes) -> Optional[str]:
    """Decode a URI record payload (prefix byte + UTF-8 remainder)."""
    if not payload:
        return None
    prefix_code = payload[0]
    prefix = URI_PREFIXES[prefix_code] if prefix_code < len(URI_PREFIXES) else ""
    try:
        rest = payload[1:].decode("utf-8")
    except UnicodeDecodeError:
        rest = payload[1:].decode("utf-8", errors="replace")
    uri = (prefix + rest).strip().strip("\x00")
    return uri or None


def extract_uri_fallback(data: bytes) -> Optional[str]:
    """Last-resort URI recovery from a data area that would not parse.

    Tries the URI record structure first, then a plain scheme match.  Kept
    because a tag written by some other tool — or one read with a dropped
    page — is still worth launching if a URI can be seen in it at all.
    """
    if not data:
        return None

    # Look for a well-known URI record: TNF/flags byte, type length 1,
    # payload length, type 'U'.
    for i in range(len(data) - 3):
        if data[i + 1] == 0x01 and data[i + 3] == _URI_RECORD_TYPE:
            payload_len = data[i + 2]
            payload = data[i + 4:i + 4 + payload_len]
            if payload:
                uri = decode_uri_payload(payload)
                if uri and "://" in uri:
                    return uri

    match = _URI_PATTERN.search(bytes(data))
    if match:
        try:
            return match.group(1).decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    return None


def records_to_dicts(records: List) -> List[dict]:
    """Flatten ndeflib records into JSON-serialisable dicts for the frontend."""
    out = []
    for record in records:
        rec = {}
        for attr in ("type", "name", "uri", "text", "language", "encoding"):
            if hasattr(record, attr):
                try:
                    value = getattr(record, attr)
                except Exception:
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    rec[attr] = value
                else:
                    rec[attr] = str(value)
        out.append(rec)
    return out


def first_uri(records: List) -> Optional[str]:
    """Return the first URI record's value from a decoded record list."""
    for record in records:
        if hasattr(record, "uri") and record.__class__.__name__.endswith("UriRecord"):
            return record.uri
    return None
