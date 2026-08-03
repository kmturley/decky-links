"""
test_format_media.py — formatting a disk, and every reason not to.

This runs `mkfs.vfat` as root against a device path that arrived over an RPC.
The guards are the feature; the format itself is one subprocess call. Each test
below is a way the wrong disk could be erased.

The substantive policy is the last guard: blkid must find no filesystem. That is
not a safety belt on top of a destructive action — it means the action can only
ever run on a disk with nothing on it to lose.
"""
import stat
import subprocess
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sources.storage_source import StorageSource, UNFORMATTED_ERROR


def _make_source(**settings):
    base = {"enabled": True, "drive_kinds": {"floppy": True}}
    base.update(settings)
    return StorageSource(base, logger=MagicMock())


def _tracked(devnode="/dev/sda", kind="floppy"):
    """A source that already knows about this drive, as udev would have told it."""
    src = _make_source()
    src._drives[devnode] = kind
    return src


class _FakeStat:
    def __init__(self, mode):
        self.st_mode = mode


def _block_stat(*_a, **_k):
    return _FakeStat(stat.S_IFBLK | 0o660)


def _file_stat(*_a, **_k):
    return _FakeStat(stat.S_IFREG | 0o644)


def _ok_run(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")


# ── The guards ────────────────────────────────────────────────────────────────

class TestRefusals:

    @pytest.mark.asyncio
    async def test_refuses_a_device_it_does_not_track(self):
        """The device path arrives over an RPC. Only devices udev told us about
        can be named — otherwise the argument is an arbitrary path."""
        src = _make_source()
        ok, err = await src.format_media("/dev/sda")
        assert ok is False
        assert "unknown device" in err

    @pytest.mark.asyncio
    async def test_refuses_a_path_that_does_not_exist(self):
        src = _tracked("/dev/nope")
        with patch("os.path.exists", return_value=False):
            ok, err = await src.format_media("/dev/nope")
        assert ok is False
        assert "does not exist" in err

    @pytest.mark.asyncio
    async def test_refuses_a_regular_file(self):
        """A path that is not a block device is not a disk, whatever it is
        named."""
        src = _tracked()
        with patch("os.path.exists", return_value=True), \
             patch("os.stat", _file_stat):
            ok, err = await src.format_media("/dev/sda")
        assert ok is False
        assert "not a block device" in err

    @pytest.mark.asyncio
    async def test_refuses_a_non_removable_drive(self):
        """The guard between a bug and the internal drive."""
        src = _tracked()
        with patch("os.path.exists", return_value=True), \
             patch("os.stat", _block_stat), \
             patch.object(src, "_is_removable", return_value=False):
            ok, err = await src.format_media("/dev/sda")
        assert ok is False
        assert "not a removable drive" in err

    @pytest.mark.asyncio
    async def test_refuses_when_a_partition_is_mounted(self):
        """Formatting the whole device out from under a live filesystem is how
        you corrupt something that was not the target."""
        src = _tracked()
        with patch("os.path.exists", return_value=True), \
             patch("os.stat", _block_stat), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_has_any_mounted_partition", return_value="/run/media/x"):
            ok, err = await src.format_media("/dev/sda")
        assert ok is False
        assert "in use" in err
        assert "/run/media/x" in err

    @pytest.mark.asyncio
    async def test_refuses_a_disk_that_already_has_a_filesystem(self):
        """The policy, not a belt. A disk holding a filesystem has data on it
        even when that filesystem is one we decline to mount — an ntfs stick is
        'unreadable' to this plugin and full of the user's files."""
        src = _tracked()
        with patch("os.path.exists", return_value=True), \
             patch("os.stat", _block_stat), \
             patch.object(src, "_is_removable", return_value=True), \
             patch.object(src, "_has_any_mounted_partition", return_value=None), \
             patch.object(src, "_probe_filesystem", AsyncMock(return_value="ntfs")):
            ok, err = await src.format_media("/dev/sda")
        assert ok is False
        assert "ntfs" in err

    @pytest.mark.asyncio
    async def test_no_refusal_path_ever_runs_mkfs(self):
        """Belt and braces on the tests themselves: if a guard returns False, the
        subprocess must not have been reached."""
        src = _make_source()
        with patch("subprocess.run") as run:
            await src.format_media("/dev/sda")  # unknown device
        run.assert_not_called()


# ── The happy path ────────────────────────────────────────────────────────────

class TestFormatting:

    @staticmethod
    def _permissive(src):
        return [
            patch("os.path.exists", return_value=True),
            patch("os.stat", _block_stat),
            patch.object(src, "_is_removable", return_value=True),
            patch.object(src, "_has_any_mounted_partition", return_value=None),
            patch.object(src, "_probe_filesystem", AsyncMock(return_value=None)),
        ]

    @pytest.mark.asyncio
    async def test_formats_an_unformatted_removable_disk(self):
        src = _tracked()
        calls = []

        def _record(argv, **kwargs):
            calls.append(argv)
            return _ok_run()

        with patch("subprocess.run", _record):
            for p in self._permissive(src):
                p.start()
            try:
                ok, err = await src.format_media("/dev/sda")
            finally:
                patch.stopall()

        assert (ok, err) == (True, None)
        assert calls == [["mkfs.vfat", "-I", "/dev/sda"]]

    @pytest.mark.asyncio
    async def test_a_failed_mkfs_reports_its_stderr(self):
        src = _tracked()
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"mkfs.vfat: unable to open"
        )
        with patch("subprocess.run", return_value=failed):
            for p in self._permissive(src):
                p.start()
            try:
                ok, err = await src.format_media("/dev/sda")
            finally:
                patch.stopall()
        assert ok is False
        assert "unable to open" in err

    @pytest.mark.asyncio
    async def test_missing_mkfs_is_reported_not_raised(self):
        src = _tracked()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            for p in self._permissive(src):
                p.start()
            try:
                ok, err = await src.format_media("/dev/sda")
            finally:
                patch.stopall()
        assert ok is False
        assert "not installed" in err

    @pytest.mark.asyncio
    async def test_success_clears_the_known_bad_marker(self):
        """The disk is a different disk now. Leaving it in _unmountable means
        the next poll skips it and the freshly formatted disk never mounts."""
        src = _tracked()
        src._unmountable.add("/dev/sda")
        src._active_media["/dev/sda"] = ""

        with patch("subprocess.run", _ok_run):
            for p in self._permissive(src):
                p.start()
            try:
                await src.format_media("/dev/sda")
            finally:
                patch.stopall()

        assert "/dev/sda" not in src._unmountable
        assert "/dev/sda" not in src._active_media


# ── What the panel keys off ───────────────────────────────────────────────────

class TestFormattableFlag:
    """The button appears on `formattable`, never on `problem == "unreadable"`.

    Both an unformatted floppy and an ntfs stick full of holiday photos are
    unreadable to this plugin. Only one of them may be offered a Format button,
    and the frontend must not be the thing telling them apart.
    """

    def test_no_filesystem_is_formattable(self):
        src = _make_source()
        src._last_mount_error = UNFORMATTED_ERROR
        event = src._load_event(
            "/dev/sda", "", unreadable=True,
            error=src._last_mount_error,
            formattable=(src._last_mount_error == UNFORMATTED_ERROR),
        )
        assert event.payload["formattable"] is True

    def test_an_unsupported_filesystem_is_not_formattable(self):
        """The case that matters. This disk has data on it."""
        src = _make_source()
        src._last_mount_error = "ntfs not supported"
        event = src._load_event(
            "/dev/sda", "", unreadable=True,
            error=src._last_mount_error,
            formattable=(src._last_mount_error == UNFORMATTED_ERROR),
        )
        assert event.payload["formattable"] is False


# ── The RPC ───────────────────────────────────────────────────────────────────

class TestFormatRpc:

    @pytest.mark.asyncio
    async def test_rpc_reports_success(self, plugin):
        from sources.base import SourceType
        storage = MagicMock()
        storage.source_type = SourceType.STORAGE
        storage.source_id = "storage:udev"
        storage.format_media = AsyncMock(return_value=(True, None))
        plugin.source_manager.replace(storage)

        assert await plugin.format_media("/dev/sda") == {"success": True, "error": None}
        storage.format_media.assert_awaited_once_with("/dev/sda")

    @pytest.mark.asyncio
    async def test_rpc_passes_the_refusal_through(self, plugin):
        """The reason is what the toast shows — "not a removable drive" tells
        the user something; a bare failure does not."""
        from sources.base import SourceType
        storage = MagicMock()
        storage.source_type = SourceType.STORAGE
        storage.source_id = "storage:udev"
        storage.format_media = AsyncMock(return_value=(False, "not a removable drive"))
        plugin.source_manager.replace(storage)

        result = await plugin.format_media("/dev/sda")
        assert result == {"success": False, "error": "not a removable drive"}

    @pytest.mark.asyncio
    async def test_rpc_without_a_storage_source_does_not_raise(self, plugin):
        """Reached before _main finishes, or with storage disabled."""
        for source in list(plugin.source_manager.sources):
            if source.source_type.value == "storage":
                plugin.source_manager._sources.remove(source)
        result = await plugin.format_media("/dev/sda")
        assert result["success"] is False
