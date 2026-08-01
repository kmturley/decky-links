"""Storage media source — USB, SD card, and optical disc support.

Uses pyudev to listen for Linux kernel udev events on block devices.
When a block device is added, we look for (or create) a mount point,
read ``decky-links.json`` from the filesystem root, and emit a LOAD event.
When the device is removed we emit an UNLOAD event and clean up any
temporary mounts we created.

Gracefully degrades on non-Linux platforms (macOS, Windows) where pyudev
is not available — ``start()`` returns False and the source stays inactive.
"""

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
            self._scan_existing_devices()
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
            self._unmount_device(mountpoint)
        self._our_mounts.clear()
        self._active_media.clear()
        self._pending.clear()

    def is_active(self) -> bool:
        return self._monitor is not None

    def has_media(self) -> bool:
        return len(self._active_media) > 0

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Drain one pending event, then check for new udev events."""
        if self._pending:
            return self._pending.popleft()

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

        if action == "add":
            # A drive can appear with no disk in it (USB floppy, card reader).
            # Mounting that just fails noisily; wait for the media-change event.
            if not has_media:
                if self._logger:
                    self._logger.info(
                        f"StorageSource: {devnode} added without media — "
                        f"waiting for a disk"
                    )
                return None
            return self._handle_device_added(devnode)

        if action == "remove":
            return self._handle_device_removed(devnode)

        if action == "change":
            # Inserting or ejecting a disk in an already-connected drive emits
            # 'change', not 'add'/'remove'. Without this branch a floppy drive
            # left plugged in never reports anything at all.
            if has_media and devnode not in self._active_media:
                return self._handle_device_added(devnode)
            if not has_media and devnode in self._active_media:
                return self._handle_device_removed(devnode)
            return None

        return None

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

    def _handle_device_added(self, devnode: str) -> Optional[MediaEvent]:
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
            mountpoint = self._mount_device(devnode)
            if mountpoint:
                mounted_by_us = True
                self._our_mounts[devnode] = mountpoint

        if not mountpoint:
            if self._logger:
                self._logger.warning(
                    f"StorageSource: {devnode} has media but could not be mounted "
                    f"— check the filesystem is readable and the plugin runs as root"
                )
            return None

        payload_path = os.path.join(mountpoint, PAYLOAD_FILENAME)
        payload = self._read_payload(payload_path)
        if payload is None:
            if self._logger:
                # Silent rejection here is the most likely reason a working disk
                # appears to do nothing, so name the exact file we wanted.
                self._logger.info(
                    f"StorageSource: {devnode} mounted at {mountpoint} but no valid "
                    f"{PAYLOAD_FILENAME} at its root — ignoring. Expected JSON with "
                    f'{{"version": 1, "uri": "steam://rungameid/..."}}'
                )
            if mounted_by_us:
                self._unmount_device(mountpoint)
                self._our_mounts.pop(devnode, None)
            return None

        uri = payload.get("uri", "")
        self._active_media[devnode] = uri
        if self._logger:
            self._logger.info(f"StorageSource: loaded {devnode} uri={uri}")

        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id=self.source_id,
            media_id=devnode,
            uri=uri,
            payload={k: v for k, v in payload.items() if k != "uri"},
        )

    def _handle_device_removed(self, devnode: str) -> Optional[MediaEvent]:
        """Emit UNLOAD event and clean up any mount we created."""
        uri = self._active_media.pop(devnode, None)
        if uri is None:
            return None  # Never saw a LOAD for this device — ignore

        mountpoint = self._our_mounts.pop(devnode, None)
        if mountpoint:
            self._unmount_device(mountpoint)

        if self._logger:
            self._logger.info(f"StorageSource: removed {devnode}")

        return MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.STORAGE,
            source_id=self.source_id,
            media_id=devnode,
            uri=uri,
        )

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

    def _mount_device(self, devnode: str) -> Optional[str]:
        """Mount devnode read-only to a temp directory. Returns mountpoint or None."""
        tmpdir = tempfile.mkdtemp(prefix="decky-links-")
        try:
            result = subprocess.run(
                ["mount", "-o", "ro", devnode, tmpdir],
                capture_output=True,
                timeout=10,
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

    def _unmount_device(self, mountpoint: str) -> None:
        """Unmount mountpoint and remove the temp directory."""
        try:
            subprocess.run(["umount", mountpoint], capture_output=True, timeout=10)
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

    def _scan_existing_devices(self) -> None:
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
            if self._logger:
                self._logger.info(
                    f"StorageSource: found existing media {devnode} uri={uri}"
                )
            self._pending.append(MediaEvent(
                kind=MediaEventKind.LOAD,
                source_type=SourceType.STORAGE,
                source_id=self.source_id,
                media_id=devnode,
                uri=uri,
                payload={k: v for k, v in payload.items() if k != "uri"},
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
                # Filter here as well as in _handle_device_added so the Deck's
                # internal partitions don't each log a refusal on every start.
                if not self._is_removable(devnode):
                    continue
                if not self._has_media(devnode):
                    continue
                event = self._handle_device_added(devnode)
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
