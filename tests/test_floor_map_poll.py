"""Tests for the Visual_Floor_1 fetch/parse/publish block in poll_loop.

Covers the shadow ``fileList.Visual_Floor_1.updatedAt`` change detection,
geometry/pose caching, live-pose republishing, and error handling for the
floor map image pipeline (src/main.py).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest
from src.main import (
    _fetch_skegox_visual_floor,
    _map_geometry,
    poll_loop,
)

from .conftest import make_skegox_device

FLOOR_BIN = b"\x01floor-map-bytes"
PNG = b"png-bytes"
TS_1 = "2026-09-08T04:25:36.000Z"
TS_2 = "2026-09-08T05:00:00.000Z"

GRID_A = ((1, 0), (0, 1))
GRID_B = ((2, 0), (0, 2))


def _parsed_map(
    pose: tuple[float, float, float] | None = (0.5, 0.75, 1.57),
    grid: Any = GRID_A,
) -> dict[str, Any]:
    """Shape of a parsed Visual_Floor_1 map."""
    return {
        "name": "Floor 1",
        "map_id": "floorRPfile1",
        "grid": grid,
        "zones": [],
        "boundaries": [],
        "pose": pose,
    }


def _raw_device(
    updated_at: str | None = TS_1,
    live: tuple[float, float, float] | None = None,
    dsn: str = "DSN123",
) -> dict[str, Any]:
    """Skegox device dict with a Visual_Floor_1 fileList entry."""
    raw = make_skegox_device(dsn=dsn)
    reported = raw["shadow"]["properties"]["reported"]
    if updated_at is not None:
        reported["fileList"] = {"Visual_Floor_1": {"updatedAt": updated_at}}
    if live is not None:
        # telemetry.LiveLocation is a JSON-encoded string
        raw["telemetry"]["LiveLocation"] = json.dumps(
            {"x_coord": live[0], "y_coord": live[1], "theta": live[2]},
        )
    return raw


class _Wired:
    """Mocks wired up so poll_loop can run the floor map path."""

    def __init__(self) -> None:
        self.api = AsyncMock()
        self.ayla = AsyncMock()
        self.ayla.get_devices.return_value = []
        self.mqtt = AsyncMock()
        self.vfm = AsyncMock()
        # parse_floor_map_bytes is called synchronously by poll_loop
        self.vfm.parse_floor_map_bytes = AsyncMock(return_value=_parsed_map())
        self.vfm.render_floor_map_pillow = AsyncMock(return_value=PNG)
        self.auth = AsyncMock()
        self.config = MagicMock()
        self.config.poll_interval = 0.01
        self.config.poll_interval_active = 0.01
        self.event = asyncio.Event()
        self.fetched: list[str] = []

        async def fetch(dsn: str, name: str, **kwargs: Any) -> bytes:
            self.fetched.append(name)
            return b"" if name == "MARD" else FLOOR_BIN

        self.api.fetch_property_file.side_effect = fetch

    async def run_polls(self, raws: list[dict[str, Any]]) -> None:
        """Drive poll_loop through one cycle per raw device, then stop."""

        async def get_all() -> list[dict[str, Any]]:
            if not raws:
                raise asyncio.CancelledError
            return [raws.pop(0)]

        self.api.get_all_devices.side_effect = get_all
        with pytest.raises(asyncio.CancelledError):
            await poll_loop(
                self.api,
                self.ayla,
                self.mqtt,
                self.vfm,
                self.auth,
                self.config,
                {},
                {},
                {},
                self.event,
            )


@pytest.mark.asyncio
async def test_first_poll_fetches_and_publishes_map():
    """A new device triggers a fetch, parse, and exactly one publish."""
    w = _Wired()
    await w.run_polls([_raw_device()])

    assert "Visual_Floor_1" in w.fetched
    w.mqtt.publish_map_image.assert_awaited_once()
    _, png = w.mqtt.publish_map_image.await_args.args
    assert png == PNG
    # Rendered with the full static geometry plus the pose
    rendered = w.vfm.render_floor_map_pillow.await_args.args[0]
    assert rendered["grid"] == GRID_A
    assert rendered["zones"] == []
    assert rendered["boundaries"] == []
    assert rendered["pose"] == _parsed_map()["pose"]


@pytest.mark.asyncio
async def test_live_pose_preferred_over_file_pose():
    """When telemetry.LiveLocation is present it wins over the .bin pose."""
    w = _Wired()
    await w.run_polls([_raw_device(live=(1.0, 2.0, 0.5))])

    w.mqtt.publish_map_image.assert_awaited_once()
    rendered = w.vfm.render_floor_map_pillow.await_args.args[0]
    assert rendered["pose"] == (1.0, 2.0, 0.5)


@pytest.mark.asyncio
async def test_unchanged_timestamp_skips_refetch_and_republish():
    """Same updatedAt on the next poll: no fetch, no publish."""
    w = _Wired()
    await w.run_polls([_raw_device(), _raw_device()])

    assert w.fetched.count("Visual_Floor_1") == 1
    w.mqtt.publish_map_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_timestamp_change_with_new_geometry_republishes():
    """A newer updatedAt re-fetches, and changed geometry is rendered."""
    w = _Wired()

    def parse_side_effect(data: bytes) -> dict[str, Any]:
        # First floor fetch has grid A, second floor fetch has grid B
        return _parsed_map(grid=GRID_A if w.fetched.count("Visual_Floor_1") == 1 else GRID_B)

    w.vfm.parse_floor_map_bytes.side_effect = parse_side_effect
    await w.run_polls([_raw_device(TS_1), _raw_device(TS_2)])

    assert w.fetched.count("Visual_Floor_1") == 2
    assert w.mqtt.publish_map_image.await_count == 2
    # The second render used the new geometry, not the cached one
    rendered = w.vfm.render_floor_map_pillow.await_args.args[0]
    assert rendered["grid"] == GRID_B


@pytest.mark.asyncio
async def test_timestamp_change_with_identical_map_skips_publish():
    """Newer timestamp re-fetches, but identical geometry+pose skips the
    publish while still recording the timestamp (no further refetch)."""
    w = _Wired()
    await w.run_polls([_raw_device(TS_1), _raw_device(TS_2), _raw_device(TS_2)])

    # Fetch on poll 1 (new device) and poll 2 (timestamp changed);
    # poll 3 sees the recorded TS_2 and does not refetch.
    assert w.fetched.count("Visual_Floor_1") == 2
    w.mqtt.publish_map_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_pose_change_republishes_without_refetch():
    """A moving LiveLocation republishes using the cached geometry."""
    w = _Wired()
    await w.run_polls(
        [_raw_device(), _raw_device(live=(3.0, 4.0, 0.1))],
    )

    assert w.fetched.count("Visual_Floor_1") == 1
    assert w.mqtt.publish_map_image.await_count == 2
    first = w.mqtt.publish_map_image.await_args_list[0].args[1]
    second = w.mqtt.publish_map_image.await_args_list[1].args[1]
    assert first == PNG
    assert second == PNG
    # Second render: cached geometry with the new live pose
    rendered = w.vfm.render_floor_map_pillow.await_args.args[0]
    assert rendered["grid"] == GRID_A
    assert rendered["pose"] == (3.0, 4.0, 0.1)


@pytest.mark.asyncio
async def test_parse_error_is_swallowed_and_retried_next_poll():
    """A parse failure must not crash the loop, and the timestamp is not
    recorded, so the next poll re-fetches."""
    w = _Wired()
    w.vfm.parse_floor_map_bytes.side_effect = ValueError("bad bin")

    await w.run_polls([_raw_device(), _raw_device()])

    assert w.fetched.count("Visual_Floor_1") == 2
    w.vfm.parse_floor_map_bytes.assert_called()
    w.mqtt.publish_map_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_error_is_swallowed():
    """An MQTT failure during publish must not crash the poll loop."""
    w = _Wired()
    w.mqtt.publish_map_image.side_effect = aiomqtt.MqttError("boom")

    # Two full poll cycles survive
    await w.run_polls([_raw_device(), _raw_device()])
    assert w.mqtt.publish_map_image.await_count == 2


@pytest.mark.asyncio
async def test_missing_floor_file_publishes_nothing():
    """When the device has no Visual_Floor_1 file, nothing is published."""
    w = _Wired()

    async def fetch(dsn: str, name: str, **kwargs: Any) -> bytes:
        w.fetched.append(name)
        return b""

    w.api.fetch_property_file.side_effect = fetch
    await w.run_polls([_raw_device()])

    w.vfm.parse_floor_map_bytes.assert_not_called()
    w.mqtt.publish_map_image.assert_not_awaited()


# --- Unit tests for the helpers -------------------------------------------


def test_map_geometry_keeps_only_static_keys():
    parsed = _parsed_map()
    parsed["extra"] = "not static"
    geom = _map_geometry(parsed)
    assert set(geom) == {"name", "map_id", "grid", "zones", "boundaries"}
    assert geom["grid"] == GRID_A
    assert "pose" not in geom


@pytest.mark.asyncio
async def test_fetch_visual_floor_returns_bytes():
    api = AsyncMock()
    api.fetch_property_file.return_value = FLOOR_BIN
    body = await _fetch_skegox_visual_floor(api, "DSN1", "Name")
    assert body == FLOOR_BIN
    api.fetch_property_file.assert_awaited_once_with(
        "DSN1", "Visual_Floor_1", cache_bust=True,
    )


@pytest.mark.asyncio
async def test_fetch_visual_floor_returns_empty_on_error():
    api = AsyncMock()
    api.fetch_property_file.side_effect = OSError("network down")
    assert await _fetch_skegox_visual_floor(api, "DSN1", "Name") == b""


@pytest.mark.asyncio
async def test_fetch_visual_floor_returns_empty_on_empty_body():
    api = AsyncMock()
    api.fetch_property_file.return_value = b""
    assert await _fetch_skegox_visual_floor(api, "DSN1", "Name") == b""
