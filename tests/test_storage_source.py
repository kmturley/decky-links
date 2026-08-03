"""
test_storage_source.py — unit tests for StorageSource.

All hardware-level dependencies (pyudev, subprocess, /proc/mounts, file I/O)
are mocked so the suite runs on any platform.
"""
import asyncio
import json
import os
import sys
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    """A source with every drive category switched on.

    Most tests here exercise mount mechanics, not categorisation, and would
    otherwise be silently skipped by the default (USB storage off). Tests that
    care about the categories set them explicitly.
    """
    from sources.storage_source import StorageSource, DriveKind
    if settings is None:
        settings = {"drive_kinds": {k: True for k in (
            DriveKind.FLOPPY, DriveKind.OPTICAL, DriveKind.USB, DriveKind.FLASH)}}
    return StorageSource(settings, logger=MagicMock())


def _make_udev_device(action: str, devnode: str):
    d = MagicMock()
    d.action = action
    d.device_node = devnode
    return d


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:

    @pytest.mark.asyncio
    async def test_start_returns_false_when_pyudev_missing(self):
        src = _make_source()
        with patch.dict(sys.modules, {"pyudev": None}):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_true_and_is_active(self):
        src = _make_source()
        mock_pyudev = MagicMock()
        mock_pyudev.Monitor.from_netlink.return_value = MagicMock()
        with patch.dict(sys.modules, {"pyudev": mock_pyudev}):
            with patch.object(src, "_scan_existing_devices"):
                ok = await src.start()
        assert ok is True
        assert src.is_active()

    @pytest.mark.asyncio
    async def test_start_calls_scan_existing_devices(self):
        src = _make_source()
        mock_pyudev = MagicMock()
        mock_pyudev.Monitor.from_netlink.return_value = MagicMock()
        with patch.dict(sys.modules, {"pyudev": mock_pyudev}):
            with patch.object(src, "_scan_existing_devices") as mock_scan:
                await src.start()
        mock_scan.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_returns_false_on_pyudev_exception(self):
        src = _make_source()
        mock_pyudev = MagicMock()
        mock_pyudev.Context.side_effect = RuntimeError("no permission")
        with patch.dict(sys.modules, {"pyudev": mock_pyudev}):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()


# ── stop() ────────────────────────────────────────────────────────────────────

class TestStop:

    @pytest.mark.asyncio
    async def test_stop_clears_monitor(self):
        src = _make_source()
        src._monitor = MagicMock()
        await src.stop()
        assert src._monitor is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_stop_unmounts_our_mounts(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._our_mounts["/dev/sdb1"] = "/tmp/decky-links-abc"
        with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
            await src.stop()
        mock_umount.assert_called_once_with("/tmp/decky-links-abc")
        assert src._our_mounts == {}

    @pytest.mark.asyncio
    async def test_stop_clears_active_media_and_pending(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        src._pending.append("dummy_event")
        await src.stop()
        assert src._active_media == {}
        assert len(src._pending) == 0


# ── poll() ────────────────────────────────────────────────────────────────────

class TestPoll:

    @pytest.mark.asyncio
    async def test_poll_drains_pending_before_udev(self):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        src = _make_source()
        src._monitor = MagicMock()
        evt = MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
            media_id="/dev/sdb1",
            uri="steam://run/123",
        )
        src._pending.append(evt)
        result = await src.poll()
        assert result is evt
        assert len(src._pending) == 0
        src._monitor.poll.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_inactive(self):
        src = _make_source()
        assert src._monitor is None
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_returns_none_on_no_udev_event(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = None
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_skips_irrelevant_device(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = _make_udev_device("add", "/dev/loop0")
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_dispatches_add_event(self):
        """An `add` carrying media is announced as loading and mounted on the
        following poll, so it takes two polls to reach _handle_device_added."""
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdb1")
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_handle_device_added", AsyncMock(return_value=None)) as mock_add:
            await src.poll()
            mock_add.assert_not_called()
            src._monitor.poll.return_value = None
            await src.poll()
        mock_add.assert_called_once_with("/dev/sdb1")

    @pytest.mark.asyncio
    async def test_poll_dispatches_remove_event(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = _make_udev_device("remove", "/dev/sdb1")
        with patch.object(src, "_handle_device_removed", AsyncMock(return_value=None)) as mock_rem:
            await src.poll()
        mock_rem.assert_called_once_with("/dev/sdb1")

    @pytest.mark.asyncio
    async def test_poll_clears_monitor_on_exception(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.side_effect = RuntimeError("udev gone")
        result = await src.poll()
        assert result is None
        assert src._monitor is None


# ── _read_payload() ───────────────────────────────────────────────────────────

class TestReadPayload:

    def test_valid_payload_returns_dict(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({
            "version": 1,
            "uri": "steam://run/12345",
            "title": "My Game",
            "icon": "icon.png",
        }))
        result = src._read_payload(str(p))
        assert result == {
            "version": 1,
            "uri": "steam://run/12345",
            "title": "My Game",
            "icon": "icon.png",
        }

    def test_missing_file_returns_none(self, tmp_path):
        src = _make_source()
        result = src._read_payload(str(tmp_path / "missing.json"))
        assert result is None

    def test_invalid_json_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text("not valid json {{{")
        assert src._read_payload(str(p)) is None

    def test_non_dict_json_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps([1, 2, 3]))
        assert src._read_payload(str(p)) is None

    def test_wrong_version_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({"version": 2, "uri": "steam://run/1"}))
        assert src._read_payload(str(p)) is None

    def test_missing_uri_key_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({"version": 1}))
        assert src._read_payload(str(p)) is None

    def test_empty_uri_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({"version": 1, "uri": ""}))
        assert src._read_payload(str(p)) is None

    def test_optional_fields_default_to_empty_string(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({"version": 1, "uri": "https://example.com"}))
        result = src._read_payload(str(p))
        assert result is not None
        assert result["title"] == ""
        assert result["icon"] == ""

    def test_extra_fields_are_ignored(self, tmp_path):
        src = _make_source()
        p = tmp_path / "decky-links.json"
        p.write_text(json.dumps({
            "version": 1, "uri": "steam://run/1", "extra": "ignored",
        }))
        result = src._read_payload(str(p))
        assert result is not None
        assert "extra" not in result


# ── _handle_device_added() ────────────────────────────────────────────────────

class TestHandleDeviceAdded:

    @pytest.mark.asyncio
    async def test_existing_mount_with_payload_emits_load(self, tmp_path):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/12345", "title": "Game", "icon": "",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = await src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.source_type == SourceType.STORAGE
        assert event.uri == "steam://run/12345"
        assert event.media_id == "/dev/sdb1"
        assert src._active_media["/dev/sdb1"] == "steam://run/12345"

    @pytest.mark.asyncio
    async def test_existing_mount_without_payload_reports_blank_media(self, tmp_path):
        """A disk with no payload is pairable, not uninteresting.

        It is the floppy equivalent of a blank NFC tag: the panel needs to know
        it is there so it can offer to write one.
        """
        from sources.base import MediaEventKind
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = await src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.uri == ""
        assert event.payload["blank"] is True
        assert event.payload["mountpoint"] == str(tmp_path)
        assert src._active_media["/dev/sdb1"] == ""

    @pytest.mark.asyncio
    async def test_no_mount_then_mounts_and_emits_load(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/999",
        }))
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_is_removable", return_value=True):
                with patch.object(src, "_mount_device", AsyncMock(return_value=str(tmp_path))):
                    event = await src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.uri == "steam://run/999"
        assert src._our_mounts["/dev/sdb1"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_failed_mount_reports_unreadable_media(self):
        """A disk that is physically in the drive but says nothing at all is
        indistinguishable from a broken plugin. Report it so the panel can
        explain why nothing happened."""
        from sources.base import MediaEventKind
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_is_removable", return_value=True):
                with patch.object(src, "_mount_device", AsyncMock(return_value=None)):
                    event = await src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.uri == ""
        assert event.payload["unreadable"] is True
        # Shown verbatim in a panel row, which is one line of a narrow panel —
        # the full "format it as FAT" diagnosis goes to the log instead.
        assert event.payload["error"] == "Unformatted disk"
        assert len(event.payload["error"]) <= 24
        assert event.payload.get("blank") is None, "unreadable is not the same as blank"

    @pytest.mark.asyncio
    async def test_fixed_disk_is_never_mounted(self):
        """The Deck's internal NVMe holds unmounted system partitions; mounting
        them while hunting for a payload would be both useless and risky."""
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_is_removable", return_value=False):
                with patch.object(src, "_mount_device", new_callable=AsyncMock) as mock_mount:
                    event = await src._handle_device_added("/dev/nvme0n1p5")
        assert event is None
        mock_mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_our_mount_kept_when_no_payload(self, tmp_path):
        """Pairing needs somewhere to write, so a blank disk stays mounted."""
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_is_removable", return_value=True):
                with patch.object(src, "_mount_device", AsyncMock(return_value=str(tmp_path))):
                    with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
                        event = await src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.payload["blank"] is True
        mock_umount.assert_not_called()
        assert src._our_mounts["/dev/sdb1"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_uri_excluded_from_event_payload(self, tmp_path):
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/1", "title": "T", "icon": "i.png",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = await src._handle_device_added("/dev/sdb1")
        assert "uri" not in event.payload
        assert event.payload["title"] == "T"
        assert event.payload["version"] == 1


# ── _handle_device_removed() ──────────────────────────────────────────────────

class TestHandleDeviceRemoved:

    @pytest.mark.asyncio
    async def test_known_device_emits_unload(self):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        event = await src._handle_device_removed("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.UNLOAD
        assert event.source_type == SourceType.STORAGE
        assert event.uri == "steam://run/12345"
        assert event.media_id == "/dev/sdb1"
        assert "/dev/sdb1" not in src._active_media

    @pytest.mark.asyncio
    async def test_unknown_device_returns_none(self):
        src = _make_source()
        event = await src._handle_device_removed("/dev/sdb1")
        assert event is None

    @pytest.mark.asyncio
    async def test_our_mount_is_unmounted_on_removal(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/1"
        src._our_mounts["/dev/sdb1"] = "/tmp/decky-links-xyz"
        with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
            await src._handle_device_removed("/dev/sdb1")
        mock_umount.assert_called_once_with("/tmp/decky-links-xyz")
        assert "/dev/sdb1" not in src._our_mounts

    @pytest.mark.asyncio
    async def test_externally_mounted_device_no_unmount_called(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/1"
        # Not in _our_mounts — we didn't mount it
        with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
            await src._handle_device_removed("/dev/sdb1")
        mock_umount.assert_not_called()


# ── _find_mount_point() ───────────────────────────────────────────────────────

class TestFindMountPoint:

    def test_finds_existing_mount(self, tmp_path):
        src = _make_source()
        mounts = tmp_path / "mounts"
        mounts.write_text("/dev/sdb1 /mnt/usb vfat ro 0 0\n")
        with patch("builtins.open", mock_open(read_data="/dev/sdb1 /mnt/usb vfat ro 0 0\n")):
            result = src._find_mount_point("/dev/sdb1")
        assert result == "/mnt/usb"

    def test_returns_none_when_device_not_in_mounts(self):
        src = _make_source()
        with patch("builtins.open", mock_open(read_data="/dev/sda1 / ext4 rw 0 0\n")):
            result = src._find_mount_point("/dev/sdb1")
        assert result is None

    def test_returns_none_when_proc_mounts_missing(self):
        src = _make_source()
        with patch("builtins.open", side_effect=OSError("no such file")):
            result = src._find_mount_point("/dev/sdb1")
        assert result is None


# ── _mount_device() ───────────────────────────────────────────────────────────

def _blkid(fstype: str):
    """A blkid result reporting ``fstype``."""
    return MagicMock(returncode=0, stdout=fstype.encode())


def _mount_calls(mock_run):
    """Just the `mount` invocations, dropping the blkid probe."""
    return [c[0][0] for c in mock_run.call_args_list if c[0][0][0] == "mount"]


class TestMountDevice:

    @pytest.mark.asyncio
    async def test_successful_mount_returns_tmpdir(self):
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [_blkid("vfat"), MagicMock(returncode=0)]
            with patch("tempfile.mkdtemp", return_value="/tmp/decky-links-test"):
                result = await src._mount_device("/dev/sdb1")
        assert result == "/tmp/decky-links-test"
        args = _mount_calls(mock_run)[0]
        assert "/dev/sdb1" in args
        # The type is stated explicitly rather than left for mount to guess.
        assert args[args.index("-t") + 1] == "vfat"

    @pytest.mark.asyncio
    async def test_mount_is_hardened_against_hostile_media(self):
        """Untrusted media must not be able to carry a setuid binary or a
        device node into a mount performed by a root process."""
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [_blkid("vfat"), MagicMock(returncode=0)]
            with patch("tempfile.mkdtemp", return_value="/tmp/decky-links-test"):
                await src._mount_device("/dev/sdb1")
        args = _mount_calls(mock_run)[0]
        opts = args[args.index("-o") + 1].split(",")
        assert set(opts) >= {"ro", "nosuid", "nodev", "noexec"}

    @pytest.mark.asyncio
    async def test_refuses_filesystem_outside_the_allowlist(self):
        """Mounting runs the filesystem's kernel driver as root, so a type we
        did not choose is never handed to it."""
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [_blkid("ntfs")]
            result = await src._mount_device("/dev/sdb1")
        assert result is None
        assert _mount_calls(mock_run) == []
        assert "ntfs" in src._last_mount_error

    @pytest.mark.asyncio
    async def test_refuses_device_with_no_filesystem(self):
        """An unformatted disk has no superblock; blkid says so in milliseconds
        where the mount it replaces seeks for the full timeout."""
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=2, stdout=b"")]
            result = await src._mount_device("/dev/sdb1")
        assert result is None
        assert _mount_calls(mock_run) == []
        assert src._last_mount_error == "Unformatted disk"

    @pytest.mark.asyncio
    async def test_failed_mount_returns_none(self, tmp_path):
        src = _make_source()
        tmpdir = str(tmp_path / "mnt")
        os.makedirs(tmpdir)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _blkid("vfat"),
                MagicMock(returncode=32, stderr=b"permission denied"),
            ]
            with patch("tempfile.mkdtemp", return_value=tmpdir):
                result = await src._mount_device("/dev/sdb1")
        assert result is None

    @pytest.mark.asyncio
    async def test_mount_exception_returns_none(self, tmp_path):
        src = _make_source()
        tmpdir = str(tmp_path / "mnt")
        os.makedirs(tmpdir)
        with patch("subprocess.run", side_effect=Exception("timeout")):
            with patch("tempfile.mkdtemp", return_value=tmpdir):
                result = await src._mount_device("/dev/sdb1")
        assert result is None


# ── _remount() ────────────────────────────────────────────────────────────────

class TestRemount:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["rw", "ro"])
    async def test_remount_preserves_hardening_flags(self, mode):
        """A remount does not inherit mount options — repeating them is what
        stops pairing quietly clearing nosuid/nodev/noexec."""
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert await src._remount("/tmp/decky-links-test", mode) is True
        opts = mock_run.call_args[0][0][2].split(",")
        assert "remount" in opts
        assert mode in opts
        assert set(opts) >= {"nosuid", "nodev", "noexec"}
        # rw and ro are mutually exclusive; only the requested one is passed.
        assert ("rw" in opts) == (mode == "rw")


# ── _scan_existing_devices() ──────────────────────────────────────────────────

class TestScanExistingDevices:

    @pytest.mark.asyncio
    async def test_buffers_load_event_for_matching_mount(self):
        from sources.base import MediaEventKind
        src = _make_source()
        payload = {"version": 1, "uri": "steam://run/42", "title": "Game 42", "icon": ""}
        mounts_text = "/dev/sdb1 /mnt/usb vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", return_value=payload):
                await src._scan_existing_devices()
        assert len(src._pending) == 1
        evt = src._pending[0]
        assert evt.kind == MediaEventKind.LOAD
        assert evt.uri == "steam://run/42"
        assert evt.media_id == "/dev/sdb1"
        assert src._active_media["/dev/sdb1"] == "steam://run/42"

    @pytest.mark.asyncio
    async def test_ignores_non_storage_devices(self):
        src = _make_source()
        mounts_text = "tmpfs /run tmpfs rw 0 0\nsysfs /sys sysfs rw 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            await src._scan_existing_devices()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_ignores_device_without_payload(self):
        src = _make_source()
        mounts_text = "/dev/sdb1 /mnt/usb vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", return_value=None):
                await src._scan_existing_devices()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_handles_proc_mounts_missing_gracefully(self):
        src = _make_source()
        with patch("builtins.open", side_effect=OSError("no such file")):
            await src._scan_existing_devices()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_multiple_matching_mounts(self):
        from sources.base import MediaEventKind
        src = _make_source()
        payload1 = {"version": 1, "uri": "steam://run/1", "title": "", "icon": ""}
        payload2 = {"version": 1, "uri": "steam://run/2", "title": "", "icon": ""}
        mounts_text = "/dev/sdb1 /mnt/usb1 vfat ro 0 0\n/dev/sdc1 /mnt/usb2 vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", side_effect=[payload1, payload2]):
                await src._scan_existing_devices()
        assert len(src._pending) == 2
        uris = {e.uri for e in src._pending}
        assert uris == {"steam://run/1", "steam://run/2"}

    @pytest.mark.asyncio
    async def test_scan_registers_a_drive_plugged_in_before_startup(self):
        """`_drives` became a devnode→category dict when drive categories
        landed, but this scan still called `.add()` on it. The AttributeError
        went straight into the scan's broad handler, so the loop died at the
        first removable drive — a floppy drive plugged in before the plugin
        started never appeared in the panel at all."""
        from sources.storage_source import DriveKind
        src = _make_source()
        device = MagicMock()
        device.device_node = "/dev/sda"
        src._context = MagicMock()
        src._context.list_devices.return_value = [device]

        with patch("builtins.open", mock_open(read_data="")), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY), \
             patch.object(src, "_has_media", return_value=False):
            await src._scan_existing_devices()

        assert src.has_drive() is True
        assert src._drives["/dev/sda"] == DriveKind.FLOPPY

    @pytest.mark.asyncio
    async def test_scan_load_events_carry_the_drive_category(self):
        """Same orphaning bug as rearm(): a disk already mounted at startup
        needs its category or the panel cannot place it in a row."""
        from sources.storage_source import DriveKind
        src = _make_source()
        payload = {"version": 1, "uri": "steam://run/42", "title": "", "icon": ""}
        mounts_text = "/dev/sda /mnt/floppy vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY), \
             patch.object(src, "_read_payload", return_value=payload):
            await src._scan_existing_devices()

        assert src._pending[0].payload["drive_kind"] == DriveKind.FLOPPY


# ── Unmountable media / event-loop safety ─────────────────────────────────────

class TestUnmountableMedia:
    """An unformatted floppy takes ~20s to fail. Retrying it on every udev
    event turns the drive into a permanent stall."""

    async def _insert(self, src, action="change"):
        """One udev event, driven to completion.

        A disk is announced as loading on the poll that notices it and mounted
        on the next, so exercising a mount takes two polls — the second with no
        udev event, which is what drains the deferred queue.
        """
        src._monitor.poll.return_value = _make_udev_device(action, "/dev/sda")
        first = await src.poll()
        src._monitor.poll.return_value = None
        second = await src.poll()
        return second if second is not None else first

    def _source_with_unmountable_disk(self):
        src = _make_source()
        src._monitor = MagicMock()
        return src

    @pytest.mark.asyncio
    async def test_failed_mount_is_not_retried_while_disk_stays_in(self):
        src = self._source_with_unmountable_disk()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_mount_device", AsyncMock(return_value=None)) as mock_mount:
            for _ in range(5):
                await self._insert(src)
        assert mock_mount.call_count == 1, "retried an unmountable disk"

    @pytest.mark.asyncio
    async def test_ejecting_the_disk_clears_the_block(self):
        src = self._source_with_unmountable_disk()
        with patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_mount_device", AsyncMock(return_value=None)) as mock_mount:
            with patch.object(src, "_has_media", return_value=True):
                await self._insert(src)
            with patch.object(src, "_has_media", return_value=False):
                await self._insert(src)          # disk taken out
            with patch.object(src, "_has_media", return_value=True):
                await self._insert(src)          # put back in — try again
        assert mock_mount.call_count == 2

    @pytest.mark.asyncio
    async def test_mount_does_not_block_the_event_loop(self):
        """StorageSource.poll runs on the plugin's only event loop. A mount
        that blocks it stalls NFC polling and every RPC for its duration."""
        import subprocess as sp
        src = self._source_with_unmountable_disk()
        ticks = []
        ticks_seen_by_mount = []

        def slow_mount(*args, **kwargs):
            time.sleep(0.25)
            # Sampled before returning: this is the count of loop iterations
            # that got through *while the mount was still running*. Asserting
            # on the total after gather() proves nothing, since a blocking
            # mount still lets the ticker finish afterwards.
            ticks_seen_by_mount.append(len(ticks))
            raise sp.TimeoutExpired(cmd="mount", timeout=0.25)

        async def ticker():
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks.append(1)

        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch("subprocess.run", side_effect=slow_mount):
            await asyncio.gather(self._insert(src), ticker())

        assert ticks_seen_by_mount == [10], (
            "the event loop made no progress while mount was running — "
            "the whole plugin is frozen for the duration of every mount"
        )


# ── Drive categorisation ──────────────────────────────────────────────────────

class TestDriveKinds:
    """A floppy drive and a USB thumb drive are both removable=1, but they are
    not the same thing to a user. udev knows the difference."""

    def _classify(self, props):
        src = _make_source()
        with patch.object(src, "_udev_properties", return_value=props):
            return src.classify_drive("/dev/sda")

    def test_usb_floppy_is_a_floppy(self):
        from sources.storage_source import DriveKind
        # Exactly what the TEAC drive reports on the Deck.
        assert self._classify({
            "ID_BUS": "usb", "ID_TYPE": "floppy", "ID_DRIVE_FLOPPY": "1",
        }) == DriveKind.FLOPPY

    def test_optical_is_detected(self):
        from sources.storage_source import DriveKind
        assert self._classify({"ID_CDROM": "1", "ID_TYPE": "cd"}) == DriveKind.OPTICAL

    def test_card_reader_is_flash(self):
        from sources.storage_source import DriveKind
        assert self._classify({
            "ID_BUS": "usb", "ID_TYPE": "disk", "ID_DRIVE_FLASH_SD": "1",
        }) == DriveKind.FLASH

    def test_thumb_drive_is_usb_storage(self):
        from sources.storage_source import DriveKind
        assert self._classify({"ID_BUS": "usb", "ID_TYPE": "disk"}) == DriveKind.USB

    def test_unknown_falls_back_to_usb_storage(self):
        """Which is off by default, so a misclassification leaves disks alone."""
        from sources.storage_source import DriveKind
        assert self._classify({}) == DriveKind.USB

    def test_only_floppy_is_on_by_default(self):
        """A floppy drive on a Steam Deck was attached to be a trigger. Every
        other category is general storage holding the user's own data, and
        mounting it uninvited is both a surprise and a delay."""
        from sources.storage_source import StorageSource, DriveKind
        src = StorageSource({}, logger=MagicMock())
        assert src._drive_kind_enabled(DriveKind.FLOPPY) is True
        assert src._drive_kind_enabled(DriveKind.OPTICAL) is False
        assert src._drive_kind_enabled(DriveKind.USB) is False
        assert src._drive_kind_enabled(DriveKind.FLASH) is False

    @pytest.mark.asyncio
    async def test_disabled_category_is_never_mounted(self):
        from sources.storage_source import StorageSource, DriveKind
        src = StorageSource({}, logger=MagicMock())     # USB storage off
        with patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.USB), \
             patch.object(src, "_mount_device", new_callable=AsyncMock) as mock_mount:
            event = await src._handle_device_added("/dev/sdb1")
        assert event is None
        mock_mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_category_is_mounted(self, tmp_path):
        from sources.storage_source import StorageSource, DriveKind
        src = StorageSource({}, logger=MagicMock())     # floppies on by default
        with patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY), \
             patch.object(src, "_mount_device", AsyncMock(return_value=str(tmp_path))):
            event = await src._handle_device_added("/dev/sdb1")
        assert event is not None


# ── Mount leaks ───────────────────────────────────────────────────────────────

class TestMountLeaks:
    """A mount left behind pins its device node, so the drive that was
    /dev/sda comes back as /dev/sdb — observed on hardware 2026-08-01."""

    @pytest.mark.asyncio
    async def test_unplugging_after_eject_still_unmounts(self):
        """Ejecting clears _active_media; the later unplug must not skip cleanup."""
        src = _make_source()
        src._our_mounts["/dev/sda"] = "/tmp/decky-links-abc"
        # No _active_media entry — the disk was already ejected.
        with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
            event = await src._handle_device_removed("/dev/sda")
        assert event is None, "nothing to report, but the mount must still go"
        mock_umount.assert_called_once_with("/tmp/decky-links-abc")
        assert "/dev/sda" not in src._our_mounts

    @pytest.mark.asyncio
    async def test_stale_mounts_from_a_previous_process_are_reaped(self):
        """A restart never runs stop(), so its mounts survive in the kernel."""
        src = _make_source()
        mounts = (
            "/dev/sda /tmp/decky-links-lp01k9a5 vfat ro,relatime 0 0\n"
            "/dev/mmcblk0p1 /run/media/deck/SC256 ext4 rw 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=mounts)):
            with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
                await src._reap_stale_mounts()
        mock_umount.assert_called_once_with("/tmp/decky-links-lp01k9a5")

    @pytest.mark.asyncio
    async def test_reaping_leaves_other_mounts_alone(self):
        src = _make_source()
        mounts = (
            "/dev/mmcblk0p1 /run/media/deck/SC256 ext4 rw 0 0\n"
            "/dev/nvme0n1p8 /home ext4 rw 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=mounts)):
            with patch.object(src, "_unmount_device", new_callable=AsyncMock) as mock_umount:
                await src._reap_stale_mounts()
        mock_umount.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_proc_mounts_is_survivable(self):
        src = _make_source()
        with patch("builtins.open", side_effect=OSError("no /proc")):
            await src._reap_stale_mounts()   # must not raise


# ── rearm() ───────────────────────────────────────────────────────────────────

class TestRearm:
    """udev fires once, on insertion. Pressing Pair with a disk already in the
    drive must not wait for an event that will never come."""

    @pytest.mark.asyncio
    async def test_rearm_requeues_load_for_present_disk(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/42"
        src.rearm()
        event = await src.poll()
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.media_id == "/dev/sdb1"
        assert event.uri == "steam://run/42"

    @pytest.mark.asyncio
    async def test_rearm_marks_unpaired_disk_blank(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = ""
        src.rearm()
        event = await src.poll()
        assert event.payload["blank"] is True

    def test_rearm_with_no_media_is_a_no_op(self):
        src = _make_source()
        src.rearm()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_rearm_carries_the_drive_category(self):
        """Reported from hardware: re-pairing a floppy wrote the disk and played
        the sound, but the row went to "No disk" until the disk was ejected and
        reinserted. Pair calls rearm(), and the row finds its medium by drive
        category — a rearm without one orphaned the disk from its own row."""
        from sources.storage_source import DriveKind
        src = _make_source()
        src._active_media["/dev/sda"] = ""
        src._drives["/dev/sda"] = DriveKind.FLOPPY
        src.rearm()
        event = await src.poll()
        assert event.payload["drive_kind"] == DriveKind.FLOPPY

    @pytest.mark.asyncio
    async def test_rearm_classifies_a_drive_it_has_not_seen(self, monkeypatch):
        from sources.storage_source import DriveKind
        src = _make_source()
        src._active_media["/dev/sr0"] = ""
        monkeypatch.setattr(src, "classify_drive", lambda d: DriveKind.OPTICAL)
        src.rearm()
        event = await src.poll()
        assert event.payload["drive_kind"] == DriveKind.OPTICAL


# ── write_uri() — pairing a disk ──────────────────────────────────────────────

class TestWriteUri:

    def test_source_advertises_write_capability(self):
        assert _make_source().can_write() is True

    @pytest.mark.asyncio
    async def test_writes_payload_to_mounted_disk(self, tmp_path):
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)):
            ok, err = await src.write_uri("/dev/sdb1", "steam://rungameid/400")
        assert (ok, err) == (True, None)
        written = json.loads((tmp_path / "decky-links.json").read_text())
        assert written == {
            "version": 1, "uri": "steam://rungameid/400", "title": "", "icon": "",
        }
        assert src._active_media["/dev/sdb1"] == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_written_payload_reads_back(self, tmp_path):
        """The file we write must satisfy the reader that will parse it later."""
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)):
            await src.write_uri("/dev/sdb1", "steam://rungameid/400", title="Portal")
        payload = src._read_payload(str(tmp_path / "decky-links.json"))
        assert payload is not None
        assert payload["uri"] == "steam://rungameid/400"
        assert payload["title"] == "Portal"

    @pytest.mark.asyncio
    async def test_title_reaches_the_disk_from_pairing(self, tmp_path):
        """The whole point of carrying it: a disk read on another machine should
        name the game rather than just carrying an app id."""
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)):
            await src.write_uri("/dev/sdb1", "steam://rungameid/400", "Half-Life 2")
        written = json.loads((tmp_path / "decky-links.json").read_text())
        assert written["title"] == "Half-Life 2"

    @pytest.mark.asyncio
    async def test_title_survives_the_round_trip_into_a_load_event(self, tmp_path):
        """Written, then read back, then forwarded into the MediaEvent the panel
        sees — the read side already carried title, only the write dropped it."""
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)):
            await src.write_uri("/dev/sdb1", "steam://rungameid/400", "Hades")

        event = src._load_event(
            "/dev/sdb1", "steam://rungameid/400",
            **{k: v for k, v in src._read_payload(
                str(tmp_path / "decky-links.json")).items() if k != "uri"}
        )
        assert event.payload.get("title") == "Hades"

    @pytest.mark.asyncio
    async def test_remounts_read_only_afterwards(self, tmp_path):
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)) as mock_remount:
            await src.write_uri("/dev/sdb1", "steam://run/1")
        assert [c.args[1] for c in mock_remount.call_args_list] == ["rw", "ro"]

    @pytest.mark.asyncio
    async def test_restores_read_only_even_when_write_fails(self, tmp_path):
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=True)) as mock_remount:
            with patch("builtins.open", side_effect=OSError("disk full")):
                ok, err = await src.write_uri("/dev/sdb1", "steam://run/1")
        assert ok is False
        assert "disk full" in err
        # Leaving the disk writable after a failed pair is how a floppy gets
        # corrupted by the next sudden eject.
        assert [c.args[1] for c in mock_remount.call_args_list] == ["rw", "ro"]

    @pytest.mark.asyncio
    async def test_fails_when_not_mounted(self):
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            ok, err = await src.write_uri("/dev/sdb1", "steam://run/1")
        assert ok is False
        assert "not mounted" in err

    @pytest.mark.asyncio
    async def test_fails_when_remount_rw_refused(self, tmp_path):
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", AsyncMock(return_value=False)):
            ok, err = await src.write_uri("/dev/sdb1", "steam://run/1")
        assert ok is False
        assert "read-write" in err
        assert not (tmp_path / "decky-links.json").exists()

    @pytest.mark.asyncio
    async def test_falls_back_to_system_mount_point(self, tmp_path):
        """Disks the system mounted are pairable too, not just ones we mounted."""
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            with patch.object(src, "_remount", AsyncMock(return_value=True)):
                ok, _ = await src.write_uri("/dev/sdb1", "steam://run/1")
        assert ok is True
        assert (tmp_path / "decky-links.json").exists()


# ── _is_removable() ───────────────────────────────────────────────────────────

class TestIsRemovable:

    def _run(self, src, devnode, flag, is_partition=False):
        with patch("os.path.realpath", side_effect=lambda p: p):
            with patch("os.path.exists", return_value=is_partition):
                with patch("builtins.open", mock_open(read_data=flag)):
                    return src._is_removable(devnode)

    def test_removable_flag_one_accepted(self):
        assert self._run(_make_source(), "/dev/sda", "1\n") is True

    def test_removable_flag_zero_rejected(self):
        assert self._run(_make_source(), "/dev/nvme0n1", "0\n") is False

    def test_partition_inherits_parent_disk_flag(self):
        src = _make_source()
        opened = []

        def fake_open(path, *a, **k):
            opened.append(path)
            return mock_open(read_data="1\n")(path, *a, **k)

        with patch("os.path.realpath", side_effect=lambda p: p):
            with patch("os.path.exists", return_value=True):  # it's a partition
                with patch("builtins.open", side_effect=fake_open):
                    result = src._is_removable("/dev/sda1")
        assert result is True
        # Read the parent disk's flag, not the partition's own path
        assert opened == [os.path.join("/sys/class/block", "removable")]

    def test_missing_sysfs_entry_treated_as_fixed(self):
        src = _make_source()
        with patch("builtins.open", side_effect=OSError("no such file")):
            assert src._is_removable("/dev/sdz") is False


# ── _is_relevant_device() ─────────────────────────────────────────────────────

class TestIsRelevantDevice:

    @pytest.mark.parametrize("devnode", [
        "/dev/sda", "/dev/sda1", "/dev/sdb1",
        "/dev/sr0",
        "/dev/mmcblk0", "/dev/mmcblk0p1",
        "/dev/nvme0n1", "/dev/nvme0n1p1",
        "/dev/fd0",
    ])
    def test_relevant_devices_accepted(self, devnode):
        src = _make_source()
        assert src._is_relevant_device(devnode) is True

    @pytest.mark.parametrize("devnode", [
        "/dev/loop0",
        "/dev/tty0",
        "/dev/null",
        "/dev/urandom",
        "/dev/dm-0",
    ])
    def test_irrelevant_devices_rejected(self, devnode):
        src = _make_source()
        assert src._is_relevant_device(devnode) is False


# ── Integration: full add/remove cycle ────────────────────────────────────────

class TestIntegration:

    @pytest.mark.asyncio
    async def test_add_then_remove_produces_load_then_unload(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        src._monitor = MagicMock()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/777",
        }))

        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdb1")
            load_event = await src.poll()

        assert load_event is not None
        assert load_event.kind == MediaEventKind.LOAD
        assert load_event.uri == "steam://run/777"

        src._monitor.poll.return_value = _make_udev_device("remove", "/dev/sdb1")
        unload_event = await src.poll()

        assert unload_event is not None
        assert unload_event.kind == MediaEventKind.UNLOAD
        assert unload_event.uri == "steam://run/777"
        assert src._active_media == {}

    @pytest.mark.asyncio
    async def test_add_without_payload_produces_blank_load(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        src._monitor = MagicMock()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdb1")
            event = await src.poll()
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.uri == ""
        assert event.payload["blank"] is True
        assert src._active_media == {"/dev/sdb1": ""}

    @pytest.mark.asyncio
    async def test_startup_scan_events_emitted_before_udev_events(self, tmp_path):
        from sources.base import MediaEvent, MediaEventKind, SourceType
        src = _make_source()
        src._monitor = MagicMock()
        queued = MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
            media_id="/dev/sdb1",
            uri="steam://run/111",
        )
        src._pending.append(queued)
        src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdc1")

        first = await src.poll()
        assert first is queued
        assert src._monitor.poll.call_count == 0


# ── has_media() ───────────────────────────────────────────────────────────────

class TestHasMedia:

    def test_has_media_false_when_no_active_media(self):
        src = _make_source()
        assert src.has_media() is False

    def test_has_media_false_when_monitor_running_but_no_payload(self):
        src = _make_source()
        src._monitor = MagicMock()
        assert src.has_media() is False

    def test_has_media_true_when_device_with_payload_present(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        assert src.has_media() is True

    def test_has_media_true_for_multiple_devices(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/1"
        src._active_media["/dev/sdc1"] = "steam://run/2"
        assert src.has_media() is True

    def test_has_media_false_after_device_entry_removed(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        del src._active_media["/dev/sdb1"]
        assert src.has_media() is False

    def test_has_media_independent_of_is_active(self):
        # udev monitor up (is_active True) but no payload device found
        src = _make_source()
        src._monitor = MagicMock()
        assert src.is_active() is True
        assert src.has_media() is False

    @pytest.mark.asyncio
    async def test_has_media_false_after_stop(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        assert src.has_media() is True
        await src.stop()
        assert src.has_media() is False

    @pytest.mark.asyncio
    async def test_has_media_tracks_load_unload_cycle(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/42",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            await src._handle_device_added("/dev/sdb1")
        assert src.has_media() is True

        await src._handle_device_removed("/dev/sdb1")
        assert src.has_media() is False


# ── Media-change handling ────────────────────────────────────────────────────

class TestMediaChangeEvents:
    """Inserting a disk into an already-connected drive emits 'change'.

    A USB floppy or card reader stays enumerated while its media comes and
    goes, so 'add'/'remove' alone never fire for the disk itself.
    """

    def _source(self, tmp_path):
        from sources.storage_source import StorageSource
        src = StorageSource(settings={}, logger=None)
        src._monitor = MagicMock()
        return src

    @pytest.mark.asyncio
    async def test_change_with_media_emits_load(self, tmp_path, monkeypatch):
        from sources.base import MediaEventKind
        src = self._source(tmp_path)

        device = MagicMock()
        device.device_node = "/dev/sdb"
        device.action = "change"
        src._monitor.poll.return_value = device

        monkeypatch.setattr(src, "_has_media", lambda d: True)
        monkeypatch.setattr(src, "_find_mount_point", lambda d: "/mnt/floppy")
        monkeypatch.setattr(
            src, "_read_payload",
            lambda p: {"version": 1, "uri": "steam://rungameid/400", "title": "", "icon": ""},
        )

        event = await src.poll()
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.media_id == "/dev/sdb"
        assert event.uri == "steam://rungameid/400"

    @pytest.mark.asyncio
    async def test_change_without_media_emits_unload(self, tmp_path, monkeypatch):
        from sources.base import MediaEventKind
        src = self._source(tmp_path)
        src._active_media["/dev/sdb"] = "steam://rungameid/400"

        device = MagicMock()
        device.device_node = "/dev/sdb"
        device.action = "change"
        src._monitor.poll.return_value = device

        monkeypatch.setattr(src, "_has_media", lambda d: False)

        event = await src.poll()
        assert event is not None
        assert event.kind == MediaEventKind.UNLOAD
        assert event.media_id == "/dev/sdb"

    @pytest.mark.asyncio
    async def test_change_with_no_state_change_is_ignored(self, tmp_path, monkeypatch):
        """Repeated change events for unchanged media must not re-fire."""
        src = self._source(tmp_path)
        src._active_media["/dev/sdb"] = "steam://rungameid/400"

        device = MagicMock()
        device.device_node = "/dev/sdb"
        device.action = "change"
        src._monitor.poll.return_value = device
        monkeypatch.setattr(src, "_has_media", lambda d: True)

        assert await src.poll() is None

    @pytest.mark.asyncio
    async def test_add_without_media_is_ignored(self, tmp_path, monkeypatch):
        """A drive plugged in empty must not trigger a doomed mount."""
        src = self._source(tmp_path)

        device = MagicMock()
        device.device_node = "/dev/sdb"
        device.action = "add"
        src._monitor.poll.return_value = device

        monkeypatch.setattr(src, "_has_media", lambda d: False)
        called = []
        monkeypatch.setattr(src, "_handle_device_added", lambda d: called.append(d))

        assert await src.poll() is None
        assert called == []

    def test_has_media_reads_sysfs_size(self, tmp_path, monkeypatch):
        from sources.storage_source import StorageSource
        src = StorageSource(settings={}, logger=None)

        sysfs = tmp_path / "sys" / "class" / "block" / "sdb"
        sysfs.mkdir(parents=True)
        (sysfs / "size").write_text("2880\n")   # a 1.44MB floppy

        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if str(path) == "/sys/class/block/sdb/size":
                return real_open(sysfs / "size", *a, **k)
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert src._has_media("/dev/sdb") is True

        (sysfs / "size").write_text("0\n")      # drive present, no disk
        assert src._has_media("/dev/sdb") is False


# ── Loading state ─────────────────────────────────────────────────────────────

class TestLoadingState:
    """Mounting a floppy takes anywhere up to the timeout — a minute is normal
    for a tired drive and a dusty disk. Done inside the poll that noticed the
    disk, nothing reaches the panel until it finishes, so the row reads "No
    disk" for the whole wait and looks broken."""

    def _udev(self, action, devnode="/dev/sda"):
        return _make_udev_device(action, devnode)

    async def _poll_with(self, src, device):
        src._monitor = MagicMock()
        src._monitor.poll.return_value = device
        return await src.poll()

    @pytest.mark.asyncio
    async def test_insert_announces_loading_before_mounting(self):
        from sources.base import MediaEventKind
        from sources.storage_source import DriveKind
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY), \
             patch.object(src, "_mount_device", new_callable=AsyncMock) as mount:
            event = await self._poll_with(src, self._udev("change"))
            mount.assert_not_called()

        assert event.kind == MediaEventKind.LOADING
        assert event.media_id == "/dev/sda"
        assert event.payload["drive_kind"] == DriveKind.FLOPPY

    @pytest.mark.asyncio
    async def test_the_mount_happens_on_the_next_poll(self):
        from sources.base import MediaEventKind
        from sources.storage_source import DriveKind
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY), \
             patch.object(src, "_mount_device", AsyncMock(return_value="/tmp/m")), \
             patch.object(src, "_read_payload", return_value={"uri": "steam://run/7"}):
            await self._poll_with(src, self._udev("change"))
            event = await self._poll_with(src, None)

        assert event.kind == MediaEventKind.LOAD
        assert event.uri == "steam://run/7"

    @pytest.mark.asyncio
    async def test_an_already_mounted_disk_skips_the_loading_state(self):
        """Reading the payload off a mounted filesystem is a file read, far too
        quick to be worth announcing."""
        from sources.base import MediaEventKind
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value="/mnt/floppy"), \
             patch.object(src, "_read_payload", return_value={"uri": "steam://run/7"}):
            event = await self._poll_with(src, self._udev("change"))
        assert event.kind == MediaEventKind.LOAD

    @pytest.mark.asyncio
    async def test_a_drive_we_will_not_mount_never_says_loading(self):
        """A row left saying "Reading disk…" forever is worse than one that
        says nothing, so only work we are actually going to do is announced."""
        from sources.storage_source import DriveKind
        src = _make_source({"drive_kinds": {DriveKind.FLOPPY: False}})
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY):
            event = await self._poll_with(src, self._udev("change"))
        assert event is None
        assert len(src._deferred_mounts) == 0

    @pytest.mark.asyncio
    async def test_a_fixed_disk_never_says_loading(self):
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=False):
            event = await self._poll_with(src, self._udev("change"))
        assert event is None

    @pytest.mark.asyncio
    async def test_ejecting_while_loading_clears_the_pending_mount(self):
        from sources.storage_source import DriveKind
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY):
            await self._poll_with(src, self._udev("change"))
        assert "/dev/sda" in src._loading

        with patch.object(src, "_has_media", return_value=False):
            await self._poll_with(src, self._udev("remove"))
        assert "/dev/sda" not in src._loading

    @pytest.mark.asyncio
    async def test_repeated_events_do_not_queue_two_mounts(self):
        """udev is chatty — a single insertion can produce several `change`
        events before the mount runs, and each one must not add another
        20-second attempt to the queue."""
        from sources.storage_source import DriveKind
        src = _make_source()
        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.FLOPPY):
            first = await src._begin_load("/dev/sda")
            second = await src._begin_load("/dev/sda")
        assert first is not None
        assert second is None, "the second event must not announce loading again"
        assert len(src._deferred_mounts) == 1


# ── Partitioned devices ───────────────────────────────────────────────────────

class TestPartitions:
    """A partitioned USB stick presents both /dev/sda and /dev/sda1. Taking
    both put two devnodes in one registry slot — the log filled with "Multiple
    media on storage:udev: /dev/sda, /dev/sda1" and pairing wrote to whichever
    was recorded last, which was the whole disk. That is not what is mounted,
    hence "Pairing failed: /dev/sda is not mounted"."""

    def test_whole_disk_with_partitions_is_ignored(self):
        src = _make_source()
        with patch("os.listdir", return_value=["sda1", "queue", "size"]):
            assert src._is_relevant_device("/dev/sda") is False

    def test_the_partition_itself_is_used(self):
        src = _make_source()
        with patch("os.listdir", return_value=["queue", "size", "partition"]):
            assert src._is_relevant_device("/dev/sda1") is True

    def test_an_unpartitioned_disk_is_still_used(self):
        """A floppy has no partition table: /dev/sda *is* the filesystem."""
        src = _make_source()
        with patch("os.listdir", return_value=["queue", "size", "removable"]):
            assert src._is_relevant_device("/dev/sda") is True

    def test_missing_sysfs_does_not_hide_a_device(self):
        """Unplugging removes sysfs before the remove event is handled, and a
        remove we ignore is a mount we never release."""
        src = _make_source()
        with patch("os.listdir", side_effect=OSError("gone")):
            assert src._is_relevant_device("/dev/sda") is True


# ── rescan() ──────────────────────────────────────────────────────────────────

class TestRescan:
    """Reported from hardware: with USB storage off, inserting a stick and then
    switching the category on left the row empty. udev fired when the stick went
    in, we declined it, and udev does not fire again."""

    @pytest.mark.asyncio
    async def test_enabling_a_category_picks_up_media_already_inserted(self):
        from sources.base import MediaEventKind
        from sources.storage_source import DriveKind
        src = _make_source({"drive_kinds": {DriveKind.USB: False}})
        src._drives["/dev/sda1"] = DriveKind.USB

        with patch.object(src, "_has_media", return_value=True), \
             patch.object(src, "_find_mount_point", return_value=None), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "classify_drive", return_value=DriveKind.USB):
            await src.rescan()
            assert len(src._pending) == 0, "a disabled category must stay ignored"

            src._settings["drive_kinds"] = {DriveKind.USB: True}
            await src.rescan()

        assert len(src._pending) == 1
        assert src._pending[0].kind == MediaEventKind.LOADING

    @pytest.mark.asyncio
    async def test_rescan_does_not_reload_media_already_read(self):
        from sources.storage_source import DriveKind
        src = _make_source()
        src._drives["/dev/sda"] = DriveKind.FLOPPY
        src._active_media["/dev/sda"] = "steam://run/1"
        with patch.object(src, "_has_media", return_value=True):
            await src.rescan()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_rescan_does_not_retry_an_unmountable_disk(self):
        from sources.storage_source import DriveKind
        src = _make_source()
        src._drives["/dev/sda"] = DriveKind.FLOPPY
        src._unmountable.add("/dev/sda")
        with patch.object(src, "_has_media", return_value=True):
            await src.rescan()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_rescan_ignores_an_empty_drive(self):
        from sources.storage_source import DriveKind
        src = _make_source()
        src._drives["/dev/sda"] = DriveKind.FLOPPY
        with patch.object(src, "_has_media", return_value=False):
            await src.rescan()
        assert len(src._pending) == 0


# ── write_uri and mount ownership ─────────────────────────────────────────────

class TestWriteUriRemountOwnership:
    """Pairing may target a mount the system created rather than one of ours.
    Flipping that to rw and then forcing it back to ro would leave the user's
    own mount read-only with no indication why."""

    @pytest.mark.asyncio
    async def test_our_own_mount_is_remounted_around_the_write(self, tmp_path):
        src = _make_source()
        src._our_mounts["/dev/sdb1"] = str(tmp_path)
        with patch.object(src, "_remount", new_callable=AsyncMock) as remount:
            remount.return_value = True
            ok, err = await src.write_uri("/dev/sdb1", "steam://rungameid/220")
        assert ok is True, err
        assert [c[0][1] for c in remount.call_args_list] == ["rw", "ro"]

    @pytest.mark.asyncio
    async def test_system_mount_is_left_alone(self, tmp_path):
        src = _make_source()  # not in _our_mounts
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            with patch.object(src, "_remount", new_callable=AsyncMock) as remount:
                ok, err = await src.write_uri("/dev/sdb1", "steam://rungameid/220")
        assert ok is True, err
        remount.assert_not_called()

    @pytest.mark.asyncio
    async def test_payload_is_written_either_way(self, tmp_path):
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            await src.write_uri("/dev/sdb1", "steam://rungameid/220")
        written = json.loads((tmp_path / "decky-links.json").read_text())
        assert written["uri"] == "steam://rungameid/220"
        assert written["version"] == 1
