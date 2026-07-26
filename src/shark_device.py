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
    PROP_GET_DOCK_ERROR_CODE,
    PROP_GET_DOCK_KNOB_STATUS,
    PROP_GET_DOCKED_STATUS,
    PROP_GET_ERROR_CODE,
    PROP_GET_EVACUATE,
    PROP_GET_EVACUATE_RESUME_STATUS,
    PROP_GET_EVACUATING,
    PROP_GET_EXTENDED_ERROR_CODE,
    PROP_GET_FLOW_MODE,
    PROP_GET_OPERATING_MODE,
    PROP_GET_POWER_MODE,
    PROP_GET_RECOMMEND_RANDR,
    PROP_GET_REPLACE_BATTERY,
    PROP_GET_ROBOT_FIRMWARE_VERSION,
    PROP_GET_RSSI,
    PROP_GET_RUN_TIME_CUMULATIVE,
    PROP_GET_SCHEDULE,
    PROP_GET_WARNING_CODE,
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
        self.has_areas_v3: bool = False
        self.api_backend: str = "ayla"
        self.room_name_map: dict[str, str] = {}

    def to_robot_room_names(self, rooms: list[str]) -> list[str]:
        """Reverse-map display names to robot_room_name values for the API.

        Clean commands must send the `robot_room_name` (e.g. `AZ_1`) that
        the device understands, not the human-readable display name we
        publish to HA. For accounts where MARD has identity mapping
        (robot_room_name == display name) this is a no-op. Unknown
        rooms pass through unchanged.

        See issue #4.
        """
        if not self.room_name_map:
            return list(rooms)
        reverse = {display: robot for robot, display in self.room_name_map.items()}
        return [reverse.get(r, r) for r in rooms]

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

        # Detect clean command capability from shadow properties
        vac.has_areas_v3 = "AreasToClean_V3" in reported

        # Parse room list from Robot_Room_List (format: "FloorID:Room1:Room2:...")
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

        vac.api_backend = "skegox"

        if logger.isEnabledFor(logging.DEBUG):
            name = metadata.get("deviceName", vac.dsn)
            raw_room_list = reported.get("Robot_Room_List", {})
            raw_v3 = reported.get("AreasToClean_V3", {})
            raw_v2 = reported.get("AreasToClean_V2", {})
            raw_atc = reported.get("Areas_To_Clean", {})
            def _val(x: Any) -> Any:
                return x.get("value", x) if isinstance(x, dict) else x
            prop_names = sorted(reported.keys())
            hint_keywords = ("room", "area", "zone", "map", "floor")
            hint_props = {
                k: _val(reported[k])
                for k in prop_names
                if any(kw in k.lower() for kw in hint_keywords)
            }
            logger.debug(
                "Shadow dump for %s (%s): "
                "Robot_Room_List=%r, AreasToClean_V3=%r, AreasToClean_V2=%r, "
                "Areas_To_Clean=%r, parsed_floor_id=%r, parsed_rooms=%r",
                name, vac.dsn,
                _val(raw_room_list), _val(raw_v3), _val(raw_v2),
                _val(raw_atc), vac.floor_id, vac.rooms,
            )
            logger.debug(
                "Shadow property names for %s: %s", name, prop_names,
            )
            logger.debug(
                "Room/area/zone/map/floor properties for %s: %s",
                name, hint_props,
            )

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
        # Charging implies docked: the robot can't charge off the dock,
        # but skegox can report a stale DockedStatus=0 for days after a
        # completed return (issue #29).
        return (
            self._get_int_prop(PROP_GET_DOCKED_STATUS) == 1
            or self.is_charging
        )

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
    def flow_mode(self) -> PowerMode | None:
        """Mop water flow level (vac+mop combo models). Same 0/1/2 scale
        as power_mode (eco/normal/max); confirmed against the SharkClean
        app's "Water Flow Level" slider."""
        val = self._get_int_prop(PROP_GET_FLOW_MODE, -1)
        try:
            return PowerMode(val)
        except ValueError:
            return None

    @property
    def water_flow(self) -> str:
        mode = self.flow_mode
        if mode is None:
            return "normal"
        return POWER_MODE_NAMES.get(mode, "normal")

    @property
    def rssi(self) -> int:
        # Some models report RSSI as positive, others as negative dBm.
        # Real WiFi RSSI is always ≤ 0 dBm, so normalize to negative.
        return -abs(self._get_int_prop(PROP_GET_RSSI))

    @property
    def firmware_version(self) -> str:
        return str(self._get_prop(PROP_GET_ROBOT_FIRMWARE_VERSION, ""))

    @property
    def model_number(self) -> str:
        return str(self._get_prop(PROP_GET_DEVICE_MODEL_NUMBER, self.model))

    @property
    def is_online(self) -> bool:
        return self.connection_status == "Online"

    @property
    def is_evacuating(self) -> bool:
        """Self-empty dock is actively emptying the bin."""
        return self._get_int_prop(PROP_GET_EVACUATING) == 1

    @property
    def evacuate_state(self) -> int:
        return self._get_int_prop(PROP_GET_EVACUATE)

    @property
    def evacuate_resume_status(self) -> bool:
        return self._get_int_prop(PROP_GET_EVACUATE_RESUME_STATUS) == 1

    @property
    def dock_error_code(self) -> int:
        return self._get_int_prop(PROP_GET_DOCK_ERROR_CODE)

    @property
    def dock_knob_status(self) -> int:
        return self._get_int_prop(PROP_GET_DOCK_KNOB_STATUS)

    @property
    def warning_code(self) -> int:
        """Separate warning channel from error_code — non-fatal conditions."""
        return self._get_int_prop(PROP_GET_WARNING_CODE)

    @property
    def extended_error_code(self) -> str:
        return str(self._get_prop(PROP_GET_EXTENDED_ERROR_CODE, "") or "")

    @property
    def run_time_cumulative(self) -> int:
        return self._get_int_prop(PROP_GET_RUN_TIME_CUMULATIVE)

    @property
    def replace_battery(self) -> bool:
        return self._get_int_prop(PROP_GET_REPLACE_BATTERY) == 1

    @property
    def recommend_rest_and_recharge(self) -> bool:
        return self._get_int_prop(PROP_GET_RECOMMEND_RANDR) == 1

    @property
    def schedule(self) -> dict[str, Any] | None:
        val = self._get_prop(PROP_GET_SCHEDULE)
        return val if isinstance(val, dict) and val else None

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
            "is_evacuating": self.is_evacuating,
            "evacuate_state": self.evacuate_state,
            "evacuate_resume_status": self.evacuate_resume_status,
            "dock_error_code": self.dock_error_code,
            "dock_knob_status": self.dock_knob_status,
            "warning_code": self.warning_code,
            "extended_error_code": self.extended_error_code,
            "run_time_cumulative": self.run_time_cumulative,
            "replace_battery": self.replace_battery,
            "recommend_rest_and_recharge": self.recommend_rest_and_recharge,
            "water_flow": self.water_flow,
        }
        if self.rooms:
            attrs["rooms"] = self.rooms
            attrs["floor_id"] = self.floor_id
        if self.schedule:
            attrs["schedule"] = self.schedule
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
