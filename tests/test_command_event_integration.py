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
) -> None:
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

    # Mock the internal client
    mock_client = AsyncMock()
    mock_client.subscribe = AsyncMock()

    # Create an async iterator that yields messages then stops
    async def message_stream():
        for msg in messages:
            yield msg

    mock_client.messages = message_stream()
    mqtt._client = mock_client

    await mqtt.command_listener(handler, devices, command_event)


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
