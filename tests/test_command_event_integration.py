"""Tests for command_listener setting command_event."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from src.mqtt_client import MqttClient
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device


class FakeTopic:
    """Mimics aiomqtt Topic for test messages."""

    def __init__(self, value: str) -> None:
        self.value = value


class FakeMessage:
    """Mimics aiomqtt Message."""

    def __init__(self, topic: str, payload: str) -> None:
        self.topic = FakeTopic(topic)
        self.payload = payload.encode()


async def _run_listener_with_messages(
    messages: list[FakeMessage],
    devices: dict[str, Any],
    command_event: asyncio.Event | None = None,
    handler: Any | None = None,
) -> MqttClient:
    """Set up a MqttClient with fake messages and run command_listener."""
    if handler is None:
        handler = AsyncMock()

    config = MagicMock()
    config.mqtt_prefix = "shark2mqtt"
    config.mqtt_host = "localhost"
    config.mqtt_port = 1883
    config.mqtt_username = None
    config.mqtt_password = None

    mqtt = MqttClient(config)

    # Mock the internal client and _publish (for handlers that publish state)
    mock_client = AsyncMock()
    mock_client.subscribe = AsyncMock()
    mock_client.publish = AsyncMock()

    # Create an async iterator that yields messages then stops
    async def message_stream():
        for msg in messages:
            yield msg

    mock_client.messages = message_stream()
    mqtt._client = mock_client

    await mqtt.command_listener(handler, devices, command_event)
    return mqtt


@pytest.mark.asyncio
async def test_command_sets_event(command_event):
    """Successful command dispatch should set command_event."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    devices = {dsn: device}

    messages = [FakeMessage(f"shark2mqtt/{dsn}/command", "start")]

    await _run_listener_with_messages(messages, devices, command_event)

    assert command_event.is_set()


@pytest.mark.asyncio
async def test_set_fan_speed_sets_event(command_event):
    """Fan speed command should also set command_event."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    devices = {dsn: device}

    messages = [FakeMessage(f"shark2mqtt/{dsn}/set_fan_speed", "max")]

    await _run_listener_with_messages(messages, devices, command_event)

    assert command_event.is_set()


@pytest.mark.asyncio
async def test_event_not_set_on_failed_command(command_event):
    """If the command handler raises, event should NOT be set."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    devices = {dsn: device}

    handler = AsyncMock()
    handler.send_command.side_effect = RuntimeError("API error")

    messages = [FakeMessage(f"shark2mqtt/{dsn}/command", "start")]

    await _run_listener_with_messages(messages, devices, command_event, handler)

    assert not command_event.is_set()


@pytest.mark.asyncio
async def test_event_not_set_for_unknown_device(command_event):
    """Commands for unknown devices should not set the event."""
    devices = {}  # no devices registered

    messages = [FakeMessage("shark2mqtt/UNKNOWN/command", "start")]

    await _run_listener_with_messages(messages, devices, command_event)

    assert not command_event.is_set()


@pytest.mark.asyncio
async def test_listener_works_without_event():
    """command_event=None should not crash — backwards compatible."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    devices = {dsn: device}

    handler = AsyncMock()
    messages = [FakeMessage(f"shark2mqtt/{dsn}/command", "stop")]

    await _run_listener_with_messages(messages, devices, command_event=None, handler=handler)

    handler.send_command.assert_awaited_once_with(dsn, "stop")


# --- send_command params parsing ---


@pytest.mark.asyncio
async def test_send_command_with_toplevel_params():
    """HA sends service data as top-level keys, not nested under params."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    device.floor_id = "FLOOR1"
    device.rooms = ["Kitchen"]
    devices = {dsn: device}

    handler = AsyncMock()
    # This is what HA actually sends
    payload = '{"command": "clean_room", "room": "Kitchen"}'
    messages = [FakeMessage(f"shark2mqtt/{dsn}/send_command", payload)]

    await _run_listener_with_messages(messages, devices, handler=handler)

    handler.clean_rooms.assert_awaited_once_with(
        dsn, rooms=["Kitchen"], floor_id="FLOOR1",
        clean_type="dry", clean_count=1, mode="UserRoom",
    )


@pytest.mark.asyncio
async def test_send_command_with_dict_params():
    """send_command should still work with params as a dict (regression)."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    device.floor_id = "FLOOR1"
    device.rooms = ["Kitchen"]
    devices = {dsn: device}

    handler = AsyncMock()
    payload = '{"command": "clean_room", "params": {"room": "Kitchen"}}'
    messages = [FakeMessage(f"shark2mqtt/{dsn}/send_command", payload)]

    await _run_listener_with_messages(messages, devices, handler=handler)

    handler.clean_rooms.assert_awaited_once_with(
        dsn, rooms=["Kitchen"], floor_id="FLOOR1",
        clean_type="dry", clean_count=1, mode="UserRoom",
    )


# --- Room button / clean mode ---


@pytest.mark.asyncio
async def test_clean_room_button_normal_mode():
    """Room button press should dispatch clean_rooms with Normal mode."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    device.floor_id = "FLOOR1"
    device.rooms = ["Kitchen"]
    devices = {dsn: device}

    handler = AsyncMock()
    payload = '{"room": "Kitchen"}'
    messages = [FakeMessage(f"shark2mqtt/{dsn}/clean_room", payload)]

    await _run_listener_with_messages(messages, devices, handler=handler)

    handler.clean_rooms.assert_awaited_once_with(
        dsn, rooms=["Kitchen"], floor_id="FLOOR1",
        clean_type="dry", clean_count=1, mode="UserRoom",
    )


@pytest.mark.asyncio
async def test_clean_room_button_matrix_mode():
    """Room button press with Matrix mode should use UltraClean."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    device.floor_id = "FLOOR1"
    device.rooms = ["Kitchen"]
    devices = {dsn: device}

    handler = AsyncMock()
    # Set mode first, then press button
    messages = [
        FakeMessage(f"shark2mqtt/{dsn}/clean_mode", "Matrix"),
        FakeMessage(f"shark2mqtt/{dsn}/clean_room", '{"room": "Kitchen"}'),
    ]

    await _run_listener_with_messages(messages, devices, handler=handler)

    handler.clean_rooms.assert_awaited_once_with(
        dsn, rooms=["Kitchen"], floor_id="FLOOR1",
        clean_type="dry", clean_count=2, mode="UltraClean",
    )


@pytest.mark.asyncio
async def test_clean_mode_updates_state():
    """Clean mode select should store the mode."""
    dsn = "DSN123"
    device = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn))
    devices = {dsn: device}

    messages = [FakeMessage(f"shark2mqtt/{dsn}/clean_mode", "Matrix")]

    mqtt = await _run_listener_with_messages(messages, devices)

    assert mqtt._clean_modes[dsn] == "Matrix"


