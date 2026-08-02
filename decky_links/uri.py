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

# rungameid does NOT carry an app id. For a non-Steam shortcut it carries a
# gameID64, which packs the app id into the high 32 bits — see
# shortcutAppIdToGameId64 in src/lib/steamIds.ts, which is what the panel's
# "Pair Current Game" button produces. Those are up to 20 digits, so checking
# them against the uint32 pattern rejected every non-Steam shortcut: pairing
# one failed at start_pairing with nothing but a log line to say why.
STEAM_GAMEID_PATTERN = re.compile(r"^[0-9]{1,20}$")

# Which pattern applies to which endpoint.
_ID_PATTERNS = {
    "steam://run/": STEAM_APPID_PATTERN,
    "steam://rungameid/": STEAM_GAMEID_PATTERN,
}

MAX_URI_LENGTH = 2048


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
            if not game_id or not _ID_PATTERNS[prefix].match(game_id):
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
