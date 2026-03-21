"""Shark vacuum device model — maps Ayla properties to HA-friendly state."""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    ERROR_CODES,
    OPERATING_MODE_TO_HA_STATE,
    POWER_MODE_NAMES,
    OperatingMode,
    PowerMode,
    PROP_GET_BATTERY_CAPACITY,
    PROP_GET_CHARGING_STATUS,
    PROP_GET_DEVICE_MODEL_NUMBER,
    PROP_GET_DOCKED_STATUS,
    PROP_GET_ERROR_CODE,
    PROP_GET_OPERATING_MODE,
    PROP_GET_POWER_MODE,
    PROP_GET_ROBOT_FIRMWARE_VERSION,
    PROP_GET_RSSI,
)

logger = logging.getLogger(__name__)


class SharkVacuum:
    """Represents a Shark robot vacuum with its current state."""

    def __init__(self, device_data: dict[str, Any]) -> None:
        self.dsn: str = device_data["dsn"]
        self.product_name: str = device_data.get("product_name", "Shark Robot")
        self.model: str = device_data.get("model", "Unknown")
        self.oem_model: str = device_data.get("oem_model", "")
        self.lan_ip: str = device_data.get("lan_ip", "")
        self.connection_status: str = device_data.get("connection_status", "Offline")
        self._properties: dict[str, Any] = {}

    def update_properties(self, properties: list[dict[str, Any]]) -> None:
        """Update device properties from Ayla API response.

        The Ayla properties response is a list of dicts, each with a
        "property" key containing {name, value, ...}.
        """
        for prop_wrapper in properties:
            prop = prop_wrapper.get("property", {})
            name = prop.get("name")
            value = prop.get("value")
            if name is not None:
                self._properties[name] = value

    def _get_prop(self, name: str, default: Any = None) -> Any:
        return self._properties.get(name, default)

    def _get_int_prop(self, name: str, default: int = 0) -> int:
        val = self._properties.get(name, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    # --- State properties ---

    @property
    def operating_mode(self) -> OperatingMode | None:
        val = self._get_int_prop(PROP_GET_OPERATING_MODE, -1)
        try:
            return OperatingMode(val)
        except ValueError:
            return None

    @property
    def is_docked(self) -> bool:
        return self._get_int_prop(PROP_GET_DOCKED_STATUS) == 1

    @property
    def error_code(self) -> int:
        return self._get_int_prop(PROP_GET_ERROR_CODE)

    @property
    def error_text(self) -> str:
        return ERROR_CODES.get(self.error_code, f"Unknown error ({self.error_code})")

    @property
    def ha_state(self) -> str:
        """Map device state to HA vacuum state string."""
        if self.error_code != 0:
            return "error"

        mode = self.operating_mode
        if mode is None:
            return "idle"

        # Docked + charging/idle takes priority over operating mode
        if self.is_docked and mode in (OperatingMode.STOP, OperatingMode.RETURN):
            return "docked"

        return OPERATING_MODE_TO_HA_STATE.get(mode, "idle")

    @property
    def battery_level(self) -> int:
        return self._get_int_prop(PROP_GET_BATTERY_CAPACITY)

    @property
    def is_charging(self) -> bool:
        return self._get_int_prop(PROP_GET_CHARGING_STATUS) == 1

    @property
    def power_mode(self) -> PowerMode | None:
        val = self._get_int_prop(PROP_GET_POWER_MODE, -1)
        try:
            return PowerMode(val)
        except ValueError:
            return None

    @property
    def fan_speed(self) -> str:
        mode = self.power_mode
        if mode is None:
            return "normal"
        return POWER_MODE_NAMES.get(mode, "normal")

    @property
    def rssi(self) -> int:
        return self._get_int_prop(PROP_GET_RSSI)

    @property
    def firmware_version(self) -> str:
        return str(self._get_prop(PROP_GET_ROBOT_FIRMWARE_VERSION, ""))

    @property
    def model_number(self) -> str:
        return str(self._get_prop(PROP_GET_DEVICE_MODEL_NUMBER, self.model))

    @property
    def is_online(self) -> bool:
        return self.connection_status == "Online"

    # --- MQTT payloads ---

    def to_state_payload(self) -> dict[str, Any]:
        """Payload for the state topic."""
        return {
            "state": self.ha_state,
            "fan_speed": self.fan_speed,
        }

    def to_attributes_payload(self) -> dict[str, Any]:
        """Payload for the attributes topic."""
        return {
            "battery_level": self.battery_level,
            "is_charging": self.is_charging,
            "error_code": self.error_code,
            "error_text": self.error_text,
            "rssi": self.rssi,
            "operating_mode": self.operating_mode.name if self.operating_mode else "unknown",
            "is_docked": self.is_docked,
            "firmware_version": self.firmware_version,
            "model_number": self.model_number,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """HA MQTT device info block."""
        return {
            "identifiers": [f"shark2mqtt_{self.dsn}"],
            "name": self.product_name,
            "manufacturer": "SharkNinja",
            "model": self.model_number,
            "sw_version": self.firmware_version,
        }
