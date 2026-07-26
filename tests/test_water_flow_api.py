"""Tests for set_water_flow dispatch on skegox and ayla API backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.skegox_api import SkegoxApi


class FakeConfig:
    shark_region = "us"


@pytest.mark.asyncio
async def test_skegox_set_water_flow_max():
    api = SkegoxApi.__new__(SkegoxApi)
    api._household_id = "hh1"
    api.set_desired_property = AsyncMock()
    await api.set_water_flow("SND1", "max")
    api.set_desired_property.assert_awaited_once_with("SND1", "Flow_Mode", 2)


@pytest.mark.asyncio
async def test_skegox_set_water_flow_eco():
    api = SkegoxApi.__new__(SkegoxApi)
    api._household_id = "hh1"
    api.set_desired_property = AsyncMock()
    await api.set_water_flow("SND1", "eco")
    api.set_desired_property.assert_awaited_once_with("SND1", "Flow_Mode", 0)


@pytest.mark.asyncio
async def test_skegox_set_water_flow_unknown_noops():
    api = SkegoxApi.__new__(SkegoxApi)
    api._household_id = "hh1"
    api.set_desired_property = AsyncMock()
    await api.set_water_flow("SND1", "bogus")
    api.set_desired_property.assert_not_awaited()
