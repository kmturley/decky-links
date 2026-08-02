"""The plugin's trust boundary.

Media arrives from outside — a tag someone handed you, a disk, a QR code in a
camera frame, an MQTT message — and this module is the whole of what decides
whether the URI on it may be acted on. It is deliberately small, has no
dependency on plugin state, and imports nothing from the plugin, so it can be
read and tested on its own.

It lived on ``Plugin`` as ``_validate_uri``, which meant standing up a plugin
instance — settings, key manager, six sources — to test a pure function.
"""

import ipaddress
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

# Steam links are narrowed to launch endpoints. steam:// has many verbs, most
# of which do things a card tapped on a table should not be able to do.
ALLOWED_STEAM_URI_PREFIXES = (
    "steam://run/",
    "steam://rungameid/",
)
ALLOWED_URI_SCHEMES = ("https://",)

# 1-10 digits: an app id is a uint32, so this is the widest it can be.
STEAM_APPID_PATTERN = re.compile(r"^[0-9]{1,10}$")

# rungameid takes one of two things, and they are not interchangeable.
#
# A Steam game is named by its plain app id. A non-Steam shortcut cannot be —
# steam://run/ does not launch shortcuts at all — so the panel builds a
# gameID64 instead, via shortcutAppIdToGameId64 in src/lib/steamIds.ts:
#
#     gameID64 = ((appid | 0x80000000) << 32) | 0x02000000
#
# which is a 20-digit number. Checking both against the uint32 app-id pattern
# rejected every shortcut, so pairing one failed inside start_pairing with
# only a log line to say why.
#
# The fix is to accept that second form *structurally* rather than by widening
# the digit count: a gameID64 has a fixed shape, and "any 20 digits" would
# admit arbitrary numbers that Steam would do something unpredictable with.
SHORTCUT_FLAG = 0x80000000      # set in the high word for a shortcut
SHORTCUT_TYPE = 0x02000000      # CGameID type 2, in the low word
U32_MASK = 0xFFFFFFFF
MAX_APPID = U32_MASK

MAX_URI_LENGTH = 2048


def is_shortcut_gameid64(value: str) -> bool:
    """True for a gameID64 of the exact shape the panel builds for shortcuts.

    Deliberately strict. The low word must be exactly the shortcut type and
    the high word must carry the flag, which is every value
    ``shortcutAppIdToGameId64`` can produce and nothing else.
    """
    if not value.isdigit():
        return False
    n = int(value)
    if n <= U32_MASK or n > 0xFFFFFFFFFFFFFFFF:
        return False
    return (n & U32_MASK) == SHORTCUT_TYPE and bool((n >> 32) & SHORTCUT_FLAG)


def _valid_launch_id(prefix: str, value: str) -> bool:
    """Whether ``value`` is a launchable id for this endpoint.

    ``steam://run/`` really does take an app id and keeps the tighter bound.
    ``steam://rungameid/`` additionally accepts a shortcut gameID64, because
    that is the only way a non-Steam game can be launched.
    """
    if STEAM_APPID_PATTERN.match(value) and 0 < int(value) <= MAX_APPID:
        return True
    if prefix == "steam://rungameid/":
        return is_shortcut_gameid64(value)
    return False


def is_valid_appid(appid) -> bool:
    """True for something that could be a Steam app id.

    Used well beyond URI parsing: ``appid`` is interpolated into a filesystem
    path when rendering card art, in a process running as root.
    """
    return bool(appid) and bool(STEAM_APPID_PATTERN.match(str(appid)))


def is_local_host(hostname: str) -> bool:
    """True when a hostname literal points at this machine or its network.

    Scope, stated because the check this replaced invited a bigger reading
    than it delivered: this stops a tapped card opening the Deck's own
    services, or a box on the same LAN, in the Steam browser. It looks at the
    *literal* in the URI, so it cannot stop a public name that resolves to a
    private address — that means resolving at launch time and racing DNS,
    which is not worth it when the user physically taps the card.

    What it covers that the old three-string comparison did not: the rest of
    127.0.0.0/8, the bracketed IPv6 form a URI actually carries, IPv4-mapped
    IPv6, the RFC1918 ranges, link-local including the cloud metadata address,
    and .local names.
    """
    host = hostname.strip().strip("[]").lower()
    if not host:
        return True

    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True

    try:
        # ip_address handles IPv4, IPv6 and the ::ffff:127.0.0.1 mapped form,
        # so the numeric variants do not need enumerating by hand.
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False

    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped

    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local      # includes 169.254.169.254
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate(uri) -> Tuple[bool, Optional[str]]:
    """Check a URI against the allowlist. Returns ``(ok, reason)``.

    The reason exists so the caller can log *why* rather than just that it
    refused — a blocked card is otherwise indistinguishable from a broken
    reader, and the two need very different responses from the user.
    """
    if not isinstance(uri, str) or not uri:
        return False, "empty or non-string URI"
    if len(uri) > MAX_URI_LENGTH:
        return False, f"URI longer than {MAX_URI_LENGTH} characters"

    for prefix in ALLOWED_STEAM_URI_PREFIXES:
        if uri.startswith(prefix):
            remainder = uri[len(prefix):]
            game_id = remainder.split("/")[0]
            if not game_id or not _valid_launch_id(prefix, game_id):
                return False, f"invalid Steam app id {game_id!r}"
            # Nothing may sit between the prefix and the id.
            if "/" in remainder and not remainder.startswith(game_id + "/"):
                return False, "suspicious Steam URI structure"
            return True, None

    if uri.startswith("https://"):
        try:
            parsed = urlparse(uri)
        except Exception:
            return False, "unparseable URL"
        if not parsed.hostname or "." not in parsed.netloc:
            return False, "no valid host"
        if is_local_host(parsed.hostname):
            return False, f"{parsed.hostname} is local to this device or network"
        return True, None

    return False, "scheme not in the allowlist (steam://run, steam://rungameid, https)"


def is_valid(uri) -> bool:
    """``validate`` without the reason, for call sites that only branch."""
    return validate(uri)[0]
