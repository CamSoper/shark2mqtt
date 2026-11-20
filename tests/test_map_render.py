"""Tests for the floor plan renderer and its MQTT publishing.

Every MARD here is synthetic: a made-up two-room flat, with a small floor
scan encoded the same way the robots encode theirs.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ayla_api import MardData, parse_mard
from src.main import _publish_floor_plan
from src.map_render import decode_floor_grid, render_map_svg
from src.mqtt_client import MqttClient


# --- building synthetic MARD files ------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, value) -> bytes:
    if isinstance(value, float):
        return _varint(num << 3 | 5) + struct.pack("<f", value)
    if isinstance(value, int):
        return _varint(num << 3 | 0) + _varint(value)
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def make_scan(width: int, height: int, cells: bytes) -> str:
    """A base64 floor protobuf: field 5 holds the grid, like the robots send."""
    origin = _field(1, -1.5) + _field(2, -2.0)
    grid = (_field(1, 0.06) + _field(2, origin) + _field(3, width)
            + _field(4, height) + _field(6, cells))
    msg = _field(1, 1234) + _field(2, b"unnamed") + _field(3, b"FLOOR01") + _field(5, grid)
    return base64.b64encode(msg).decode()


def room(name: str, x0: int, y0: int, x1: int, y1: int, size: float, rid: str) -> dict:
    return {
        "area_meta_data": f"UserRoom:{rid}", "robot_room_name": rid,
        "user_room_name": name, "area_size": size, "uuid": rid,
        "points": [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}],
    }


def make_mard(rooms: list[dict], scan: str | None = None) -> bytes:
    mard = {"version": "1.1", "floor_id": "FLOOR01", "map_rotation": 0, "areas": rooms}
    if scan is not None:
        mard["mapStatePbFloorBase64String"] = scan
    return json.dumps(mard).encode()


FLAT = [room("Lounge", 2, 2, 20, 14, 7.8, "AZ_1"), room("Galley Kitchen", 20, 2, 30, 14, 4.3, "AZ_2")]


# --- decoding -----------------------------------------------------------------

def test_floor_grid_round_trips():
    cells = bytes([75] * 10 + [15] * 10 + [100] * 10)
    w, h, got = decode_floor_grid(json.loads(make_mard(FLAT, make_scan(10, 3, cells))))
    assert (w, h) == (10, 3)
    assert got == cells


@pytest.mark.parametrize("scan", [None, "", "not base64!!", base64.b64encode(b"\x0a\x02hi").decode()])
def test_missing_or_broken_scan_is_none(scan):
    mard = json.loads(make_mard(FLAT, scan))
    assert decode_floor_grid(mard) is None


def test_grid_with_wrong_cell_count_is_rejected():
    mard = json.loads(make_mard(FLAT, make_scan(10, 3, bytes(5))))
    assert decode_floor_grid(mard) is None


def test_parse_mard_keeps_the_file():
    body = make_mard(FLAT)
    assert parse_mard(body, "Test", "DSN1").raw == body


# --- rendering ----------------------------------------------------------------

def test_renders_each_room_with_name_and_area():
    svg = render_map_svg(make_mard(FLAT))
    assert svg is not None and svg.startswith("<svg")
    assert svg.count("<polygon") == 2
    assert "Lounge" in svg and "Galley Kitchen" in svg
    assert "7.8 m²" in svg and "4.3 m²" in svg
    assert "2 rooms · 12.1 m² mapped" in svg
    assert "<image" not in svg  # no scan unless asked


def test_canvas_is_square():
    svg = render_map_svg(make_mard(FLAT))
    w, h = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).groups()
    assert w == h


def test_room_names_are_escaped():
    svg = render_map_svg(make_mard([room('Tom & Jerry\'s <Den>', 0, 0, 10, 10, 3.6, "AZ_1")]))
    assert "Tom &amp; Jerry's &lt;Den&gt;" in svg
    assert "<Den>" not in svg


def test_ignores_non_rooms_and_bad_outlines():
    zone = room("No-go zone", 0, 0, 5, 5, 1.0, "Z_1") | {"area_meta_data": "NoGo:Z_1"}
    line = room("Sliver", 0, 0, 5, 5, 1.0, "AZ_9") | {"points": [{"x": 0, "y": 0}, {"x": 5, "y": 0}]}
    svg = render_map_svg(make_mard(FLAT + [zone, line]))
    assert svg.count("<polygon") == 2
    assert "No-go zone" not in svg and "Sliver" not in svg


def test_scan_is_drawn_underneath_when_asked():
    cells = bytes([0, 15, 100, 75] * 25)
    svg = render_map_svg(make_mard(FLAT, make_scan(10, 10, cells)), show_scan=True)
    m = re.search(r'href="data:image/png;base64,([^"]+)"', svg)
    assert m, "scan image missing"
    assert base64.b64decode(m.group(1)).startswith(b"\x89PNG\r\n\x1a\n")
    assert svg.index("<image") < svg.index("<polygon")  # under the rooms


def test_canvas_grows_to_frame_the_whole_scan():
    # explored floor reaches well past the rooms (cells 0..49 wide)
    cells = bytes([15] * (50 * 20))
    rooms_only = render_map_svg(make_mard(FLAT, make_scan(50, 20, cells)))
    with_scan = render_map_svg(make_mard(FLAT, make_scan(50, 20, cells)), show_scan=True)
    side = lambda svg: int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
    assert side(with_scan) > side(rooms_only)


def test_broken_scan_still_draws_the_rooms():
    svg = render_map_svg(make_mard(FLAT, "garbage"), show_scan=True)
    assert svg.count("<polygon") == 2 and "<image" not in svg


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", make_mard([])])
def test_nothing_to_draw_is_none(body):
    assert render_map_svg(body) is None


# --- publishing ---------------------------------------------------------------

def _device(dsn: str = "DSN1", name: str = "Test Shark") -> MagicMock:
    dev = MagicMock()
    dev.dsn, dev.product_name = dsn, name
    dev.device_info = {"identifiers": [f"shark2mqtt_{dsn}"], "name": name}
    return dev


@pytest.fixture
def mqtt_client():
    cfg = MagicMock()
    cfg.mqtt_prefix = "shark2mqtt"
    client = MqttClient(cfg)
    client._publish = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_publish_map_announces_an_svg_image_entity(mqtt_client):
    await mqtt_client.publish_map(_device(), "<svg>plan</svg>")
    (cfg_topic, cfg), _ = mqtt_client._publish.call_args_list[0]
    assert cfg_topic == "homeassistant/image/shark2mqtt_DSN1_map/config"
    assert cfg["image_topic"] == "shark2mqtt/DSN1/map"
    assert cfg["content_type"] == "image/svg+xml"
    assert cfg["unique_id"] == "shark2mqtt_DSN1_map"
    assert cfg["object_id"] == "test_shark_floor_plan"
    (img_topic, payload), kwargs = mqtt_client._publish.call_args_list[1]
    assert (img_topic, payload, kwargs["retain"]) == ("shark2mqtt/DSN1/map", "<svg>plan</svg>", True)


@pytest.mark.asyncio
async def test_publish_map_skips_an_unchanged_plan(mqtt_client):
    dev = _device()
    await mqtt_client.publish_map(dev, "<svg>v1</svg>")
    await mqtt_client.publish_map(dev, "<svg>v1</svg>")
    assert mqtt_client._publish.await_count == 2      # config + image, once
    await mqtt_client.publish_map(dev, "<svg>v2</svg>")
    assert mqtt_client._publish.await_count == 4      # changed plan is re-sent


@pytest.mark.asyncio
async def test_floor_plan_rendered_once_per_file():
    mqtt, cfg, dev, rendered = AsyncMock(), MagicMock(map_image=True, map_show_scan=False), _device(), {}
    mard = MardData({}, ["Lounge"], "FLOOR01", make_mard(FLAT))
    await _publish_floor_plan(mqtt, cfg, dev, mard, rendered)
    await _publish_floor_plan(mqtt, cfg, dev, mard, rendered)
    assert mqtt.publish_map.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, mard", [
    (False, MardData({}, ["Lounge"], "F", make_mard(FLAT))),   # MAP_IMAGE=false
    (True, MardData({}, ["Lounge"], "F", None)),               # no file kept (e.g. Ayla prefetch)
    (True, None),                                              # no MARD at all
    (True, MardData({}, [], "F", make_mard([]))),              # file without outlines
])
async def test_floor_plan_not_published(enabled, mard):
    mqtt = AsyncMock()
    await _publish_floor_plan(mqtt, MagicMock(map_image=enabled, map_show_scan=False), _device(), mard, {})
    mqtt.publish_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_floor_plan_failure_never_breaks_polling(caplog):
    mqtt = AsyncMock()
    mqtt.publish_map.side_effect = RuntimeError("broker gone")
    mard = MardData({}, ["Lounge"], "F", make_mard(FLAT))
    await _publish_floor_plan(mqtt, MagicMock(map_image=True, map_show_scan=False), _device(), mard, {})
    assert "Could not publish floor plan" in caplog.text
