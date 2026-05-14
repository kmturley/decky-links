"""MQTT virtual trigger source.

Subscribes to a configurable MQTT topic and emits a ``MediaEvent(LOAD)``
for each message whose JSON payload contains a ``uri`` field.

Opt-in only — disabled by default via ``settings["enabled"] = False``.
Supports an optional shared-secret check: if ``settings["secret"]`` is
non-empty, incoming messages must carry a matching ``"secret"`` field or
they are silently rejected.

Thread safety: paho-mqtt delivers messages on a background thread.  The
``_pending`` deque is used for cross-thread communication; CPython's GIL
makes single-item ``append``/``popleft`` operations on ``deque`` atomic.
"""

import json
import traceback
from collections import deque
from typing import Optional

from sources.base import (
    MediaEvent,
    MediaEventKind,
    MediaSource,
    PluginEvent,
    SourceType,
)


class MqttSource(MediaSource):
    """MQTT push-trigger source.

    Each received message that passes validation produces a LOAD event.
    There is no paired UNLOAD — MQTT is a one-shot trigger.
    """

    source_type = SourceType.MQTT

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._client = None
        self._pending: deque = deque()
        self._active = False

    @property
    def source_id(self) -> str:
        host = self._settings.get("broker_host", "localhost")
        port = self._settings.get("broker_port", 1883)
        topic = self._settings.get("topic", "decky-links")
        return f"mqtt:{host}:{port}/{topic}"

    @property
    def poll_interval(self) -> float:
        return 0.1  # drain queue quickly; paho does actual I/O on its own thread

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Connect to broker and subscribe to topic."""
        if not self._settings.get("enabled", False):
            return False

        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            if self._logger:
                self._logger.warning(
                    "MqttSource: paho-mqtt not available — MQTT source disabled"
                )
            return False

        host = self._settings.get("broker_host", "localhost")
        port = int(self._settings.get("broker_port", 1883))
        topic = self._settings.get("topic", "decky-links")

        client = mqtt.Client()
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(host, port, keepalive=60)
            client.subscribe(topic)
            client.loop_start()
            self._client = client
            self._active = True
            if self._logger:
                self._logger.info(
                    f"MqttSource: connected to {host}:{port} topic={topic}"
                )
            return True
        except Exception as e:
            if self._logger:
                self._logger.error(f"MqttSource: connection failed: {e}")
            return False

    async def stop(self) -> None:
        """Disconnect and release resources."""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._active = False
        self._pending.clear()

    def is_active(self) -> bool:
        return self._active

    # ── Poll ───────────────────────────────────────────────────────────

    async def poll(self) -> Optional[PluginEvent]:
        """Drain one pending message and return a LOAD event."""
        if not self._active or not self._pending:
            return None
        uri = self._pending.popleft()
        if self._logger:
            self._logger.info(f"MqttSource: trigger uri={uri}")
        return MediaEvent(
            kind=MediaEventKind.LOAD,
            source_type=SourceType.MQTT,
            source_id=self.source_id,
            media_id=uri,
            uri=uri,
        )

    # ── paho callbacks (background thread) ────────────────────────────

    def _on_message(self, client, userdata, msg):
        """Called by paho on its I/O thread when a message arrives."""
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            if self._logger:
                self._logger.warning(f"MqttSource: bad message payload: {e}")
            return

        uri = data.get("uri", "")
        if not isinstance(uri, str) or not uri:
            if self._logger:
                self._logger.warning("MqttSource: message missing 'uri' field")
            return

        secret = self._settings.get("secret", "")
        if secret and data.get("secret") != secret:
            if self._logger:
                self._logger.warning("MqttSource: message rejected (wrong secret)")
            return

        self._pending.append(uri)

    def _on_disconnect(self, client, userdata, rc):
        """Called by paho when the broker connection drops."""
        if self._logger:
            self._logger.warning(f"MqttSource: disconnected (rc={rc})")
        self._active = False
