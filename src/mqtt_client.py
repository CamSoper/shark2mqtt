"""MQTT client with Home Assistant autodiscovery and command handling."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import aiomqtt

if TYPE_CHECKING:
    from .config import Settings
    from .shark_device import SharkVacuum

logger = logging.getLogger(__name__)

# HA discovery prefix (standard)
HA_DISCOVERY_PREFIX = "homeassistant"


class MqttClient:
    """Async MQTT client for shark2mqtt."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._prefix = config.mqtt_prefix
        self._client: aiomqtt.Client | None = None

    async def __aenter__(self) -> MqttClient:
        will = aiomqtt.Will(
            topic=f"{self._prefix}/status",
            payload=json.dumps({"state": "offline"}),
            qos=1,
            retain=True,
        )
        self._client = aiomqtt.Client(
            hostname=self._config.mqtt_host,
            port=self._config.mqtt_port,
            username=self._config.mqtt_username,
            password=self._config.mqtt_password,
            will=will,
        )
        await self._client.__aenter__()
        # Announce online
        await self._publish(f"{self._prefix}/status", {"state": "online"}, retain=True)
        logger.info("MQTT connected to %s:%d", self._config.mqtt_host, self._config.mqtt_port)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._publish(f"{self._prefix}/status", {"state": "offline"}, retain=True)
            await self._client.__aexit__(*args)
            self._client = None

    async def _publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        assert self._client is not None
        msg = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        await self._client.publish(topic, msg, qos=1, retain=retain)

    # --- HA Autodiscovery ---

    async def publish_discovery(self, device: SharkVacuum) -> None:
        """Publish HA MQTT autodiscovery configs for a vacuum and its sensors."""
        dsn = device.dsn
        uid = f"shark2mqtt_{dsn}"

        # Vacuum entity
        await self._publish(
            f"{HA_DISCOVERY_PREFIX}/vacuum/{uid}/config",
            {
                "name": device.product_name,
                "unique_id": uid,
                "object_id": uid,
                "state_topic": f"{self._prefix}/{dsn}/state",
                "json_attributes_topic": f"{self._prefix}/{dsn}/attributes",
                "command_topic": f"{self._prefix}/{dsn}/command",
                "set_fan_speed_topic": f"{self._prefix}/{dsn}/set_fan_speed",
                "fan_speed_list": ["eco", "normal", "max"],
                "supported_features": [
                    "start", "stop", "pause", "return_home",
                    "locate", "fan_speed", "status",
                ],
                "availability_topic": f"{self._prefix}/{dsn}/available",
                "payload_available": "online",
                "payload_not_available": "offline",
                "value_template": "{{ value_json.state }}",
                "device": device.device_info,
            },
            retain=True,
        )

        # Battery sensor
        await self._publish(
            f"{HA_DISCOVERY_PREFIX}/sensor/{uid}_battery/config",
            {
                "name": f"{device.product_name} Battery",
                "unique_id": f"{uid}_battery",
                "object_id": f"{uid}_battery",
                "state_topic": f"{self._prefix}/{dsn}/attributes",
                "value_template": "{{ value_json.battery_level }}",
                "unit_of_measurement": "%",
                "device_class": "battery",
                "state_class": "measurement",
                "availability_topic": f"{self._prefix}/{dsn}/available",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device.device_info,
            },
            retain=True,
        )

        # RSSI sensor
        await self._publish(
            f"{HA_DISCOVERY_PREFIX}/sensor/{uid}_rssi/config",
            {
                "name": f"{device.product_name} WiFi Signal",
                "unique_id": f"{uid}_rssi",
                "object_id": f"{uid}_rssi",
                "state_topic": f"{self._prefix}/{dsn}/attributes",
                "value_template": "{{ value_json.rssi }}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "availability_topic": f"{self._prefix}/{dsn}/available",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device.device_info,
            },
            retain=True,
        )

        # Charging binary sensor
        await self._publish(
            f"{HA_DISCOVERY_PREFIX}/binary_sensor/{uid}_charging/config",
            {
                "name": f"{device.product_name} Charging",
                "unique_id": f"{uid}_charging",
                "object_id": f"{uid}_charging",
                "state_topic": f"{self._prefix}/{dsn}/attributes",
                "value_template": "{{ value_json.is_charging }}",
                "payload_on": True,
                "payload_off": False,
                "device_class": "battery_charging",
                "availability_topic": f"{self._prefix}/{dsn}/available",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device.device_info,
            },
            retain=True,
        )

        logger.info("Published HA discovery for %s (%s)", device.product_name, dsn)

    # --- State publishing ---

    async def publish_state(self, device: SharkVacuum) -> None:
        """Publish device state, attributes, and availability."""
        dsn = device.dsn
        # Always report available when we have data — Ayla's connection_status
        # just means the vacuum's WiFi is asleep, not that state is stale.
        available = "online"

        await self._publish(f"{self._prefix}/{dsn}/state", device.to_state_payload(), retain=True)
        await self._publish(f"{self._prefix}/{dsn}/attributes", device.to_attributes_payload(), retain=True)
        await self._publish(f"{self._prefix}/{dsn}/available", available, retain=True)

    async def publish_unavailable(self, devices: list[SharkVacuum]) -> None:
        """Mark all devices as unavailable."""
        for device in devices:
            await self._publish(f"{self._prefix}/{device.dsn}/available", "offline", retain=True)

    async def publish_status(self, status: dict[str, Any]) -> None:
        """Publish auth/system status."""
        await self._publish(f"{self._prefix}/status", status, retain=True)

    # --- Command handling ---

    async def command_listener(
        self,
        command_handler: Any,
        devices: dict[str, SharkVacuum],
    ) -> None:
        """Subscribe to command topics and dispatch via handler.

        command_handler must implement:
          send_command(device_id, command) -> None
          set_fan_speed(device_id, speed) -> None
        """
        assert self._client is not None

        await self._client.subscribe(f"{self._prefix}/+/command")
        await self._client.subscribe(f"{self._prefix}/+/set_fan_speed")
        await self._client.subscribe(f"{self._prefix}/+/clean_rooms")

        async for message in self._client.messages:
            topic = message.topic.value
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            device_id = self._extract_dsn(topic)

            if not device_id:
                continue

            if device_id not in devices:
                logger.warning("Command for unknown device: %s", device_id)
                continue

            try:
                if topic.endswith("/command"):
                    command = payload.strip().lower()
                    logger.info("Command received: %s for %s", command, device_id)
                    await command_handler.send_command(device_id, command)
                elif topic.endswith("/set_fan_speed"):
                    speed = payload.strip().lower()
                    logger.info("Fan speed received: %s for %s", speed, device_id)
                    await command_handler.set_fan_speed(device_id, speed)
                elif topic.endswith("/clean_rooms"):
                    logger.info("Clean rooms received for %s", device_id)
                    await self._handle_clean_rooms(command_handler, device_id, payload)
            except Exception:
                logger.exception("Failed to handle command on %s", topic)

    @staticmethod
    async def _handle_clean_rooms(handler: Any, device_id: str, payload: str) -> None:
        """Handle a room cleaning command.

        Payload is JSON:
        {
            "rooms": ["Kitchen", "Den"],
            "floor_id": "2A38EFA6",
            "mode": "UserRoom",        // optional, default "UserRoom"
            "clean_count": 1,           // optional, default 1 (2 = matrix)
            "clean_type": "dry"         // optional, default "dry"
        }
        """
        import json as _json
        data = _json.loads(payload)
        rooms = data.get("rooms", [])
        floor_id = data.get("floor_id", "")
        if not rooms or not floor_id:
            logger.warning("clean_rooms requires 'rooms' and 'floor_id'")
            return
        await handler.clean_rooms(
            device_id,
            rooms=rooms,
            floor_id=floor_id,
            clean_type=data.get("clean_type", "dry"),
            clean_count=data.get("clean_count", 1),
            mode=data.get("mode", "UserRoom"),
        )

    def _extract_dsn(self, topic: str) -> str | None:
        """Extract DSN from topic like 'shark2mqtt/{dsn}/command'."""
        parts = topic.split("/")
        if len(parts) >= 3 and parts[0] == self._prefix:
            return parts[1]
        return None
