"""
test_event_names.py — the frontend event contract.

Three event names were NFC-shaped because NFC was the only source when they
were written. They now carry storage, camera, MQTT, serial and file-watch
events too, where "tag" and "reader" describe nothing that exists — a floppy
insert arrived as `tag_detected` with a device node in the `uid` field.

  tag_detected  → media_detected
  tag_removed   → media_removed
  reader_status → source_connection

The compatibility shim that emitted both names is gone: the plugin has not
shipped, so there is no installed frontend anywhere holding the old names. What
these tests protect is that the old names do not creep back in — the backend and
the panel agree by string literal and nothing type-checks across that boundary,
so a stray `tag_detected` fails silently and looks like dead hardware.
"""
import pytest
from unittest.mock import patch

from sources.base import (
    MediaEvent,
    MediaEventKind,
    SourceEvent,
    SourceEventKind,
    SourceType,
)


RETIRED_NAMES = ("tag_detected", "tag_removed", "reader_status")


def _emitted(mock_decky):
    """Every event name emitted, in order."""
    return [c.args[0] for c in mock_decky.emit.call_args_list]


def _payloads_for(mock_decky, name):
    return [c.args[1] for c in mock_decky.emit.call_args_list if c.args[0] == name]


# ── The retired names are gone for good ───────────────────────────────────────

class TestRetiredNames:

    def test_backend_never_emits_a_retired_name(self):
        """A stray emit of an old name reaches no listener and raises nothing —
        it just looks like the medium was never detected."""
        import inspect
        import main

        source = inspect.getsource(main)
        for name in RETIRED_NAMES:
            assert f'"{name}"' not in source, f"{name} is still emitted by main.py"

    def test_frontend_never_listens_for_a_retired_name(self):
        """The other half of the same contract. A listener on a name nothing
        emits is a panel row that never updates."""
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for path in src.rglob("*.ts*"):
            text = path.read_text(encoding="utf-8")
            for name in RETIRED_NAMES:
                if f'"{name}"' in text:
                    offenders.append(f"{path.name}: {name}")
        assert not offenders, f"retired event names still referenced: {offenders}"

    def test_the_shim_is_gone(self):
        """Both names were emitted during the rename. Removed once it was clear
        no installed frontend existed to protect — the plugin has not shipped."""
        import main

        assert not hasattr(main, "LEGACY_EVENT_NAMES")
        assert not hasattr(main, "emit"), (
            "the emit wrapper existed only to add the alias; without it, "
            "decky.emit is the whole story"
        )


# ── The current names, from the real handler paths ────────────────────────────

class TestHandlersEmitCurrentNames:

    @pytest.mark.asyncio
    async def test_media_load_emits_media_detected(self, plugin, mock_decky):
        event = MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
            media_id="DEADBEEF",
            uri="steam://rungameid/400",
            payload={},
        )
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(event)

        assert "media_detected" in _emitted(mock_decky)

    @pytest.mark.asyncio
    async def test_media_unload_emits_media_removed(self, plugin, mock_decky):
        from main import PluginState
        plugin.state = PluginState.READY
        plugin.current_tag_uid = "DEADBEEF"

        event = MediaEvent(
            kind=MediaEventKind.UNLOAD,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
            media_id="DEADBEEF",
            uri=None,
        )
        await plugin._handle_media_unload(event)

        assert "media_removed" in _emitted(mock_decky)

    @pytest.mark.asyncio
    async def test_reader_connect_emits_source_connection(self, plugin, mock_decky):
        from main import PluginState
        plugin.state = PluginState.IDLE

        await plugin._handle_source_event(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
        ))

        payload, = _payloads_for(mock_decky, "source_connection")
        assert payload["connected"] is True
        assert payload["source_type"] == "nfc"

    @pytest.mark.asyncio
    async def test_storage_connect_emits_no_connection_event(self, plugin, mock_decky):
        """The rename did not widen what emits it. This is still the NFC
        reader's connection state, under a name that no longer implies the
        payload describes a tag."""
        await plugin._handle_source_event(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
        ))

        assert "source_connection" not in _emitted(mock_decky)


# ── The retired RPC ───────────────────────────────────────────────────────────

class TestGetTagStatusIsGone:

    def test_rpc_no_longer_exists(self, plugin):
        """It reported a single global slot that whichever source presented
        media last overwrote — a tag and a disk could not both be present.
        get_active_media is the per-source replacement."""
        assert not hasattr(plugin, "get_tag_status")

    def test_its_cache_went_with_it(self, plugin):
        """The 100ms cache existed solely because the frontend polled the RPC
        twice a second."""
        for attr in ("_tag_status_lock", "_last_tag_status_query", "_tag_status_cache"):
            assert not hasattr(plugin, attr), f"{attr} outlived get_tag_status"

    @pytest.mark.asyncio
    async def test_get_active_media_reports_every_source_separately(self, plugin):
        """What get_tag_status structurally could not say."""
        with patch.object(plugin, "_play_sound"):
            await plugin._handle_media_load(MediaEvent(
                kind=MediaEventKind.LOAD,
                source_type=SourceType.NFC,
                source_id="nfc:/dev/ttyUSB0",
                media_id="DEADBEEF",
                uri="steam://rungameid/400",
                payload={},
            ))
            await plugin._handle_media_load(MediaEvent(
                kind=MediaEventKind.LOAD,
                source_type=SourceType.STORAGE,
                source_id="storage:udev",
                media_id="/dev/sda1",
                uri="steam://rungameid/500",
                payload={},
            ))

        media = await plugin.get_active_media()
        by_source = {m["source_id"]: m for m in media}
        assert by_source["nfc:/dev/ttyUSB0"]["uri"] == "steam://rungameid/400"
        assert by_source["storage:udev"]["uri"] == "steam://rungameid/500"
