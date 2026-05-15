"""
test_storage_source.py — unit tests for StorageSource.

All hardware-level dependencies (pyudev, subprocess, /proc/mounts, file I/O)
are mocked so the suite runs on any platform.
"""
import asyncio
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, mock_open


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    from sources.storage_source import StorageSource
    return StorageSource(settings or {}, logger=MagicMock())


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
        with patch.object(src, "_unmount_device") as mock_umount:
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
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdb1")
        with patch.object(src, "_handle_device_added", return_value=None) as mock_add:
            await src.poll()
        mock_add.assert_called_once_with("/dev/sdb1")

    @pytest.mark.asyncio
    async def test_poll_dispatches_remove_event(self):
        src = _make_source()
        src._monitor = MagicMock()
        src._monitor.poll.return_value = _make_udev_device("remove", "/dev/sdb1")
        with patch.object(src, "_handle_device_removed", return_value=None) as mock_rem:
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

    def test_existing_mount_with_payload_emits_load(self, tmp_path):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/12345", "title": "Game", "icon": "",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.source_type == SourceType.STORAGE
        assert event.uri == "steam://run/12345"
        assert event.media_id == "/dev/sdb1"
        assert src._active_media["/dev/sdb1"] == "steam://run/12345"

    def test_existing_mount_without_payload_returns_none(self, tmp_path):
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = src._handle_device_added("/dev/sdb1")
        assert event is None
        assert "/dev/sdb1" not in src._active_media

    def test_no_mount_then_mounts_and_emits_load(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/999",
        }))
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_mount_device", return_value=str(tmp_path)):
                event = src._handle_device_added("/dev/sdb1")
        assert event is not None
        assert event.uri == "steam://run/999"
        assert src._our_mounts["/dev/sdb1"] == str(tmp_path)

    def test_no_mount_and_mount_fails_returns_none(self):
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_mount_device", return_value=None):
                event = src._handle_device_added("/dev/sdb1")
        assert event is None

    def test_our_mount_cleaned_up_when_no_payload(self, tmp_path):
        src = _make_source()
        with patch.object(src, "_find_mount_point", return_value=None):
            with patch.object(src, "_mount_device", return_value=str(tmp_path)):
                with patch.object(src, "_unmount_device") as mock_umount:
                    event = src._handle_device_added("/dev/sdb1")
        assert event is None
        mock_umount.assert_called_once_with(str(tmp_path))
        assert "/dev/sdb1" not in src._our_mounts

    def test_uri_excluded_from_event_payload(self, tmp_path):
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/1", "title": "T", "icon": "i.png",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            event = src._handle_device_added("/dev/sdb1")
        assert "uri" not in event.payload
        assert event.payload["title"] == "T"
        assert event.payload["version"] == 1


# ── _handle_device_removed() ──────────────────────────────────────────────────

class TestHandleDeviceRemoved:

    def test_known_device_emits_unload(self):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/12345"
        event = src._handle_device_removed("/dev/sdb1")
        assert event is not None
        assert event.kind == MediaEventKind.UNLOAD
        assert event.source_type == SourceType.STORAGE
        assert event.uri == "steam://run/12345"
        assert event.media_id == "/dev/sdb1"
        assert "/dev/sdb1" not in src._active_media

    def test_unknown_device_returns_none(self):
        src = _make_source()
        event = src._handle_device_removed("/dev/sdb1")
        assert event is None

    def test_our_mount_is_unmounted_on_removal(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/1"
        src._our_mounts["/dev/sdb1"] = "/tmp/decky-links-xyz"
        with patch.object(src, "_unmount_device") as mock_umount:
            src._handle_device_removed("/dev/sdb1")
        mock_umount.assert_called_once_with("/tmp/decky-links-xyz")
        assert "/dev/sdb1" not in src._our_mounts

    def test_externally_mounted_device_no_unmount_called(self):
        src = _make_source()
        src._active_media["/dev/sdb1"] = "steam://run/1"
        # Not in _our_mounts — we didn't mount it
        with patch.object(src, "_unmount_device") as mock_umount:
            src._handle_device_removed("/dev/sdb1")
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

class TestMountDevice:

    def test_successful_mount_returns_tmpdir(self):
        src = _make_source()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("tempfile.mkdtemp", return_value="/tmp/decky-links-test"):
                result = src._mount_device("/dev/sdb1")
        assert result == "/tmp/decky-links-test"
        args = mock_run.call_args[0][0]
        assert "mount" in args
        assert "ro" in args
        assert "/dev/sdb1" in args

    def test_failed_mount_returns_none(self, tmp_path):
        src = _make_source()
        tmpdir = str(tmp_path / "mnt")
        os.makedirs(tmpdir)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=32, stderr=b"permission denied")
            with patch("tempfile.mkdtemp", return_value=tmpdir):
                result = src._mount_device("/dev/sdb1")
        assert result is None

    def test_mount_exception_returns_none(self, tmp_path):
        src = _make_source()
        tmpdir = str(tmp_path / "mnt")
        os.makedirs(tmpdir)
        with patch("subprocess.run", side_effect=Exception("timeout")):
            with patch("tempfile.mkdtemp", return_value=tmpdir):
                result = src._mount_device("/dev/sdb1")
        assert result is None


# ── _scan_existing_devices() ──────────────────────────────────────────────────

class TestScanExistingDevices:

    def test_buffers_load_event_for_matching_mount(self):
        from sources.base import MediaEventKind
        src = _make_source()
        payload = {"version": 1, "uri": "steam://run/42", "title": "Game 42", "icon": ""}
        mounts_text = "/dev/sdb1 /mnt/usb vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", return_value=payload):
                src._scan_existing_devices()
        assert len(src._pending) == 1
        evt = src._pending[0]
        assert evt.kind == MediaEventKind.LOAD
        assert evt.uri == "steam://run/42"
        assert evt.media_id == "/dev/sdb1"
        assert src._active_media["/dev/sdb1"] == "steam://run/42"

    def test_ignores_non_storage_devices(self):
        src = _make_source()
        mounts_text = "tmpfs /run tmpfs rw 0 0\nsysfs /sys sysfs rw 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            src._scan_existing_devices()
        assert len(src._pending) == 0

    def test_ignores_device_without_payload(self):
        src = _make_source()
        mounts_text = "/dev/sdb1 /mnt/usb vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", return_value=None):
                src._scan_existing_devices()
        assert len(src._pending) == 0

    def test_handles_proc_mounts_missing_gracefully(self):
        src = _make_source()
        with patch("builtins.open", side_effect=OSError("no such file")):
            src._scan_existing_devices()
        assert len(src._pending) == 0

    def test_multiple_matching_mounts(self):
        from sources.base import MediaEventKind
        src = _make_source()
        payload1 = {"version": 1, "uri": "steam://run/1", "title": "", "icon": ""}
        payload2 = {"version": 1, "uri": "steam://run/2", "title": "", "icon": ""}
        mounts_text = "/dev/sdb1 /mnt/usb1 vfat ro 0 0\n/dev/sdc1 /mnt/usb2 vfat ro 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts_text)):
            with patch.object(src, "_read_payload", side_effect=[payload1, payload2]):
                src._scan_existing_devices()
        assert len(src._pending) == 2
        uris = {e.uri for e in src._pending}
        assert uris == {"steam://run/1", "steam://run/2"}


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
    async def test_add_without_payload_produces_no_events(self, tmp_path):
        src = _make_source()
        src._monitor = MagicMock()
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            src._monitor.poll.return_value = _make_udev_device("add", "/dev/sdb1")
            event = await src.poll()
        assert event is None
        assert src._active_media == {}

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

    def test_has_media_tracks_load_unload_cycle(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source()
        (tmp_path / "decky-links.json").write_text(json.dumps({
            "version": 1, "uri": "steam://run/42",
        }))
        with patch.object(src, "_find_mount_point", return_value=str(tmp_path)):
            src._handle_device_added("/dev/sdb1")
        assert src.has_media() is True

        src._handle_device_removed("/dev/sdb1")
        assert src.has_media() is False
