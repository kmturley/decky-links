"""
test_event_names.py — the frontend event contract, and its compatibility shim.

Three event names were NFC-shaped because NFC was the only source when they
were written. They now carry storage, camera, MQTT, serial and file-watch
events too, where "tag" and "reader" describe nothing that exists — a floppy
insert arrived as `tag_detected` with a device node in the `uid` field.

Both names are emitted during the transition. The two halves of the plugin ship
in one zip, so a deploy cannot skew them — but the frontend bundle lives in the
Steam UI process and outlives a plugin_loader restart, which is the normal
development loop. These tests pin the shim so that window stays covered until
it is deliberately removed.
"""
import pytest
from unittest.mock import MagicMock, patch

from sources.base import (
    MediaEvent,
    MediaEventKind,
    SourceEvent,
    SourceEventKind,
    SourceType,
)


def _emitted(mock_decky):
    """Every event name emitted, in order."""
    return [c.args[0] for c in mock_decky.emit.call_args_list]


def _payloads_for(mock_decky, name):
    return [c.args[1] for c in mock_decky.emit.call_args_list if c.args[0] == name]


# ── The shim itself ───────────────────────────────────────────────────────────

class TestEmitAlias:

    @pytest.mark.asyncio
    async def test_aliased_event_emits_both_names(self, mock_decky):
        import main

        await main.emit("media_detected", {"uid": "DEADBEEF"})

        assert _emitted(mock_decky) == ["media_detected", "tag_detected"]

    @pytest.mark.asyncio
    async def test_both_names_carry_the_identical_payload(self, mock_decky):
        """A stale frontend has to behave exactly as it did before the rename,
        which means the alias cannot be a reduced or reordered payload."""
        import main

        payload = {"uid": "DEADBEEF", "source_type": "nfc", "source_id": "nfc:0"}
        await main.emit("media_detected", payload)

        new, = _payloads_for(mock_decky, "media_detected")
        old, = _payloads_for(mock_decky, "tag_detected")
        assert new == old == payload
        assert new is old, "the alias must not be a copy that can drift"

    @pytest.mark.asyncio
    async def test_unaliased_event_emits_once(self, mock_decky):
        """Only the three renamed events are duplicated. Doubling every event
        would double the RPC traffic this refactor spent Phase E reducing."""
        import main

        await main.emit("uri_detected", {"uri": None, "uid": "DEADBEEF"})

        assert _emitted(mock_decky) == ["uri_detected"]

    def test_alias_map_covers_every_renamed_event(self):
        import main

        assert main.LEGACY_EVENT_NAMES == {
            "media_detected": "tag_detected",
            "media_removed": "tag_removed",
            "source_connection": "reader_status",
        }

    def test_no_emit_site_still_uses_a_legacy_name_directly(self):
        """The shim only works if every site goes through main.emit. A stray
        `decky.emit("tag_detected", ...)` would emit the old name alone, and
        the migrated frontend would never see it."""
        import inspect
        import main

        source = inspect.getsource(main)
        for legacy in main.LEGACY_EVENT_NAMES.values():
            assert f'decky.emit("{legacy}"' not in source, (
                f"{legacy} is emitted directly; it must go through main.emit "
                f"so the new name is emitted too"
            )


# ── The real handler paths ────────────────────────────────────────────────────

class TestHandlersEmitBothNames:

    @pytest.mark.asyncio
    async def test_media_load_emits_media_detected_and_tag_detected(self, plugin, mock_decky):
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

        emitted = _emitted(mock_decky)
        assert "media_detected" in emitted
        assert "tag_detected" in emitted

    @pytest.mark.asyncio
    async def test_media_unload_emits_media_removed_and_tag_removed(self, plugin, mock_decky):
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

        emitted = _emitted(mock_decky)
        assert "media_removed" in emitted
        assert "tag_removed" in emitted

    @pytest.mark.asyncio
    async def test_reader_connect_emits_source_connection_and_reader_status(self, plugin, mock_decky):
        from main import PluginState
        plugin.state = PluginState.IDLE

        await plugin._handle_source_event(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.NFC,
            source_id="nfc:/dev/ttyUSB0",
        ))

        new, = _payloads_for(mock_decky, "source_connection")
        old, = _payloads_for(mock_decky, "reader_status")
        assert new["connected"] is True
        assert new == old

    @pytest.mark.asyncio
    async def test_storage_connect_emits_neither(self, plugin, mock_decky):
        """The rename does not widen what emits it. This is still the NFC
        reader's connection state, under a name that no longer implies the
        payload describes a tag."""
        await plugin._handle_source_event(SourceEvent(
            kind=SourceEventKind.CONNECTED,
            source_type=SourceType.STORAGE,
            source_id="storage:udev",
        ))

        emitted = _emitted(mock_decky)
        assert "source_connection" not in emitted
        assert "reader_status" not in emitted


# ── The retired RPC ───────────────────────────────────────────────────────────

class TestGetTagStatusIsGone:

    def test_rpc_no_longer_exists(self, plugin):
        """It reported a single global slot that whichever source presented
        media last overwrote — a tag and a disk could not both be present.
        get_active_media is the per-source replacement, and nothing has called
        the old RPC since E5 removed the frontend's copy of that model."""
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
