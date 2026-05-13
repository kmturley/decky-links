"""
test_file_watch_source.py — unit tests for FileWatchSource.

All filesystem access is mocked so the suite is hermetic.
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    from sources.file_watch_source import FileWatchSource
    defaults = {"enabled": True, "watch_dir": "/tmp/decky-watch"}
    if settings:
        defaults.update(settings)
    return FileWatchSource(defaults, logger=MagicMock())


def _valid_payload(uri="steam://run/1", title="Game", icon=""):
    return {"version": 1, "uri": uri, "title": title, "icon": icon}


# ── source_id / poll_interval ─────────────────────────────────────────────────

class TestProperties:

    def test_source_id_includes_watch_dir(self):
        src = _make_source({"watch_dir": "/srv/triggers"})
        assert src.source_id == "file_watch:/srv/triggers"

    def test_poll_interval_from_settings(self):
        src = _make_source({"poll_interval": 5.0})
        assert src.poll_interval == 5.0

    def test_poll_interval_default(self):
        src = _make_source()
        assert src.poll_interval == 2.0

    def test_poll_interval_clamps_out_of_range(self):
        src = _make_source({"poll_interval": 0.01})
        assert src.poll_interval == 2.0


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:

    @pytest.mark.asyncio
    async def test_start_returns_false_when_disabled(self):
        src = _make_source({"enabled": False})
        ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_no_watch_dir(self):
        src = _make_source({"watch_dir": ""})
        ok = await src.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_start_returns_false_when_relative_path(self):
        src = _make_source({"watch_dir": "relative/path"})
        ok = await src.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_start_returns_false_when_dir_missing(self):
        src = _make_source({"watch_dir": "/no/such/dir"})
        with patch("os.path.isdir", return_value=False):
            ok = await src.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_start_returns_true_when_dir_exists(self):
        src = _make_source({"watch_dir": "/tmp/triggers"})
        with patch("os.path.isdir", return_value=True):
            ok = await src.start()
        assert ok is True
        assert src.is_active()


# ── stop() ────────────────────────────────────────────────────────────────────

class TestStop:

    @pytest.mark.asyncio
    async def test_stop_clears_active(self):
        src = _make_source()
        src._active = True
        await src.stop()
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_stop_clears_seen_and_pending(self):
        src = _make_source()
        src._active = True
        src._seen["game.json"] = "steam://run/1"
        src._pending.append("dummy")
        await src.stop()
        assert src._seen == {}
        assert len(src._pending) == 0


# ── poll() ────────────────────────────────────────────────────────────────────

class TestPoll:

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_inactive(self):
        src = _make_source()
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_drains_pending_before_scan(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        from sources.base import MediaEvent, MediaEventKind, SourceType
        evt = MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.FILE_WATCH,
            source_id=src.source_id,
            media_id="f.json",
            uri="steam://run/1",
        )
        src._pending.append(evt)
        with patch("os.listdir") as mock_ls:
            result = await src.poll()
        assert result is evt
        mock_ls.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_no_json_files(self):
        src = _make_source()
        src._active = True
        with patch("os.listdir", return_value=["readme.txt", "image.png"]):
            result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_emits_load_for_new_file(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source({"watch_dir": str(tmp_path)})
        src._active = True
        payload = _valid_payload("steam://run/42", "My Game")
        with patch("os.listdir", return_value=["game.json"]):
            with patch.object(src, "_read_payload", return_value=payload):
                result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.LOAD
        assert result.uri == "steam://run/42"
        assert result.media_id == "game.json"
        assert src._seen["game.json"] == "steam://run/42"

    @pytest.mark.asyncio
    async def test_poll_emits_unload_for_removed_file(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        src._seen["game.json"] = "steam://run/99"
        with patch("os.listdir", return_value=[]):  # file gone
            result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.UNLOAD
        assert result.uri == "steam://run/99"
        assert result.media_id == "game.json"
        assert "game.json" not in src._seen

    @pytest.mark.asyncio
    async def test_poll_ignores_file_without_valid_payload(self):
        src = _make_source()
        src._active = True
        with patch("os.listdir", return_value=["bad.json"]):
            with patch.object(src, "_read_payload", return_value=None):
                result = await src.poll()
        assert result is None
        assert "bad.json" not in src._seen

    @pytest.mark.asyncio
    async def test_poll_marks_inactive_on_listdir_error(self):
        src = _make_source()
        src._active = True
        with patch("os.listdir", side_effect=OSError("gone")):
            result = await src.poll()
        assert result is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_poll_buffers_multiple_new_files(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        payloads = {
            "a.json": _valid_payload("steam://run/1"),
            "b.json": _valid_payload("steam://run/2"),
        }
        with patch("os.listdir", return_value=["a.json", "b.json"]):
            with patch.object(src, "_read_payload",
                              side_effect=lambda p: payloads[os.path.basename(p)]):
                r1 = await src.poll()
        # First poll returns one, buffers second
        assert r1 is not None
        assert r1.kind == MediaEventKind.LOAD
        # Second poll drains buffer (no scan)
        with patch("os.listdir") as mock_ls:
            r2 = await src.poll()
        assert r2 is not None
        mock_ls.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_known_file_not_emitted_again(self):
        src = _make_source()
        src._active = True
        src._seen["game.json"] = "steam://run/1"
        with patch("os.listdir", return_value=["game.json"]):
            result = await src.poll()
        assert result is None  # already seen


# ── _read_payload() ───────────────────────────────────────────────────────────

class TestReadPayload:

    def test_valid_payload_returns_dict(self, tmp_path):
        src = _make_source()
        p = tmp_path / "trigger.json"
        p.write_text(json.dumps({
            "version": 1, "uri": "steam://run/1", "title": "T", "icon": "i",
        }))
        result = src._read_payload(str(p))
        assert result == {"version": 1, "uri": "steam://run/1", "title": "T", "icon": "i"}

    def test_missing_file_returns_none(self, tmp_path):
        src = _make_source()
        assert src._read_payload(str(tmp_path / "ghost.json")) is None

    def test_invalid_json_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert src._read_payload(str(p)) is None

    def test_wrong_version_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "v2.json"
        p.write_text(json.dumps({"version": 2, "uri": "steam://run/1"}))
        assert src._read_payload(str(p)) is None

    def test_missing_uri_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "no_uri.json"
        p.write_text(json.dumps({"version": 1}))
        assert src._read_payload(str(p)) is None

    def test_empty_uri_returns_none(self, tmp_path):
        src = _make_source()
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"version": 1, "uri": ""}))
        assert src._read_payload(str(p)) is None

    def test_optional_fields_default_empty(self, tmp_path):
        src = _make_source()
        p = tmp_path / "min.json"
        p.write_text(json.dumps({"version": 1, "uri": "https://example.com"}))
        result = src._read_payload(str(p))
        assert result["title"] == ""
        assert result["icon"] == ""

    def test_uri_excluded_from_poll_event_payload(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        payload = _valid_payload("steam://run/5", "My Game", "icon.png")
        with patch("os.listdir", return_value=["g.json"]):
            with patch.object(src, "_read_payload", return_value=payload):
                import asyncio
                event = asyncio.get_event_loop().run_until_complete(src.poll())
        assert "uri" not in event.payload
        assert event.payload["title"] == "My Game"


# ── Integration ───────────────────────────────────────────────────────────────

class TestIntegration:

    @pytest.mark.asyncio
    async def test_file_appear_then_disappear(self, tmp_path):
        from sources.base import MediaEventKind
        src = _make_source({"watch_dir": str(tmp_path)})
        with patch("os.path.isdir", return_value=True):
            await src.start()

        payload = _valid_payload("steam://run/7")
        # File appears
        with patch("os.listdir", return_value=["t.json"]):
            with patch.object(src, "_read_payload", return_value=payload):
                load = await src.poll()
        assert load.kind == MediaEventKind.LOAD
        assert load.uri == "steam://run/7"

        # File disappears
        with patch("os.listdir", return_value=[]):
            unload = await src.poll()
        assert unload.kind == MediaEventKind.UNLOAD
        assert unload.uri == "steam://run/7"

        assert src._seen == {}

    @pytest.mark.asyncio
    async def test_two_files_load_then_unload_both(self):
        from sources.base import MediaEventKind
        src = _make_source()
        src._active = True
        payloads = {
            "a.json": _valid_payload("steam://run/1"),
            "b.json": _valid_payload("steam://run/2"),
        }

        # Both appear
        with patch("os.listdir", return_value=["a.json", "b.json"]):
            with patch.object(src, "_read_payload",
                              side_effect=lambda p: payloads[os.path.basename(p)]):
                ev1 = await src.poll()
                ev2 = await src.poll()
        assert ev1.kind == MediaEventKind.LOAD
        assert ev2.kind == MediaEventKind.LOAD
        assert {ev1.uri, ev2.uri} == {"steam://run/1", "steam://run/2"}

        # Both disappear
        with patch("os.listdir", return_value=[]):
            ev3 = await src.poll()
            ev4 = await src.poll()
        assert ev3.kind == MediaEventKind.UNLOAD
        assert ev4.kind == MediaEventKind.UNLOAD

        assert src._seen == {}

    @pytest.mark.asyncio
    async def test_non_json_files_ignored(self):
        src = _make_source()
        src._active = True
        with patch("os.listdir", return_value=["readme.txt", "image.png", "script.sh"]):
            result = await src.poll()
        assert result is None
        assert src._seen == {}
