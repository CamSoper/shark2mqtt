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
