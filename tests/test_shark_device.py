"""Tests for SharkVacuum state mapping and MQTT discovery dedup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mqtt_client import MqttClient
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device


def make_vacuum(
    operating_mode: int = 0,
    docked_status: int = 1,
    charging_status: int = 0,
) -> SharkVacuum:
    data = make_skegox_device(operating_mode=operating_mode)
    reported = data["shadow"]["properties"]["reported"]
    reported["DockedStatus"]["value"] = docked_status
    reported["Charging_Status"]["value"] = charging_status
    return SharkVacuum.from_skegox(data)


class TestDockedState:
    def test_docked_status_docked(self):
        vac = make_vacuum(operating_mode=0, docked_status=1)
        assert vac.is_docked
        assert vac.ha_state == "docked"

    def test_charging_implies_docked(self):
        # Issue #29: skegox reported DockedStatus=0 + Operating_Mode=RETURN
        # for days while the robot sat on the dock charging.
        vac = make_vacuum(operating_mode=3, docked_status=0, charging_status=1)
        assert vac.is_docked
        assert vac.ha_state == "docked"

    def test_returning_when_not_charging(self):
        vac = make_vacuum(operating_mode=3, docked_status=0, charging_status=0)
        assert not vac.is_docked
        assert vac.ha_state == "returning"

    def test_cleaning_not_masked_by_dock(self):
        vac = make_vacuum(operating_mode=2, docked_status=1)
        assert vac.ha_state == "cleaning"


class TestDockAndMaintenanceProperties:
    def test_defaults_when_absent(self):
        vac = make_vacuum()
        assert vac.is_evacuating is False
        assert vac.evacuate_state == 0
        assert vac.evacuate_resume_status is False
        assert vac.dock_error_code == 0
        assert vac.dock_knob_status == 0
        assert vac.warning_code == 0
        assert vac.extended_error_code == ""
        assert vac.run_time_cumulative == 0
        assert vac.replace_battery is False
        assert vac.recommend_rest_and_recharge is False
        assert vac.schedule is None

    def test_reads_reported_values(self):
        data = make_skegox_device()
        reported = data["shadow"]["properties"]["reported"]
        reported["Evacuating"] = {"value": True}
        reported["DockErrorCode"] = {"value": 3}
        reported["Warning_Code"] = {"value": 7}
        reported["Extended_Error_Code"] = {"value": "E-42"}
        reported["RunTimeCumulative"] = {"value": 117}
        reported["ReplaceBattery"] = {"value": True}
        reported["Schedule"] = {"value": {"Monday": {"value": []}}}
        vac = SharkVacuum.from_skegox(data)

        assert vac.is_evacuating is True
        assert vac.dock_error_code == 3
        assert vac.warning_code == 7
        assert vac.extended_error_code == "E-42"
        assert vac.run_time_cumulative == 117
        assert vac.replace_battery is True
        assert vac.schedule == {"Monday": {"value": []}}

    def test_attributes_payload_includes_new_fields(self):
        vac = make_vacuum()
        attrs = vac.to_attributes_payload()
        for key in (
            "is_evacuating", "evacuate_state", "evacuate_resume_status",
            "dock_error_code", "dock_knob_status", "warning_code",
            "extended_error_code", "run_time_cumulative", "replace_battery",
            "recommend_rest_and_recharge", "water_flow",
        ):
            assert key in attrs
        assert "schedule" not in attrs  # omitted when empty


class TestWaterFlow:
    """Mop water flow level — mirrors Power_Mode's 0/1/2 eco/normal/max scale."""

    def test_defaults_to_normal_when_absent(self):
        vac = make_vacuum()
        assert vac.flow_mode is None
        assert vac.water_flow == "normal"

    def test_reads_flow_mode_max(self):
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["Flow_Mode"] = {"value": 2}
        vac = SharkVacuum.from_skegox(data)
        assert vac.water_flow == "max"

    def test_reads_flow_mode_eco(self):
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["Flow_Mode"] = {"value": 0}
        vac = SharkVacuum.from_skegox(data)
        assert vac.water_flow == "eco"

    def test_invalid_flow_mode_falls_back_to_normal(self):
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["Flow_Mode"] = {"value": 99}
        vac = SharkVacuum.from_skegox(data)
        assert vac.flow_mode is None
        assert vac.water_flow == "normal"


class TestDiscoveryDedup:
    @pytest.fixture
    def client(self, mock_config):
        client = MqttClient(mock_config)
        client._publish = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_unchanged_discovery_skipped(self, client):
        vac = make_vacuum()
        await client.publish_discovery(vac)
        first_count = client._publish.call_count
        assert first_count > 0

        await client.publish_discovery(vac)
        assert client._publish.call_count == first_count

    @pytest.mark.asyncio
    async def test_room_change_republishes(self, client):
        vac = make_vacuum()
        await client.publish_discovery(vac)
        first_count = client._publish.call_count

        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        assert client._publish.call_count > first_count
