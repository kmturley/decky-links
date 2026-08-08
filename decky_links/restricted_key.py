"""The key: a medium that locks and unlocks the plugin.

A key is not a new kind of medium. It is an ordinary payload of a
reserved shape — ``decky-links://key/<token>`` — written by the ordinary
pairing path onto whatever the user wants to carry it: a tag, a floppy, a USB
stick, a printed QR card. Everything downstream of a source already moves a URI
string around, so the key travels on rails that exist.

Identity lives on the medium rather than in a table on the device, which is the
same decision as §2.6 of the spec and for the same reason: a key registered on
one Deck opens another Deck that was told the same secret, and there is no
database to keep in step. What the device stores is a SHA-256 of the token, so
settings.json holds something that can *recognise* the key without being one.

That is worth being plain about: this stops a child, a guest, or a curious
sibling. Anyone who can read the medium can copy it, and anyone who can read
the disk in desktop mode can bypass the whole thing. The lock is a guardrail
around a shared living-room device, not a security boundary.
"""

import hashlib
import re
import secrets
from typing import Optional

# Reserved scheme. Deliberately not in decky_links.uri's allowlist: a key
# payload is a control message, never something to launch or navigate to, and
# the two must not be confusable. The plugin intercepts it before the allowlist
# is ever consulted, so a copy of this URI reaching any other code path is
# rejected as an unknown scheme, which is exactly right.
KEY_URI_PREFIX = "decky-links://key/"

# Read, never written. The payload was called "master" while this feature was
# being built, and keys written then are on physical objects that someone still
# has in a drawer — a rename here turns one of those into a medium that reads
# as an unrecognised URI, which is the silent breakage the presence model
# exists to avoid. Droppable once no such key can still be in circulation.
_LEGACY_URI_PREFIXES = ("decky-links://master/",)

TOKEN_BYTES = 16
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def mint_token() -> str:
    """A fresh key token, as lowercase hex."""
    return secrets.token_hex(TOKEN_BYTES)


def uri_for(token: str) -> str:
    """The payload written onto a medium to make it a key."""
    return f"{KEY_URI_PREFIX}{token}"


def parse_token(uri) -> Optional[str]:
    """The token carried by ``uri``, or None when this is not a key payload.

    Shape is checked here rather than at the comparison, so a malformed token
    never reaches the hash: "is this a key at all" and "is it *the*
    key" are different questions and the caller needs to tell a
    stranger's tag apart from a game tag.
    """
    if not isinstance(uri, str):
        return None
    for prefix in (KEY_URI_PREFIX, *_LEGACY_URI_PREFIXES):
        if uri.startswith(prefix):
            token = uri[len(prefix):].strip().lower()
            return token if _TOKEN_PATTERN.match(token) else None
    return None


def hash_token(token: str) -> str:
    """The stored form of a token: SHA-256, hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def matches(token: str, stored_hash: str) -> bool:
    """Whether ``token`` is the registered key.

    An empty ``stored_hash`` means no key is registered, and must never match —
    without that check the empty string would hash to a fixed value that a
    crafted payload could carry.
    """
    if not token or not stored_hash:
        return False
    return secrets.compare_digest(hash_token(token), stored_hash.lower())
