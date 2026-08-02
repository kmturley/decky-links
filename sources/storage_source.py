"""Storage media source — USB, SD card, and optical disc support.

Uses pyudev to listen for Linux kernel udev events on block devices.
When a block device is added, we look for (or create) a mount point,
read ``decky-links.json`` from the filesystem root, and emit a LOAD event.
When the device is removed we emit an UNLOAD event and clean up any
temporary mounts we created.

Gracefully degrades on non-Linux platforms (macOS, Windows) where pyudev
is not available — ``start()`` returns False and the source stays inactive.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import traceback
from collections import deque
from typing import Any, Dict, Optional

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceType,
)

PAYLOAD_FILENAME = "decky-links.json"

# A floppy drive seeks for several seconds before it admits defeat on an
# unformatted disk, so this has to be generous. Every call using it runs in a
# worker thread — blocking the event loop for this long stalls the whole plugin.
MOUNT_TIMEOUT_SECONDS = 20

# Temp mountpoints we create. Also the marker used to identify — and reap —
# mounts stranded by a previous plugin process.
_MOUNT_PREFIX = "/tmp/decky-links-"

class DriveKind:
    """Categories of removable drive, as udev distinguishes them.

    Deliberately not an Enum: these values cross the RPC boundary into settings
    JSON and the frontend, where plain strings are what everything else uses.
    """
    FLOPPY = "floppy"
    OPTICAL = "optical"
    FLASH = "flash"
    USB = "usb"


# Off by default unless the drive exists to be a trigger. A floppy drive on a
# Steam Deck is there on purpose; optical, USB and card readers are general
# storage holding the user's own data, and mounting those uninvited is both a
# surprise and a delay. Every category is one toggle away in the panel.
DEFAULT_DRIVE_KINDS = {
    DriveKind.FLOPPY: True,
    DriveKind.OPTICAL: False,
    DriveKind.USB: False,
    DriveKind.FLASH: False,
}

# Device node prefixes we consider mountable storage
_DEVICE_PREFIXES = (
    "/dev/fd",       # floppy
    "/dev/sd",       # SATA / USB mass storage
    "/dev/sr",       # optical
    "/dev/mmcblk",   # SD / eMMC
    "/dev/nvme",     # NVMe
)


class StorageSource(MediaSource):
    """Block device media source.

    Monitors udev events for block device arrivals/departures and
    exposes ``decky-links.json`` payloads as MediaEvents.
    """

    source_type = SourceType.STORAGE

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._monitor = None           # pyudev.Monitor, set on successful start()
        self._context = None           # pyudev.Context
        self._pending: deque = deque() # buffered events (startup scan)
        self._our_mounts: Dict[str, str] = {}  # devnode → tmpdir we created
        self._active_media: Dict[str, str] = {}  # devnode → URI (needed for UNLOAD)
        self._drives: Dict[str, str] = {}  # devnode → DriveKind, disk or no disk
        self._unmountable: set = set()   # media that already failed to mount
        self._deferred_mounts: deque = deque()  # announced loading, not yet mounted
        self._loading: set = set()       # devnodes currently being mounted

    @property
    def source_id(self) -> str:
        return "storage:udev"

    @property
    def poll_interval(self) -> float:
        return 1.0

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Start udev monitoring for block devices."""
        try:
            import pyudev
        except ImportError:
            if self._logger:
                self._logger.warning(
                    "StorageSource: pyudev not available — storage source disabled"
                )
            return False

        try:
            self._context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(self._context)
            monitor.filter_by(subsystem="block")
            monitor.start()
            self._monitor = monitor
            if self._logger:
                self._logger.info("StorageSource: udev monitor started")
            await self._reap_stale_mounts()
            await self._scan_existing_devices()
            return True
        except Exception as e:
            if self._logger:
                self._logger.error(f"StorageSource: failed to start: {e}")
                self._logger.error(traceback.format_exc())
            self._monitor = None
            self._context = None
            return False

    async def stop(self) -> None:
        """Stop monitoring and clean up any temporary mounts."""
        self._monitor = None
        self._context = None
        for _devnode, mountpoint in list(self._our_mounts.items()):
            await self._unmount_device(mountpoint)
        self._our_mounts.clear()
        self._active_media.clear()
        self._drives.clear()
        self._unmountable.clear()
        self._pending.clear()
        self._deferred_mounts.clear()
        self._loading.clear()

    def is_active(self) -> bool:
        return self._monitor is not None

    def has_media(self) -> bool:
        return len(self._active_media) > 0

    def drive_kinds_present(self) -> Dict[str, bool]:
        """Which categories of drive are currently attached.

        Reported for every category, enabled or not: a user who has switched
        USB storage off should still be able to see that a stick is plugged in,
        or the toggle is undiscoverable.
        """
        present = {kind: False for kind in DEFAULT_DRIVE_KINDS}
        for kind in self._drives.values():
            present[kind] = True
        return present

    def has_drive(self) -> bool:
        """True when a storage drive is connected, disk or no disk.

        The panel's source row tracks this rather than has_media: ejecting a
        floppy does not unplug the drive, and showing the whole source as
        inactive the moment a disk comes out reads as a fault.
        """
        return len(self._drives) > 0

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Drain one pending event, then check for new udev events."""
        if self._pending:
            return self._pending.popleft()

        # A disk announced as loading on the previous poll. Mounting it here
        # rather than in the poll that noticed it is what lets the panel show a
        # loading state instead of "No disk" for the length of the mount.
        if self._deferred_mounts:
            devnode = self._deferred_mounts.popleft()
            try:
                return await self._handle_device_added(devnode)
            finally:
                self._loading.discard(devnode)

        if not self._monitor:
            return None

        try:
            device = self._monitor.poll(timeout=0)
        except Exception as e:
            if self._logger:
                self._logger.error(f"StorageSource: poll error: {e}")
            self._monitor = None
            return None

        if device is None:
            return None

        devnode = device.device_node
        if not devnode or not self._is_relevant_device(devnode):
            return None

        action = device.action
        has_media = self._has_media(devnode)
        if self._logger:
            self._logger.info(
                f"StorageSource: udev event action={action} devnode={devnode} "
                f"media={has_media}"
            )

        # Media that has already failed to mount must not be retried on every
        # subsequent event. An unformatted floppy takes ~20s to fail, so
        # retrying turns the drive into a permanent stall. The disk is only
        # reconsidered once it has physically left the drive.
        if not has_media:
            self._unmountable.discard(devnode)
        elif devnode in self._unmountable:
            return None

        if action == "add":
            self._note_drive(devnode)
            # A drive can appear with no disk in it (USB floppy, card reader).
            # Mounting that just fails noisily; wait for the media-change event.
            if not has_media:
                if self._logger:
                    self._logger.info(
                        f"StorageSource: {devnode} added without media — "
                        f"waiting for a disk"
                    )
                return None
            return await self._begin_load(devnode)

        if action == "remove":
            self._drives.pop(devnode, None)
            self._unmountable.discard(devnode)
            self._loading.discard(devnode)
            return await self._handle_device_removed(devnode)

        if action == "change":
            # Inserting or ejecting a disk in an already-connected drive emits
            # 'change', not 'add'/'remove'. Without this branch a floppy drive
            # left plugged in never reports anything at all.
            self._note_drive(devnode)
            if has_media and devnode not in self._active_media:
                return await self._begin_load(devnode)
            if not has_media and devnode in self._active_media:
                self._loading.discard(devnode)
                return await self._handle_device_removed(devnode)
            return None

        return None

    async def _begin_load(self, devnode: str) -> Optional[PluginEvent]:
        """Report a disk as loading, then mount it on the following poll.

        Mounting a floppy takes anywhere up to the timeout — a minute is normal
        for a tired drive and a dusty disk. Doing it inside the same poll that
        noticed the disk means nothing reaches the panel until it finishes, so
        the row reads "No disk" for the whole wait and looks broken.

        Splitting it costs one poll interval before the mount starts, which is
        nothing against the mount itself, and needs no background task: the
        deferred devnode is picked up at the top of the next poll.
        """
        if devnode in self._loading:
            return None
        if self._find_mount_point(devnode):
            # Already mounted by the system — reading the payload is a file
            # read, far too quick to be worth announcing.
            return await self._handle_device_added(devnode)

        # Only announce work we are actually going to do. A drive we would
        # refuse to mount must not be left saying "Loading" forever.
        if not self._is_removable(devnode):
            return None
        if not self._drive_kind_enabled(self.classify_drive(devnode)):
            return None

        self._loading.add(devnode)
        self._deferred_mounts.append(devnode)
        if self._logger:
            self._logger.info(f"StorageSource: {devnode} has media — mounting")
        return MediaEvent(
            kind=MediaEventKind.LOADING,
            source_type=SourceType.STORAGE,
            source_id=self.source_id,
            media_id=devnode,
            uri="",
            payload={
                "drive_kind": self._drives.get(devnode) or self.classify_drive(devnode),
            },
        )

    def _note_drive(self, devnode: str) -> None:
        """Record a connected drive and its category.

        Gated on removability so the Deck's internal NVMe partitions — which
        emit `change` events of their own — never appear as attached drives.
        """
        if devnode in self._drives:
            return
        if not self._is_removable(devnode):
            return
        self._drives[devnode] = self.classify_drive(devnode)

    def _has_media(self, devnode: str) -> bool:
        """True when the drive actually holds media.

        Reads the block device's size in 512-byte sectors from sysfs; an empty
        drive reports 0. This is how a floppy or card reader with no disk is
        distinguished from one with a disk in it.
        """
        name = os.path.basename(devnode)
        try:
            with open(f"/sys/class/block/{name}/size", "r") as f:
                return int(f.read().strip()) > 0
        except (OSError, ValueError):
            # Unknown — assume media is present and let the mount decide.
            return True

    def _udev_properties(self, devnode: str) -> Dict[str, str]:
        """udev properties for a block device, or {} if unavailable."""
        if not self._context:
            return {}
        try:
            import pyudev
            device = pyudev.Devices.from_device_file(self._context, devnode)
            return dict(device.properties)
        except Exception:
            return {}

    def classify_drive(self, devnode: str) -> str:
        """Which kind of drive this is, as a DriveKind value.

        A floppy drive and a USB thumb drive are the same thing to
        ``removable=1``, but they are not the same thing to a user: one holds
        collectible media they want to trigger games with, the other usually
        holds their own data and should be left alone. udev already knows the
        difference — a USB floppy reports ``ID_TYPE=floppy`` and
        ``ID_DRIVE_FLOPPY=1``.

        Anything unrecognised falls back to USB storage, which is off by
        default, so a misclassification errs towards leaving the disk alone.
        """
        props = self._udev_properties(devnode)

        if props.get("ID_DRIVE_FLOPPY") == "1" or props.get("ID_TYPE") == "floppy":
            return DriveKind.FLOPPY
        if props.get("ID_CDROM") == "1" or props.get("ID_TYPE") == "cd":
            return DriveKind.OPTICAL
        if any(k.startswith("ID_DRIVE_FLASH") or k.startswith("ID_DRIVE_MEDIA_FLASH")
               for k in props):
            return DriveKind.FLASH
        return DriveKind.USB

    def _drive_kind_enabled(self, kind: str) -> bool:
        """Whether the user has switched this category of drive on."""
        kinds = self._settings.get("drive_kinds")
        if not isinstance(kinds, dict):
            kinds = DEFAULT_DRIVE_KINDS
        return bool(kinds.get(kind, DEFAULT_DRIVE_KINDS.get(kind, False)))

    def _is_removable(self, devnode: str) -> bool:
        """True when devnode belongs to a removable drive.

        Guards every mount we perform ourselves. A Steam Deck's internal NVMe
        carries several unmounted system partitions (rootfs B, var B, the EFI
        partitions); without this the startup scan would mount each of them in
        turn looking for a payload. Unknown devices are treated as fixed.
        """
        name = os.path.basename(devnode)
        try:
            syspath = os.path.realpath(f"/sys/class/block/{name}")
            # A partition has no `removable` of its own — it inherits the disk's.
            if os.path.exists(os.path.join(syspath, "partition")):
                syspath = os.path.dirname(syspath)
            with open(os.path.join(syspath, "removable"), "r") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    # ── Device handling ────────────────────────────────────────────────

    def _is_relevant_device(self, devnode: str) -> bool:
        return any(devnode.startswith(p) for p in _DEVICE_PREFIXES)

    def _load_event(self, devnode: str, uri: str, **payload) -> MediaEvent:
        """Build a LOAD event for a disk.

        Every LOAD goes through here so the fields the panel depends on cannot
        be omitted by one call site. `rearm()` used to build its own event and
        left out `drive_kind`; the panel matches a medium to its row by drive
        category, so a freshly paired disk was orphaned from its own row until
        it was ejected and reinserted. The startup scan had the same gap.
        """
        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id=self.source_id,
            media_id=devnode,
            uri=uri,
            payload={
                **payload,
                "drive_kind": self._drives.get(devnode) or self.classify_drive(devnode),
            },
        )

    async def _handle_device_added(self, devnode: str) -> Optional[MediaEvent]:
        """Find or create a mount, read payload, return LOAD event or None."""
        mountpoint = self._find_mount_point(devnode)
        mounted_by_us = False

        if not mountpoint:
            if not self._is_removable(devnode):
                if self._logger:
                    self._logger.warning(
                        f"StorageSource: refusing to mount {devnode} — not a "
                        f"removable drive"
                    )
                return None
            kind = self.classify_drive(devnode)
            if not self._drive_kind_enabled(kind):
                if self._logger:
                    self._logger.info(
                        f"StorageSource: ignoring {devnode} — {kind} drives are "
                        f"switched off"
                    )
                return None
            mountpoint = await self._mount_device(devnode)
            if mountpoint:
                mounted_by_us = True
                self._our_mounts[devnode] = mountpoint

        if not mountpoint:
            self._unmountable.add(devnode)
            if self._logger:
                self._logger.warning(
                    f"StorageSource: {devnode} has media but could not be mounted. "
                    f"The most likely cause is an unformatted disk — format it with "
                    f"a FAT filesystem (mkfs.vfat). Not retrying until it is ejected."
                )
            # Still report it. A disk that is physically in the drive but says
            # nothing at all is indistinguishable from a broken plugin; the
            # panel needs to be able to say *why* nothing happened.
            self._active_media[devnode] = ""
            return self._load_event(
                devnode,
                "",
                unreadable=True,
                # Shown verbatim in a panel row, so it has to fit one. The full
                # diagnosis is in the log line above, where there is room.
                error="Unformatted disk",
            )

        payload_path = os.path.join(mountpoint, PAYLOAD_FILENAME)
        payload = self._read_payload(payload_path)
        if payload is None:
            # A disk with no payload is still a pairable medium — the floppy
            # equivalent of a blank NFC tag. Report it as present-but-blank so
            # the panel can offer to write one, and keep the mount so pairing
            # has somewhere to write to.
            if self._logger:
                self._logger.info(
                    f"StorageSource: {devnode} mounted at {mountpoint} with no valid "
                    f"{PAYLOAD_FILENAME} — reporting as blank media, ready to pair"
                )
            self._active_media[devnode] = ""
            return self._load_event(devnode, "", blank=True, mountpoint=mountpoint)

        uri = payload.get("uri", "")
        self._active_media[devnode] = uri
        if self._logger:
            self._logger.info(f"StorageSource: loaded {devnode} uri={uri}")

        return self._load_event(
            devnode, uri, **{k: v for k, v in payload.items() if k != "uri"}
        )

    async def _handle_device_removed(self, devnode: str) -> Optional[MediaEvent]:
        """Emit UNLOAD event and clean up any mount we created."""
        uri = self._active_media.pop(devnode, None)

        # Release the mount before deciding whether there is an event to emit.
        # Returning early on "no active media" used to strand the mount: eject
        # the disk (which clears _active_media), then unplug the drive, and the
        # filesystem stayed mounted forever. The kernel will not reuse a device
        # node that is still busy, so the next drive plugged in comes up as
        # /dev/sdb, then /dev/sdc, drifting further each time.
        mountpoint = self._our_mounts.pop(devnode, None)
        if mountpoint:
            await self._unmount_device(mountpoint)

        if uri is None:
            return None  # Never saw a LOAD for this device — nothing to report

        if self._logger:
            self._logger.info(f"StorageSource: removed {devnode}")

        return MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.STORAGE,
            source_id=self.source_id,
            media_id=devnode,
            uri=uri,
        )

    # ── Pairing ────────────────────────────────────────────────────────

    def can_write(self) -> bool:
        return True

    def rearm(self) -> None:
        """Re-queue a LOAD for every disk currently in a drive.

        udev fires once, on insertion. Without this, pressing "Pair" with a
        disk already in the drive would wait for an event that never comes —
        the user would have to eject and reinsert to pair.

        The payload must carry ``drive_kind`` like every other LOAD: the panel
        matches a medium to its row by drive category, so a rearm that omitted
        it left the freshly-paired disk unable to find its own row. It showed
        "No disk" until the disk was ejected and reinserted.
        """
        for devnode, uri in list(self._active_media.items()):
            self._pending.append(
                self._load_event(devnode, uri, blank=not uri, rearmed=True)
            )

    async def write_uri(self, media_id: str, uri: str, title: str = "", icon: str = ""):
        """Write ``decky-links.json`` to the disk's filesystem root.

        Disks are mounted read-only so that a disk sitting in a drive is never
        at risk from a crash or a sudden eject; pairing briefly remounts
        read-write and puts it back afterwards regardless of outcome.
        """
        devnode = media_id
        mountpoint = self._our_mounts.get(devnode) or self._find_mount_point(devnode)
        if not mountpoint:
            return False, f"{devnode} is not mounted"

        if not await self._remount(mountpoint, "rw"):
            return False, f"could not remount {mountpoint} read-write"

        try:
            path = os.path.join(mountpoint, PAYLOAD_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": 1, "uri": uri, "title": title, "icon": icon},
                    f,
                    indent=2,
                )
                f.flush()
                # Floppies are slow and users eject the moment the UI says done.
                os.fsync(f.fileno())
        except OSError as e:
            if self._logger:
                self._logger.error(f"StorageSource: failed writing {devnode}: {e}")
            return False, str(e)
        finally:
            await self._remount(mountpoint, "ro")

        self._active_media[devnode] = uri
        if self._logger:
            self._logger.info(f"StorageSource: wrote uri={uri} to {devnode}")
        return True, None

    async def _reap_stale_mounts(self) -> None:
        """Unmount temp mounts left behind by a previous plugin process.

        A restart (or a crash, or `systemctl restart plugin_loader`) never runs
        ``stop()``, so every mount we held stays in the kernel's table with
        nothing tracking it. They pin their device nodes, which is how a floppy
        drive that was /dev/sda comes back as /dev/sdb. Only paths matching our
        own tempdir prefix are touched.
        """
        stale = []
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].startswith(_MOUNT_PREFIX):
                        stale.append((parts[0], parts[1]))
        except OSError:
            return

        for devnode, mountpoint in stale:
            if self._logger:
                self._logger.info(
                    f"StorageSource: reaping stale mount {devnode} at {mountpoint} "
                    f"left by a previous run"
                )
            await self._unmount_device(mountpoint)

    async def _remount(self, mountpoint: str, mode: str) -> bool:
        """Remount an existing mountpoint ``rw`` or ``ro`` in place."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["mount", "-o", f"remount,{mode}", mountpoint],
                capture_output=True,
                timeout=MOUNT_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                return True
            if self._logger:
                self._logger.warning(
                    f"StorageSource: remount,{mode} failed for {mountpoint}: "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
        except Exception as e:
            if self._logger:
                self._logger.error(
                    f"StorageSource: remount,{mode} error for {mountpoint}: {e}"
                )
        return False

    # ── Mount helpers ──────────────────────────────────────────────────

    def _find_mount_point(self, devnode: str) -> Optional[str]:
        """Return the existing mount point for devnode from /proc/mounts, or None."""
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == devnode:
                        return parts[1]
        except OSError:
            pass
        return None

    async def _mount_device(self, devnode: str) -> Optional[str]:
        """Mount devnode read-only to a temp directory. Returns mountpoint or None."""
        tmpdir = tempfile.mkdtemp(prefix=os.path.basename(_MOUNT_PREFIX),
                                  dir=os.path.dirname(_MOUNT_PREFIX))
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["mount", "-o", "ro", devnode, tmpdir],
                capture_output=True,
                timeout=MOUNT_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                return tmpdir
            if self._logger:
                self._logger.warning(
                    f"StorageSource: mount failed for {devnode}: "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
        except Exception as e:
            if self._logger:
                self._logger.error(f"StorageSource: mount error for {devnode}: {e}")
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass
        return None

    async def _unmount_device(self, mountpoint: str) -> None:
        """Unmount mountpoint and remove the temp directory."""
        try:
            await asyncio.to_thread(
                subprocess.run, ["umount", mountpoint],
                capture_output=True, timeout=MOUNT_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        try:
            os.rmdir(mountpoint)
        except Exception:
            pass

    # ── Payload ────────────────────────────────────────────────────────

    def _read_payload(self, path: str) -> Optional[Dict[str, Any]]:
        """Read and validate decky-links.json. Returns normalised dict or None."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(data, dict):
            return None
        if data.get("version") != 1:
            return None
        if not isinstance(data.get("uri"), str) or not data["uri"]:
            return None

        return {
            "version": data["version"],
            "uri": data["uri"],
            "title": data.get("title", ""),
            "icon": data.get("icon", ""),
        }

    # ── Startup scan ───────────────────────────────────────────────────

    async def _scan_existing_devices(self) -> None:
        """Buffer LOAD events for media already present when the plugin starts.

        Covers two cases: filesystems the system has already mounted, and — the
        common one in SteamOS game mode, which auto-mounts nothing — a disk
        sitting in a drive that we have to mount ourselves.
        """
        seen = set()

        # 1. Already-mounted filesystems.
        try:
            with open("/proc/mounts", "r") as f:
                mounts = f.readlines()
        except OSError:
            mounts = []

        for line in mounts:
            parts = line.split()
            if len(parts) < 2:
                continue
            devnode, mountpoint = parts[0], parts[1]
            if not self._is_relevant_device(devnode):
                continue
            seen.add(devnode)

            payload = self._read_payload(os.path.join(mountpoint, PAYLOAD_FILENAME))
            if payload is None:
                continue

            uri = payload.get("uri", "")
            self._active_media[devnode] = uri
            self._note_drive(devnode)
            if self._logger:
                self._logger.info(
                    f"StorageSource: found existing media {devnode} uri={uri}"
                )
            self._pending.append(self._load_event(
                devnode, uri, **{k: v for k, v in payload.items() if k != "uri"}
            ))

        # 2. Unmounted drives that currently hold media. Without this, a disk
        # already in the drive at startup stays invisible until it is ejected
        # and reinserted, because no udev event will ever fire for it.
        if not self._context:
            return
        try:
            for device in self._context.list_devices(subsystem="block"):
                devnode = device.device_node
                if not devnode or devnode in seen:
                    continue
                if not self._is_relevant_device(devnode):
                    continue
                # Record the drive itself before considering its media, so a
                # drive that starts up empty still shows the source as active.
                # `_drives` became a devnode→category dict when drive
                # categories landed and this call site kept using `.add()`,
                # which raised straight into the broad handler below — the
                # whole scan died at the first removable drive, so a drive
                # plugged in before the plugin started never appeared at all.
                self._note_drive(devnode)
                # Filter here as well as in _handle_device_added so the Deck's
                # internal partitions don't each log a refusal on every start.
                if not self._is_removable(devnode):
                    continue
                if not self._has_media(devnode):
                    continue
                event = await self._handle_device_added(devnode)
                if event is not None:
                    if self._logger:
                        self._logger.info(
                            f"StorageSource: media already inserted in {devnode}"
                        )
                    self._pending.append(event)
        except Exception as e:
            if self._logger:
                self._logger.warning(
                    f"StorageSource: startup device scan failed: {e}"
                )
