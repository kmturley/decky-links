"""The one place that decides whether a setting value is acceptable.

There used to be three answers to that question and they disagreed:

- ``SettingsManager._validate_setting`` — types plus ranges, applied to values
  loaded from disk and to ``set_setting``.
- ``Plugin._validate_setting`` — a near-identical copy carrying a comment
  admitting it was a copy.
- ``Plugin.set_source_setting`` — a flat ``{key: type}`` map that checked the
  *type* and nothing else.

The third was the dangerous one. Per-source settings reached it with no range
check at all, so ``broker_port`` accepted -1 and 70000, ``poll_interval``
accepted 0.0, serial ``port`` accepted any string at all — bypassing the
``/dev/`` prefix rule that the same class of value (``device_path``) was held
to two functions away — and ``watch_dir`` accepted ``/``, pointing a root
process's directory scanner at the filesystem root.

So the rules live here, once, as data. Both RPC entry points and the on-disk
loader go through :func:`validate`, which means a new setting is a line in a
table rather than an edit in three places that will drift again.
"""

import os
import re
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple


class Rule(NamedTuple):
    """How one setting is checked.

    ``types`` is what ``isinstance`` must accept. ``check`` is an optional
    extra predicate for anything types cannot express — a range, a prefix, a
    membership test. ``describe`` is what the user is told when it fails, so
    it has to read as a requirement rather than a restatement of the failure.
    """

    types: Tuple[type, ...]
    check: Optional[Callable[[Any], bool]] = None
    describe: str = ""


def _in_range(low, high):
    return lambda v: low <= float(v) <= high


def _is_dev_path(value: str) -> bool:
    """A device node, and only a device node.

    Normalised before the prefix test: "/dev/../etc/passwd" starts with
    "/dev/" but is not under it.
    """
    return (
        len(value) <= 255
        and os.path.normpath(value).startswith("/dev/")
    )


def _is_safe_watch_dir(value: str) -> bool:
    """An absolute directory that is not the root of something enormous.

    FileWatchSource scans this on a timer as root. Pointing it at ``/`` or
    ``/proc`` is not a useful configuration, it is a way to make the plugin
    walk the entire filesystem forever.
    """
    if not value or len(value) > 4096:
        return False
    path = os.path.normpath(value)
    if not os.path.isabs(path):
        return False
    forbidden = {"/", "/proc", "/sys", "/dev"}
    return path not in forbidden and not any(
        path.startswith(f + "/") for f in ("/proc", "/sys", "/dev")
    )


# ── Top-level settings ─────────────────────────────────────────────────────

TOP_LEVEL_RULES: Dict[str, Rule] = {
    "auto_launch": Rule((bool,), describe="true or false"),
    "auto_close": Rule((bool,), describe="true or false"),
}

# ── Per-source settings ────────────────────────────────────────────────────
#
# Keyed by source type so the same key name can mean different things: the
# serial source's "baudrate" and the NFC reader's are both baud rates, but
# "port" is a device path here and a TCP port for MQTT.

READER_TYPES = ("pn532_uart", "acr122u", "proxmark", "nfcpy")

SOURCE_RULES: Dict[str, Dict[str, Rule]] = {
    "nfc": {
        "enabled": Rule((bool,), describe="true or false"),
        "device_path": Rule((str,), _is_dev_path, "a path under /dev/"),
        "baudrate": Rule((int,), _in_range(1200, 1_000_000), "1200-1000000"),
        "polling_interval": Rule((int, float), _in_range(0.1, 10.0), "0.1-10.0 seconds"),
        "reader_type": Rule(
            (str,), lambda v: v in READER_TYPES, f"one of {', '.join(READER_TYPES)}"
        ),
    },
    "storage": {
        "enabled": Rule((bool,), describe="true or false"),
        "drive_kinds": Rule(
            (dict,),
            lambda v: all(isinstance(k, str) and isinstance(x, bool) for k, x in v.items()),
            "a map of drive kind to true/false",
        ),
    },
    "camera": {
        "enabled": Rule((bool,), describe="true or false"),
        "device": Rule((str,), _is_dev_path, "a path under /dev/"),
        "poll_interval": Rule((int, float), _in_range(0.1, 10.0), "0.1-10.0 seconds"),
    },
    "mqtt": {
        "enabled": Rule((bool,), describe="true or false"),
        "broker_host": Rule(
            (str,), lambda v: 0 < len(v) <= 255, "a hostname of 1-255 characters"
        ),
        # A TCP port, not a device path — the old flat map called this "port"
        # for MQTT and for serial and applied the same (absent) rule to both.
        "broker_port": Rule((int,), _in_range(1, 65535), "1-65535"),
        "topic": Rule(
            (str,),
            lambda v: 0 < len(v) <= 255 and "#" not in v and "+" not in v,
            "a topic of 1-255 characters with no wildcards",
        ),
        "secret": Rule((str,), lambda v: len(v) <= 512, "at most 512 characters"),
        "tls": Rule((bool,), describe="true or false"),
        "username": Rule((str,), lambda v: len(v) <= 255, "at most 255 characters"),
        "password": Rule((str,), lambda v: len(v) <= 512, "at most 512 characters"),
    },
    "serial": {
        "enabled": Rule((bool,), describe="true or false"),
        "port": Rule((str,), _is_dev_path, "a path under /dev/"),
        "baudrate": Rule((int,), _in_range(1200, 1_000_000), "1200-1000000"),
    },
    "file_watch": {
        "enabled": Rule((bool,), describe="true or false"),
        "watch_dir": Rule(
            (str,), _is_safe_watch_dir, "an absolute directory outside /proc, /sys and /dev"
        ),
        "poll_interval": Rule((int, float), _in_range(0.5, 60.0), "0.5-60.0 seconds"),
    },
}

# ── Restricted mode settings ───────────────────────────────────────────────
#
# Kept out of TOP_LEVEL_RULES on purpose. Those keys are writable through the
# generic ``set_setting`` RPC, and what arms the lock must not be: a switch
# that turns the lock off is not a setting, it is the thing the lock exists to
# protect. These are reached only through the dedicated restricted RPCs, which
# refuse while locked.
#
# There is no ``locked`` key here. Whether the plugin is locked is derived from
# whether the key is present, so storing it would be a second answer to a
# question the media registry already answers — and the two would disagree the
# moment anything happened while the plugin was not running.

def _is_token_hash(value: str) -> bool:
    """A SHA-256 hex digest, or empty for "no key registered"."""
    return value == "" or bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


RESTRICTED_RULES: Dict[str, Rule] = {
    "key_hash": Rule((str,), _is_token_hash, "a SHA-256 hex digest"),
    "key_label": Rule(
        (str,), lambda v: len(v) <= 128, "at most 128 characters"
    ),
}

RESTRICTED_SETTING_KEYS = frozenset(RESTRICTED_RULES)

SOURCE_TYPES = frozenset(SOURCE_RULES)

# Kept so callers can still ask "is this an NFC key?" without knowing the
# shape of the table.
NFC_SETTING_KEYS = frozenset(SOURCE_RULES["nfc"]) - {"enabled"}
TOP_LEVEL_SETTING_KEYS = frozenset(TOP_LEVEL_RULES)


def rule_for(key: str, source_type: Optional[str] = None) -> Optional[Rule]:
    """The rule governing ``key``, or None when there is no such setting.

    With no ``source_type`` this covers the top-level keys and, for backwards
    compatibility, the NFC ones — ``set_setting`` has always addressed the
    reader's settings by bare name.
    """
    if source_type is not None:
        return SOURCE_RULES.get(source_type, {}).get(key)
    if key in TOP_LEVEL_RULES:
        return TOP_LEVEL_RULES[key]
    if key in NFC_SETTING_KEYS:
        return SOURCE_RULES["nfc"][key]
    return None


def validate(key: str, value: Any, source_type: Optional[str] = None) -> Tuple[bool, str]:
    """Check one setting. Returns ``(ok, reason)``; reason is "" when ok.

    The reason is worth returning rather than logging here: these come from
    the frontend, and "poll_interval must be 0.5-60.0 seconds" is something
    the panel can show, where a log line on the device is not.
    """
    rule = rule_for(key, source_type)
    if rule is None:
        where = f"{source_type}." if source_type else ""
        return False, f"unknown setting {where}{key}"
    return _check(rule, key, value)


def validate_restricted(key: str, value: Any) -> Tuple[bool, str]:
    """Check one restricted setting. Same contract as :func:`validate`.

    A separate entry point rather than a ``section`` argument to ``validate``:
    these keys are addressed only by the restricted RPCs, and letting them resolve
    through the same lookup that ``set_setting`` uses is precisely the drift
    this module exists to prevent.
    """
    rule = RESTRICTED_RULES.get(key)
    if rule is None:
        return False, f"unknown setting restricted.{key}"
    return _check(rule, key, value)


def _check(rule: Rule, key: str, value: Any) -> Tuple[bool, str]:
    """Apply one rule to one value."""
    # bool is a subclass of int, so an int-typed setting would otherwise
    # silently accept True. That matters for baudrate and broker_port.
    if bool not in rule.types and isinstance(value, bool):
        return False, f"{key} must be {rule.describe or 'a number'}"

    if not isinstance(value, rule.types):
        return False, f"{key} must be {rule.describe or _type_names(rule.types)}"

    if rule.check is not None:
        try:
            if not rule.check(value):
                return False, f"{key} must be {rule.describe}"
        except Exception:
            return False, f"{key} must be {rule.describe}"

    return True, ""


def coerce(key: str, value: Any, source_type: Optional[str] = None) -> Any:
    """Normalise an accepted value to the type the rest of the code expects.

    Only one case so far: a rule that takes ``(int, float)`` gets a float, so
    a JSON ``2`` and a JSON ``2.0`` cannot behave differently downstream.
    """
    rule = rule_for(key, source_type)
    if rule is not None and rule.types == (int, float) and isinstance(value, int):
        return float(value)
    return value


def _type_names(types: Tuple[type, ...]) -> str:
    return " or ".join(t.__name__ for t in types)
