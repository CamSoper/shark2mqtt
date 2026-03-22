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
        self.floor_id: str = ""
        self.rooms: list[str] = []

    @classmethod
    def from_skegox(cls, device_data: dict[str, Any]) -> SharkVacuum:
        """Create a SharkVacuum from skegox API response."""
        metadata = device_data.get("metadata", {})
        registry = device_data.get("registry", {})
        telemetry = device_data.get("telemetry", {})
        connectivity = device_data.get("connectivityStatus", {})
        shadow = device_data.get("shadow", {})
        props = shadow.get("properties", {})
        reported = props.get("reported", {})

        # Extract SND from registry Battery_Serial_Num (format: DSN-SND)
        bsn = registry.get("Battery_Serial_Num", "")
        snd = bsn.split("-")[-1] if "-" in bsn else bsn

        # Build a device_data dict compatible with the constructor
        compat = {
            "dsn": snd or device_data.get("deviceId", ""),
            "product_name": metadata.get("deviceName", "Shark Robot"),
            "model": registry.get("Device_Model_Number", "Unknown"),
            "oem_model": registry.get("Device_Serial_Num", ""),
            "connection_status": "Online" if connectivity.get("connected") else "Offline",
        }
        vac = cls(compat)

        # Populate properties from telemetry (real-time) and shadow (reported)
        # Telemetry has live battery/RSSI; shadow has operating mode, etc.
        for key, value in telemetry.items():
            vac._properties[f"GET_{key}"] = value

        for key, val_obj in reported.items():
            value = val_obj.get("value", val_obj) if isinstance(val_obj, dict) else val_obj
            vac._properties[f"GET_{key}"] = value

        # Also set firmware from registry
        fw = registry.get("FW_VERSION", "")
        if fw:
            vac._properties[PROP_GET_ROBOT_FIRMWARE_VERSION] = fw

        # Parse room list from Robot_Room_List (format: "FloorID:Room1:Room2:...")
        # Available in Ayla but may be empty in skegox shadow
        room_list_raw = reported.get("Robot_Room_List", {})
        room_list_val = room_list_raw.get("value", room_list_raw) if isinstance(room_list_raw, dict) else room_list_raw
        if room_list_val and isinstance(room_list_val, str) and ":" in room_list_val:
            parts = room_list_val.split(":")
            vac.floor_id = parts[0]
            vac.rooms = parts[1:]

        # Also try to get floor_id from AreasToClean_V3 if not set
        if not vac.floor_id:
            atc = reported.get("AreasToClean_V3", {})
            atc_val = atc.get("value", atc) if isinstance(atc, dict) else atc
            if atc_val and isinstance(atc_val, str) and "floor_id" in atc_val:
                try:
                    import json as _json
                    atc_data = _json.loads(atc_val)
                    vac.floor_id = atc_data.get("floor_id", "")
                except (ValueError, TypeError):
                    pass

        return vac

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
        attrs: dict[str, Any] = {
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
        if self.rooms:
            attrs["rooms"] = self.rooms
            attrs["floor_id"] = self.floor_id
        return attrs

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
