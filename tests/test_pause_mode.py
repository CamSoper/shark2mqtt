"""Tests for the configurable pause Operating_Mode value."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.skegox_api import SkegoxApi


def make_settings(**overrides) -> Settings:
    values = {
        "shark_username": "user@example.com",
        "shark_password": "secret",
        "mqtt_host": "localhost",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def make_api(settings: Settings) -> SkegoxApi:
    api = SkegoxApi.__new__(SkegoxApi)
    api._config = settings
    api._household_id = "hh1"
    api.set_desired_property = AsyncMock()
    return api


def test_default_pause_value_is_one(monkeypatch):
    monkeypatch.delenv("PAUSE_OPERATING_MODE", raising=False)
    assert make_settings().pause_operating_mode == 1


def test_pause_value_from_env(monkeypatch):
    monkeypatch.setenv("PAUSE_OPERATING_MODE", "0")
    settings = Settings(
        _env_file=None,
        shark_username="user@example.com",
        shark_password="secret",
        mqtt_host="localhost",
    )
    assert settings.pause_operating_mode == 0


@pytest.mark.parametrize("bad", [-1, 2, 7])
def test_pause_value_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        make_settings(pause_operating_mode=bad)


@pytest.mark.asyncio
async def test_pause_sends_default_one():
    api = make_api(make_settings())
    await api.send_command("SND1", "pause")
    api.set_desired_property.assert_awaited_once_with("SND1", "Operating_Mode", 1)


@pytest.mark.asyncio
async def test_pause_sends_configured_zero():
    api = make_api(make_settings(pause_operating_mode=0))
    await api.send_command("SND1", "pause")
    api.set_desired_property.assert_awaited_once_with("SND1", "Operating_Mode", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,prop,value",
    [
        ("start", "Operating_Mode", 2),
        ("stop", "Operating_Mode", 0),
        ("return_to_base", "Operating_Mode", 3),
        ("locate", "Find_Device", 1),
        ("mop", "Operating_Mode", 7),
        ("vacuum_and_mop", "Operating_Mode", 8),
    ],
)
async def test_other_commands_unchanged_by_pause_setting(command, prop, value):
    api = make_api(make_settings(pause_operating_mode=0))
    await api.send_command("SND1", command)
    api.set_desired_property.assert_awaited_once_with("SND1", prop, value)
