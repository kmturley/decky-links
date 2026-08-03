"""MQTT virtual trigger source.

Subscribes to a configurable MQTT topic and emits a ``MediaEvent(LOAD)``
for each message whose JSON payload contains a ``uri`` field.

Opt-in only — disabled by default via ``settings["enabled"] = False``, and
it will not start without a shared secret. Every message must carry a
``"secret"`` field matching ``settings["secret"]``; anything else is dropped.
That is not optional because anything able to publish to the topic can launch
games and open web pages on the device.

TLS and broker credentials are available via ``settings["tls"]``,
``["username"]`` and ``["password"]``. The secret authenticates the message;
TLS is what keeps it off the wire in clear and stops the broker being
impersonated.

Thread safety: paho-mqtt delivers messages on a background thread.  The
``_pending`` deque is used for cross-thread communication; CPython's GIL
makes single-item ``append``/``popleft`` operations on ``deque`` atomic.
"""

import hmac
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

    # Messages waiting to be drained. Bounded because poll() takes exactly one
    # per cycle at a 0.1s interval — a hard ceiling of 10/s — while paho's I/O
    # thread appends as fast as the broker sends. An unbounded deque there is a
    # publisher-controlled memory leak on a device with no headroom to spare.
    #
    # Oldest are dropped rather than newest: this is a trigger, so the most
    # recent intent is the one worth acting on, and a backlog of stale ones is
    # exactly what should be shed.
    MAX_PENDING = 100

    def __init__(self, settings: dict, logger=None):
        self._settings = settings
        self._logger = logger
        self._client = None
        self._pending: deque = deque(maxlen=self.MAX_PENDING)
        self._dropped = 0
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
        """Connect to broker and subscribe to topic.

        Refuses to start without a shared secret. Anything that publishes to
        the topic can launch games and open web pages on the device, so with
        an empty secret this source is an unauthenticated remote trigger for
        everyone who can reach the broker — and it was empty by default, one
        toggle away, with nothing saying so.
        """
        if not self._settings.get("enabled", False):
            return False

        secret = self._settings.get("secret", "")
        if not secret:
            if self._logger:
                self._logger.error(
                    "MqttSource: refusing to start without a shared secret. "
                    "Anyone able to publish to this topic could launch games on "
                    "this device. Set a secret in the panel, and have publishers "
                    "include it as a 'secret' field in the message."
                )
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

        username = self._settings.get("username", "")
        if username:
            client.username_pw_set(username, self._settings.get("password", "") or None)

        # The secret authenticates the *message*; TLS is what stops it being
        # read off the wire on the way, and stops the broker being
        # impersonated. Off by default only because a self-hosted broker on a
        # home LAN often has no certificate.
        if self._settings.get("tls", False):
            try:
                client.tls_set()
            except Exception as e:
                if self._logger:
                    self._logger.error(f"MqttSource: could not enable TLS: {e}")
                return False

        try:
            client.connect(host, port, keepalive=60)
            client.subscribe(topic)
            client.loop_start()
            self._client = client
            self._active = True
            if self._logger:
                self._logger.info(
                    f"MqttSource: connected to {host}:{port} topic={topic} "
                    f"(tls={bool(self._settings.get('tls', False))}, "
                    f"auth={'yes' if username else 'no'})"
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

        # start() refuses to run without a secret, so an empty one here means
        # the setting was cleared underneath us — fail closed rather than
        # reverting to accepting anything.
        secret = self._settings.get("secret", "")
        supplied = data.get("secret")
        if not secret or not isinstance(supplied, str) or not hmac.compare_digest(supplied, secret):
            if self._logger:
                self._logger.warning("MqttSource: message rejected (bad secret)")
            return

        # deque(maxlen=...) discards silently, and a trigger that vanishes with
        # no trace is indistinguishable from a broken broker. Count and report.
        if len(self._pending) == self._pending.maxlen:
            self._dropped += 1
            if self._logger and self._dropped % 100 == 1:
                self._logger.warning(
                    f"MqttSource: buffer full ({self._pending.maxlen}); dropping "
                    f"the oldest trigger. {self._dropped} dropped so far — the "
                    f"topic is publishing faster than 10/s, which is the rate "
                    f"this source can act on."
                )

        self._pending.append(uri)

    def _on_disconnect(self, client, userdata, rc):
        """Called by paho when the broker connection drops."""
        if self._logger:
            self._logger.warning(f"MqttSource: disconnected (rc={rc})")
        self._active = False
