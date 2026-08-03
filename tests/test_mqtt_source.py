"""
test_mqtt_source.py — unit tests for MqttSource.

paho-mqtt is mocked so the suite runs without a broker.
"""
import json
import sys
import pytest
from unittest.mock import MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(settings=None):
    from sources.mqtt_source import MqttSource
    defaults = {
        "enabled": True,
        "broker_host": "localhost",
        "broker_port": 1883,
        "topic": "decky-links",
        # A secret is mandatory now: without one this source is an
        # unauthenticated remote trigger, so start() refuses and _on_message
        # drops everything. Tests that care about its absence set it to "".
        "secret": "s3cret",
    }
    if settings:
        defaults.update(settings)
    return MqttSource(defaults, logger=MagicMock())


def _make_paho_mock():
    """Return (paho_pkg, paho_mqtt_pkg, paho_client_mod, client_instance).

    `import paho.mqtt.client as mqtt` traverses attributes from the top-level
    package, so the attribute chain mock_paho.mqtt.client must point to the
    same object we inject into sys.modules["paho.mqtt.client"].
    """
    mock_client_instance = MagicMock()
    mock_paho_client_mod = MagicMock()
    mock_paho_client_mod.Client.return_value = mock_client_instance
    mock_paho_mqtt = MagicMock()
    mock_paho_mqtt.client = mock_paho_client_mod
    mock_paho = MagicMock()
    mock_paho.mqtt = mock_paho_mqtt
    return mock_paho, mock_paho_mqtt, mock_paho_client_mod, mock_client_instance


def _paho_sys_modules(mock_paho, mock_paho_mqtt, mock_paho_client_mod):
    return {
        "paho": mock_paho,
        "paho.mqtt": mock_paho_mqtt,
        "paho.mqtt.client": mock_paho_client_mod,
    }


def _make_mqtt_msg(payload: bytes, topic: str = "decky-links"):
    msg = MagicMock()
    msg.topic = topic
    msg.payload = payload
    return msg


def _signed(body: dict, secret: str = "s3cret"):
    """An mqtt message carrying the shared secret _make_source expects."""
    return _make_mqtt_msg(json.dumps({**body, "secret": secret}).encode())


# ── source_id ─────────────────────────────────────────────────────────────────

class TestProperties:

    def test_source_id_includes_host_port_topic(self):
        src = _make_source({
            "enabled": True,
            "broker_host": "192.168.1.10",
            "broker_port": 1883,
            "topic": "my/topic",
        })
        assert src.source_id == "mqtt:192.168.1.10:1883/my/topic"

    def test_poll_interval_is_fast(self):
        src = _make_source()
        assert src.poll_interval <= 0.5


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:

    @pytest.mark.asyncio
    async def test_start_returns_false_when_disabled(self):
        src = _make_source({"enabled": False})
        ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_paho_missing(self):
        src = _make_source()
        with patch.dict(sys.modules, {
            "paho": None,
            "paho.mqtt": None,
            "paho.mqtt.client": None,
        }):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_connects_and_subscribes(self):
        src = _make_source()
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok is True
        assert src.is_active()
        mock_client.connect.assert_called_once_with("localhost", 1883, keepalive=60)
        mock_client.subscribe.assert_called_once_with("decky-links")
        mock_client.loop_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_returns_false_on_connect_error(self):
        src = _make_source()
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        mock_client.connect.side_effect = ConnectionRefusedError("refused")
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_start_refuses_without_a_shared_secret(self):
        """With no secret this source is an unauthenticated remote trigger:
        anything able to publish to the topic can launch games on the device.
        It was empty by default and one toggle away."""
        src = _make_source({"secret": ""})
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok is False
        assert not src.is_active()
        mock_client.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_broker_credentials_are_applied_when_set(self):
        src = _make_source({"username": "deck", "password": "hunter2"})
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            await src.start()
        mock_client.username_pw_set.assert_called_once_with("deck", "hunter2")

    @pytest.mark.asyncio
    async def test_no_credentials_sent_when_unset(self):
        src = _make_source()
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            await src.start()
        mock_client.username_pw_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_tls_is_enabled_when_requested(self):
        src = _make_source({"tls": True})
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok is True
        mock_client.tls_set.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_fails_rather_than_falling_back_to_plaintext(self):
        """Asking for TLS and silently connecting without it is the one
        outcome the user cannot detect."""
        src = _make_source({"tls": True})
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        mock_client.tls_set.side_effect = RuntimeError("no ca bundle")
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok is False
        mock_client.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_registers_on_message_callback(self):
        src = _make_source()
        mock_paho, mock_mqtt, mock_client_mod, mock_client = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            await src.start()
        assert mock_client.on_message == src._on_message


# ── stop() ────────────────────────────────────────────────────────────────────

class TestStop:

    @pytest.mark.asyncio
    async def test_stop_disconnects_client(self):
        src = _make_source()
        mock_client = MagicMock()
        src._client = mock_client
        src._active = True
        await src.stop()
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()
        assert src._client is None
        assert not src.is_active()

    @pytest.mark.asyncio
    async def test_stop_clears_pending(self):
        src = _make_source()
        src._active = True
        src._pending.append("steam://run/1")
        await src.stop()
        assert len(src._pending) == 0

    @pytest.mark.asyncio
    async def test_stop_tolerates_disconnect_error(self):
        src = _make_source()
        mock_client = MagicMock()
        mock_client.disconnect.side_effect = RuntimeError("already gone")
        src._client = mock_client
        src._active = True
        await src.stop()   # should not raise
        assert not src.is_active()


# ── poll() ────────────────────────────────────────────────────────────────────

class TestPoll:

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_inactive(self):
        src = _make_source()
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_no_pending(self):
        src = _make_source()
        src._active = True
        result = await src.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_returns_load_event_from_pending(self):
        from sources.base import MediaEventKind, SourceType
        src = _make_source()
        src._active = True
        src._pending.append("steam://run/12345")
        result = await src.poll()
        assert result is not None
        assert result.kind == MediaEventKind.LOAD
        assert result.source_type == SourceType.MQTT
        assert result.uri == "steam://run/12345"
        assert result.media_id == "steam://run/12345"

    @pytest.mark.asyncio
    async def test_poll_drains_pending_fifo(self):
        src = _make_source()
        src._active = True
        src._pending.append("steam://run/1")
        src._pending.append("steam://run/2")
        r1 = await src.poll()
        r2 = await src.poll()
        assert r1.uri == "steam://run/1"
        assert r2.uri == "steam://run/2"


# ── _on_message() ─────────────────────────────────────────────────────────────

class TestOnMessage:

    def test_valid_message_enqueued(self):
        src = _make_source()
        src._on_message(None, None, _signed({"uri": "steam://run/999"}))
        assert list(src._pending) == ["steam://run/999"]

    def test_missing_uri_rejected(self):
        src = _make_source()
        msg = _make_mqtt_msg(json.dumps({"title": "Game"}).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_empty_uri_rejected(self):
        src = _make_source()
        msg = _make_mqtt_msg(json.dumps({"uri": ""}).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_invalid_json_rejected(self):
        src = _make_source()
        msg = _make_mqtt_msg(b"not json {{{")
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_non_string_uri_rejected(self):
        src = _make_source()
        msg = _make_mqtt_msg(json.dumps({"uri": 12345}).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_correct_secret_accepted(self):
        src = _make_source({"secret": "my-secret", "enabled": True})
        msg = _make_mqtt_msg(json.dumps(
            {"uri": "steam://run/1", "secret": "my-secret"}
        ).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 1

    def test_wrong_secret_rejected(self):
        src = _make_source({"secret": "correct", "enabled": True})
        msg = _make_mqtt_msg(json.dumps(
            {"uri": "steam://run/1", "secret": "wrong"}
        ).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_missing_secret_rejected_when_required(self):
        src = _make_source({"secret": "required", "enabled": True})
        msg = _make_mqtt_msg(json.dumps({"uri": "steam://run/1"}).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_no_secret_configured_rejects_everything(self):
        """Fail closed. start() refuses without a secret, so an empty one here
        means it was cleared underneath us — reverting to accepting anything
        would turn a cleared field into an open remote trigger."""
        src = _make_source({"secret": "", "enabled": True})
        msg = _make_mqtt_msg(json.dumps({"uri": "steam://run/1"}).encode())
        src._on_message(None, None, msg)
        assert len(src._pending) == 0

    def test_message_without_the_secret_is_rejected(self):
        src = _make_source()
        src._on_message(None, None, _make_mqtt_msg(
            json.dumps({"uri": "steam://run/1"}).encode()
        ))
        assert len(src._pending) == 0

    def test_message_with_the_wrong_secret_is_rejected(self):
        src = _make_source()
        src._on_message(None, None, _signed({"uri": "steam://run/1"}, "wrong"))
        assert len(src._pending) == 0

    def test_non_string_secret_is_rejected(self):
        """compare_digest raises on a non-str, which would surface as an
        exception on paho's thread rather than a rejection."""
        src = _make_source()
        src._on_message(None, None, _make_mqtt_msg(
            json.dumps({"uri": "steam://run/1", "secret": 12345}).encode()
        ))
        assert len(src._pending) == 0

    def test_multiple_messages_queued(self):
        src = _make_source()
        for i in range(3):
            src._on_message(None, None, _signed({"uri": f"steam://run/{i}"}))
        assert len(src._pending) == 3


# ── _on_disconnect() ──────────────────────────────────────────────────────────

class TestOnDisconnect:

    def test_disconnect_marks_inactive(self):
        src = _make_source()
        src._active = True
        src._on_disconnect(None, None, 1)
        assert not src.is_active()


# ── Integration ───────────────────────────────────────────────────────────────

class TestIntegration:

    @pytest.mark.asyncio
    async def test_message_to_poll_cycle(self):
        from sources.base import MediaEventKind
        src = _make_source()
        mock_paho, mock_mqtt, mock_client_mod, _mc = _make_paho_mock()
        with patch.dict(sys.modules, _paho_sys_modules(mock_paho, mock_mqtt, mock_client_mod)):
            ok = await src.start()
        assert ok

        # Simulate message arriving on paho's thread
        src._on_message(None, None, _signed({"uri": "steam://run/7"}))

        event = await src.poll()
        assert event is not None
        assert event.kind == MediaEventKind.LOAD
        assert event.uri == "steam://run/7"

        # No more pending
        assert await src.poll() is None


# ── Buffer bounds ─────────────────────────────────────────────────────────────

class TestPendingBufferIsBounded:
    """poll() drains one message per 0.1s cycle — a hard 10/s ceiling — while
    paho appends from its own thread as fast as the broker sends. Unbounded,
    that is a publisher-controlled memory leak on a device with no headroom."""

    def test_buffer_does_not_grow_without_limit(self):
        src = _make_source()
        for i in range(src.MAX_PENDING * 3):
            src._on_message(None, None, _signed({"uri": f"https://example.com/{i}"}))
        assert len(src._pending) == src.MAX_PENDING

    def test_oldest_are_dropped_so_the_newest_intent_survives(self):
        """This is a trigger: the most recent message is the one worth acting
        on, and a backlog of stale ones is what should be shed."""
        src = _make_source()
        for i in range(src.MAX_PENDING + 10):
            src._on_message(None, None, _signed({"uri": f"https://example.com/{i}"}))
        assert src._pending[-1] == f"https://example.com/{src.MAX_PENDING + 9}"
        assert "https://example.com/0" not in src._pending

    def test_drops_are_counted_and_reported(self):
        """deque(maxlen) discards silently, and a trigger that vanishes with no
        trace is indistinguishable from a broken broker."""
        logger = MagicMock()
        src = _make_source()
        src._logger = logger
        for i in range(src.MAX_PENDING + 5):
            src._on_message(None, None, _signed({"uri": f"https://example.com/{i}"}))
        assert src._dropped == 5
        assert any("buffer full" in str(c) for c in logger.warning.call_args_list)
