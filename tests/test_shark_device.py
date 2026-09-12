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


def make_mop_vacuum(
    flow_mode: int = 1,
    operating_mode: int = 0,
    docked_status: int = 1,
    charging_status: int = 0,
) -> SharkVacuum:
    """A vac+mop combo model — i.e. one whose shadow carries a mop plate."""
    data = make_skegox_device(operating_mode=operating_mode)
    reported = data["shadow"]["properties"]["reported"]
    reported["DockedStatus"]["value"] = docked_status
    reported["Charging_Status"]["value"] = charging_status
    reported["Flow_Mode"] = {"value": flow_mode}
    reported["MopPlateAttached"] = {"value": True}
    return SharkVacuum.from_skegox(data)


def make_wet_dry_vacuum(
    flow_mode: int = 1,
    operating_mode: int = 0,
    docked_status: int = 1,
    charging_status: int = 0,
) -> SharkVacuum:
    """A wet/dry model (UR2850ZEUS) — mop plate plus CleaningParameters."""
    data = make_skegox_device(operating_mode=operating_mode)
    reported = data["shadow"]["properties"]["reported"]
    reported["DockedStatus"]["value"] = docked_status
    reported["Charging_Status"]["value"] = charging_status
    reported["Flow_Mode"] = {"value": flow_mode}
    reported["MopPlateAttached"] = {"value": True}
    reported["CleaningParameters"] = {
        "value": '{"CleanStage":2,"Deep":0,"Wet":0,"Dry":1}',
    }
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
            "recommend_rest_and_recharge",
        ):
            assert key in attrs
        assert "schedule" not in attrs  # omitted when empty
        # No Flow_Mode in this shadow, so no mop tank to report a level for
        assert "water_flow" not in attrs


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


class TestFlowModeCapability:
    """Only models with a mop plate may advertise a water flow control."""

    def test_absent_mop_plate_is_not_a_capability(self):
        assert make_vacuum().has_flow_mode is False

    def test_flow_mode_alone_is_not_a_capability(self):
        # Dry-only models (AV251WAXUS) report Flow_Mode without a mop plate.
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["Flow_Mode"] = {"value": 1}
        vac = SharkVacuum.from_skegox(data)
        assert vac.has_flow_mode is False
        assert "water_flow" not in vac.to_attributes_payload()

    def test_present_mop_plate_is_a_capability(self):
        assert make_mop_vacuum(flow_mode=1).has_flow_mode is True

    def test_capability_holds_even_for_out_of_range_values(self):
        # The property exists, so the hardware has a tank; the level just
        # didn't parse. Still a mop model.
        vac = make_mop_vacuum(flow_mode=99)
        assert vac.has_flow_mode is True
        assert vac.water_flow == "normal"

    def test_attributes_include_water_flow_for_mop_models(self):
        assert "water_flow" in make_mop_vacuum(flow_mode=2).to_attributes_payload()


class TestWetDryCapability:
    """Wet/dry models are identified by CleaningParameters presence."""

    def test_absent_cleaning_parameters_is_not_wet_dry(self):
        assert make_vacuum().has_wet_dry is False
        assert make_mop_vacuum().has_wet_dry is False

    def test_present_cleaning_parameters_is_wet_dry(self):
        assert make_wet_dry_vacuum().has_wet_dry is True

    def test_wet_dry_models_also_have_mop_plate(self):
        # Real captures (UR2850ZEUS, RV2820YEUS) carry both properties.
        vac = make_wet_dry_vacuum()
        assert vac.has_wet_dry is True
        assert vac.has_flow_mode is True
        assert "water_flow" in vac.to_attributes_payload()

    @pytest.mark.asyncio
    async def test_discovery_retracts_select_for_vac_only(self, mock_config):
        # Not publishing isn't enough — discovery configs are retained, so a
        # config published by an earlier version would linger in HA. The
        # empty payload is autodiscovery's delete.
        client = MqttClient(mock_config)
        client._publish = AsyncMock()
        await client.publish_discovery(make_vacuum())
        water_flow = [
            c for c in client._publish.call_args_list
            if "_water_flow/config" in c.args[0]
        ]
        assert len(water_flow) == 1
        assert water_flow[0].args[1] == ""
        assert water_flow[0].kwargs["retain"] is True

    @pytest.mark.asyncio
    async def test_discovery_publishes_select_for_mop_models(self, mock_config):
        client = MqttClient(mock_config)
        client._publish = AsyncMock()
        await client.publish_discovery(make_mop_vacuum(flow_mode=1))
        topics = [c.args[0] for c in client._publish.call_args_list]
        assert any("_water_flow/config" in t for t in topics)


class TestWaterFlowOverrideWhileDocked:
    """Mirrors the fan_speed override: hardware reports eco while docked,
    so the last user-set value should be substituted in the published
    attributes, same as the existing fan_speed override for state."""

    @pytest.fixture
    def client(self, mock_config):
        client = MqttClient(mock_config)
        client._publish = AsyncMock()
        return client

    @staticmethod
    def _attrs(client):
        return [
            c for c in client._publish.call_args_list
            if c.args[0].endswith("/attributes")
        ][0].args[1]

    @pytest.mark.asyncio
    async def test_no_override_uses_device_reported_value(self, client):
        vac = make_mop_vacuum(flow_mode=1, docked_status=1)
        await client.publish_state(vac)
        assert self._attrs(client)["water_flow"] == "normal"

    @pytest.mark.asyncio
    async def test_override_applied_while_docked(self, client):
        vac = make_mop_vacuum(flow_mode=0, docked_status=1)
        client._water_flow_overrides[vac.dsn] = "max"
        await client.publish_state(vac)
        assert self._attrs(client)["water_flow"] == "max"

    @pytest.mark.asyncio
    async def test_no_override_when_not_docked(self, client):
        vac = make_mop_vacuum(
            flow_mode=1, operating_mode=2, docked_status=0, charging_status=0
        )
        client._water_flow_overrides[vac.dsn] = "max"
        await client.publish_state(vac)
        # Not docked -> trust the device's own reported value, not the override
        assert self._attrs(client)["water_flow"] == "normal"

    @pytest.mark.asyncio
    async def test_override_never_resurrects_water_flow_on_vac_only(self, client):
        # A stale override must not put the attribute back on a model that
        # has no mop tank.
        vac = make_vacuum(docked_status=1)
        client._water_flow_overrides[vac.dsn] = "max"
        await client.publish_state(vac)
        assert "water_flow" not in self._attrs(client)


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


class TestWetDryDiscovery:
    """Wet/dry models get a Wet/Dry clean mode select + Deep button;
    everything else keeps Normal/Matrix and has the Deep button retracted."""

    @pytest.fixture
    def client(self, mock_config):
        client = MqttClient(mock_config)
        client._publish = AsyncMock()
        return client

    def _clean_mode_config(self, client):
        for call in client._publish.call_args_list:
            if "_clean_mode/config" in call.args[0]:
                return call.args[1]
        return None

    def _deep_button_config(self, client):
        for call in client._publish.call_args_list:
            topic = call.args[0]
            if "/button/" in topic and topic.endswith("_deep/config"):
                return call.args[1]
        return None

    @pytest.mark.asyncio
    async def test_wet_dry_device_gets_wet_dry_select(self, client):
        vac = make_wet_dry_vacuum()
        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        config = self._clean_mode_config(client)
        assert config["options"] == ["Wet", "Dry"]

    @pytest.mark.asyncio
    async def test_wet_dry_device_gets_deep_button(self, client):
        vac = make_wet_dry_vacuum()
        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        config = self._deep_button_config(client)
        assert config is not None
        assert config["payload_press"] == "vacuum_and_mop"

    @pytest.mark.asyncio
    async def test_wet_dry_state_defaults_to_dry(self, client):
        vac = make_wet_dry_vacuum()
        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        states = [
            (c.args[0], c.args[1]) for c in client._publish.call_args_list
            if c.args[0].endswith("/clean_mode/state")
        ]
        assert states and states[-1][1] == "Dry"

    @pytest.mark.asyncio
    async def test_dry_only_device_keeps_normal_matrix_select(self, client):
        vac = make_vacuum()
        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        config = self._clean_mode_config(client)
        assert config["options"] == ["Normal", "Matrix"]

    @pytest.mark.asyncio
    async def test_deep_button_retracted_for_dry_only_devices(self, client):
        vac = make_vacuum()
        vac.rooms = ["Kitchen"]
        await client.publish_discovery(vac)
        config = self._deep_button_config(client)
        assert config is not None
        assert config == ""

    @pytest.mark.asyncio
    async def test_stale_matrix_mode_falls_back_to_dry(self, client):
        vac = make_wet_dry_vacuum()
        vac.rooms = ["Kitchen"]
        client._clean_modes[vac.dsn] = "Matrix"  # stale from an older model
        await client.publish_discovery(vac)
        states = [
            (c.args[0], c.args[1]) for c in client._publish.call_args_list
            if c.args[0].endswith("/clean_mode/state")
        ]
        assert states and states[-1][1] == "Dry"
