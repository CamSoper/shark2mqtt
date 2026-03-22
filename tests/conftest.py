"""Shared fixtures for shark2mqtt tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shark_device import SharkVacuum


@pytest.fixture
def command_event():
    return asyncio.Event()


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.poll_interval = 5  # short for tests
    config.poll_interval_active = 1
    config.mqtt_prefix = "shark2mqtt"
    return config


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.get_all_devices.return_value = []
    return api


@pytest.fixture
def mock_auth():
    auth = AsyncMock()
    auth.ensure_authenticated.return_value = None
    return auth


@pytest.fixture
def mock_mqtt():
    mqtt = AsyncMock()
    mqtt.publish_discovery.return_value = None
    mqtt.publish_state.return_value = None
    return mqtt


def make_skegox_device(
    dsn: str = "DSN123",
    name: str = "Test Shark",
    operating_mode: int = 0,
    battery: int = 100,
    connected: bool = True,
) -> dict[str, Any]:
    """Build a minimal skegox API response dict."""
    return {
        "deviceId": dsn,
        "metadata": {"deviceName": name},
        "registry": {
            "Battery_Serial_Num": f"BSN-{dsn}",
            "Device_Model_Number": "RV2001",
        },
        "telemetry": {
            "Battery_Capacity": battery,
            "RSSI": -50,
        },
        "connectivityStatus": {"connected": connected},
        "shadow": {
            "properties": {
                "reported": {
                    "Operating_Mode": {"value": operating_mode},
                    "Charging_Status": {"value": 0},
                    "Power_Mode": {"value": 2},
                    "DockedStatus": {"value": 1},
                    "Error_Code": {"value": 0},
                },
            },
        },
    }
