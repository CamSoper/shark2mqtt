"""Tests for src.visualize_floor_map.

The module exposes a single ``VisualizeFloorMap`` class whose decode/parse/
render methods are all ``async``; tests drive them via pytest-asyncio.
"""

import io
import os
import struct

import numpy as np
import pytest
from PIL import Image
from src.visualize_floor_map import VisualizeFloorMap
from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor

palette = ColorsPalette()


def _rgb(color_name):
    return tuple(palette.get_color(color_name)[:3])


@pytest.fixture
def vm():
    return VisualizeFloorMap()


def _parsed(rows=10, cols=12):
    row = lambda b: bytes([b]) * cols
    cells = (
        row(0x64)[:2] + row(0x00)[:8] + row(0x64)[:2]  # wall corners
        + row(0x00)
        + row(0x00)[:4] + row(0x4B)[:4] + row(0x00)[:4]  # navigable strip
        + row(0x56)  # virtual wall
        + row(0x00)
        + row(0x00)
    )
    cells = cells[: rows * cols].ljust(rows * cols, b"\x00")
    grid = {
        "resolution": 0.01,
        "origin": (0.0, 0.0),
        "width": rows,
        "height": cols,
        "cells": cells,
    }
    return {
        "name": "t",
        "map_id": "m",
        "grid": grid,
        "zones": [
            {
                "zone_id": 1,
                "zone_name": "Kitchen",
                "boundary": [
                    (0.02, 0.02),
                    (0.10, 0.02),
                    (0.10, 0.08),
                    (0.02, 0.08),
                ],
            }
        ],
        "boundaries": [
            [
                (0.11, 0.02),
                (0.115, 0.02),
                (0.115, 0.08),
                (0.11, 0.08),
            ]
        ],
        "pose": (0.06, 0.05, 1.5708),
    }


def _pixel(img, parsed, col, cell_row):
    """Pixel for a grid cell after the world-y -> image-y flip."""
    rows = parsed["grid"]["width"]
    cols = parsed["grid"]["height"]
    w, h = img.size
    px = min(w - 1, round(col / cols * (w - 1)))
    py = min(h - 1, round((1 - (cell_row + 0.5) / rows) * (h - 1)))
    return img.getpixel((px, py))


def _f32(value):
    return struct.pack("<f", value)


def _point_msg(x, y, index=None):
    """Build the raw bytes of one point message.

    The base point has field 1 = x and field 2 = y (fixed32 floats). Some
    models append a point index as field 3 (varint); pass `index` to add it.
    """
    msg = b"\x0d" + _f32(x) + b"\x15" + _f32(y)
    if index is not None:
        msg += b"\x18" + bytes([index])
    return msg


def _points_msg(points):
    """Build a points message: each point wrapped in a field 1 (LEN) message."""
    out = b""
    for x, y, index in points:
        p = _point_msg(x, y, index)
        out += b"\x0a" + bytes([len(p)]) + p
    return out


# ---------------------------------------------------------------------------
# build_grid_image / cell LUT
# ---------------------------------------------------------------------------


def test_build_grid_image_uses_header_width_as_row_count(vm):
    # Header "width" = cell rows (y-dir), "height" = cols (x-dir).
    grid = {"width": 4, "height": 3, "cells": bytes(range(12))}
    img = vm.build_grid_image(grid)
    assert img.shape == (4, 3)
    assert img[3, 2] == 8  # 0x0B unmapped -> "other"
    assert img[0, 0] == 0  # 0x00 -> free


def test_build_grid_image_rejects_short_cells_buffer(vm):
    grid = {"width": 4, "height": 3, "cells": b"\x00" * 5}
    with pytest.raises(ValueError, match="cells buffer too short"):
        vm.build_grid_image(grid)


def test_cell_lut_maps_known_bytes_after_init(vm):
    assert vm._CELL_LUT[0x64] == 6  # wall
    assert vm._CELL_LUT[0x56] == 7  # virtual wall
    assert vm._CELL_LUT[0x4B] == 3  # navigable
    assert vm._CELL_LUT[0xAB] == 8  # unmapped -> other


# ---------------------------------------------------------------------------
# Low-level protobuf decoders
# ---------------------------------------------------------------------------


def test_decode_varint_single_and_multi_byte(vm):
    assert vm.decode_varint(b"\x01", 0) == (1, 1)
    # 300 = 0xAC 0x02 (low 7 bits 0x2C, then 0x02)
    assert vm.decode_varint(b"\xac\x02", 0) == (300, 2)
    # Offset is preserved and advanced.
    assert vm.decode_varint(b"\x00\xac\x02", 1) == (300, 3)


def test_decode_point2d(vm):
    pt = _point_msg(1.5, -2.25)
    assert vm.decode_point2d(pt) == pytest.approx((1.5, -2.25))



def test_decode_pose_ignores_incomplete_pose(vm):
    payload = b"\x0d" + _f32(1.0) + b"\x15" + _f32(2.0)
    assert vm.decode_pose(payload) is None
    # Wrong leading tag is also rejected.
    bad = b"\x15" + _f32(1.0) + b"\x15" + _f32(2.0) + b"\x1d" + _f32(3.0)
    assert vm.decode_pose(bad) is None
    complete = payload + b"\x1d" + _f32(3.0)
    assert vm.decode_pose(complete) == pytest.approx((1.0, 2.0, 3.0))



def test_decode_occupancy_grid(vm):
    origin = b"\x0d" + _f32(-0.5) + b"\x15" + _f32(1.25)
    cells = b"\x00" * 6
    buf = (
        b"\x0d" + _f32(0.06)  # field 1: resolution
        + b"\x12" + bytes([len(origin)]) + origin  # field 2: origin
        + b"\x18\x03"  # field 3: height (cols)
        + b"\x20\x02"  # field 4: width (rows)
        + b"\x28\x01"  # field 5: skipped varint
        + b"\x32" + bytes([len(cells)]) + cells  # field 6: cells
    )
    grid = vm.decode_occupancy_grid(buf)
    assert grid["resolution"] == pytest.approx(0.06)
    assert grid["origin"] == pytest.approx((-0.5, 1.25))
    assert grid["height"] == 3
    assert grid["width"] == 2
    assert grid["cells"] == cells



def test_decode_polygon_points(vm):
    # Each 12-byte record: 0x0a (field 1 LEN) + 0x0a (len) + point(x, y).
    buf = _points_msg([(1.0, 2.0, None), (3.0, 4.0, None), (5.0, 6.0, None)])
    assert vm.decode_polygon_points(buf) == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    # Too short to contain a full record.
    assert vm.decode_polygon_points(b"\x0a\x0a\x0d\x00\x01") == []
    # A partial header (0x0a 0x0a without the 0x0d) is skipped, then matched.
    buf = b"\x0a\x0a" + b"\x0a" + _point_msg(1.0, 2.0)
    assert vm.decode_polygon_points(buf) == [(1.0, 2.0)]



def test_decode_message_varint_length_and_fixed32(vm):
    fields = vm._decode_message(b"\x08\x2a\x12\x03abc\x0d\x00\x01\x02\x03")
    assert fields == [(1, 0, 42), (2, 2, b"abc"), (1, 5, None)]



def test_decode_message_rejects_invalid_input(vm):
    with pytest.raises(ValueError, match="invalid protobuf tag"):
        vm._decode_message(b"\x00")
    with pytest.raises(ValueError, match="truncated field"):
        vm._decode_message(b"\x12" + bytes([10]) + b"ab")
    with pytest.raises(ValueError, match="truncated field"):
        vm._decode_message(b"\x15" + bytes([10]) + b"ab")
    # Field 1 with group-start wire type (3) is unsupported.
    with pytest.raises(ValueError, match="unsupported wire type"):
        vm._decode_message(b"\x0b")



def test_collect_points_matches_floats_by_field_number(vm):
    # Points with an extra index varint (field 3) still decode by field number.
    sub = _points_msg([(1.0, 2.0, 7), (3.0, 4.0, 8)])
    assert vm._collect_points(sub) == [(1.0, 2.0), (3.0, 4.0)]
    # Garbage payload degrades to no points instead of raising.
    assert vm._collect_points(b"\x00") == []


# ---------------------------------------------------------------------------
# Boundary / zone decoding
# ---------------------------------------------------------------------------



def test_decode_boundary_payload_plain_points(vm):
    # Boundary-only layout: a single nested message (field 4) of points.
    pts = _points_msg([(1.0, 2.0, None), (3.0, 4.0, None), (5.0, 6.0, None)])
    payload = b"\x22" + bytes([len(pts)]) + pts
    assert vm.decode_boundary_payload(payload) == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]



def test_decode_boundary_payload_named_edge_with_index(vm):
    # Named edge/door layout: field 2 = label, field 4 = points, and each
    # point carries an extra index varint (field 3).
    pts = _points_msg([(1.0, 2.0, 0), (3.0, 4.0, 1), (5.0, 6.0, 2)])
    payload = b"\x12\x04edge\x22" + bytes([len(pts)]) + pts
    assert vm.decode_boundary_payload(payload) == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]



def test_decode_boundary_payload_fallback_and_invalid(vm):
    # Fallback: the payload itself is a points message (no field 4 wrapper).
    pts = _points_msg([(9.0, 8.0, None)])
    assert vm.decode_boundary_payload(pts) == [(9.0, 8.0)]
    # Invalid payload degrades to an empty list.
    assert vm.decode_boundary_payload(b"\x00") == []



def test_decode_zone_prefers_nested_field16_name(vm):
    # Wet/deep model layout: fields 2/3 are "AZ_<n>" placeholders, the real
    # room name lives in the nested string field 16, which comes *after* the
    # boundary, field 5 and the repeated field-13 list.
    zone = (
        b"\x08\x09"  # field 1 type=9
        + b"\x12\x04AZ_3"  # field 2 zone_id
        + b"\x1a\x04AZ_3"  # field 3 zone_name (placeholder)
        + b"\x22\x04\x0a\x02"  # field 4 boundary (stub)
        + b"\x28\x02"  # field 5 varint
        + b"\x6a\x04AZ_1\x6a\x04AZ_5"  # field 13 repeated neighbor ids
        + b"\x82\x01\x07Kitchen"  # field 16 string name
    )
    result = vm.decode_zone(zone)
    assert result["zone_id"] == "AZ_3"
    assert result["zone_name"] == "Kitchen"
    assert result["type"] == 9
    assert result["boundary"] == []



def test_decode_zone_uses_field3_name_when_no_field16(vm):
    # Dry-only model layout: field 3 holds the real name, no field 16.
    zone = (
        b"\x12\x07Hallway"  # field 2 zone_id
        + b"\x1a\x07Hallway"  # field 3 zone_name
        + b"\x22\x04\x0a\x02"  # field 4 boundary (stub)
        + b"\x28\x01"  # field 5 varint
    )
    result = vm.decode_zone(zone)
    assert result["zone_name"] == "Hallway"



def test_decode_zone_zone_name_skips_placeholders(vm):
    # AZ_ placeholders in field 3 are ignored; no field 16 -> None.
    assert vm._decode_zone_zone_name(b"\x1a\x04AZ_1") is None
    # Non-placeholder field 3 name is returned.
    assert vm._decode_zone_zone_name(b"\x1a\x06Office") == "Office"


def test_decode_zone_zone_name_value_error(vm):
    assert vm._decode_zone_zone_name(b"\x00") == None

# ---------------------------------------------------------------------------
# Grid selection
# ---------------------------------------------------------------------------


def test_grid_world_extent(vm):
    grid = {"resolution": 0.06, "origin": (-1.0, -2.0), "width": 10, "height": 20}
    # width = rows (y), height = cols (x).
    assert vm._grid_world_extent(grid) == pytest.approx((-1.0, 0.2, -2.0, -1.4))


def test_grid_zone_coverage(vm):
    zones = [
        {
            "zone_name": "Kitchen",
            "boundary": [(5.0, 5.0), (6.0, 5.0), (-5.0, -5.0), (9.0, 9.0)],
        }
    ]
    small = {"resolution": 0.06, "origin": (0.0, 0.0), "width": 10, "height": 10}
    big = {"resolution": 0.06, "origin": (-10.0, -10.0), "width": 300, "height": 300}
    # 3 of 4 points inside the big grid ([-10, 8]^2), none inside the small one.
    assert vm._grid_zone_coverage(small, zones) == 0.0
    assert vm._grid_zone_coverage(big, zones) == pytest.approx(0.75)
    # Degenerate extent -> 0.
    assert vm._grid_zone_coverage({"resolution": 0.0, "origin": (0, 0), "width": 1, "height": 1}, zones) == 0.0
    # No zone points -> 0.
    assert vm._grid_zone_coverage(big, []) == 0.0



def test_pick_grid_prefers_full_map_over_partial(vm):
    zones = [
        {
            "zone_name": "Kitchen",
            "boundary": [(5.0, 5.0), (6.0, 5.0), (6.0, 6.0)],
        }
    ]
    # A tiny grid that does not contain the zone.
    partial = {
        "resolution": 0.06,
        "origin": (0.0, 0.0),
        "width": 10,
        "height": 10,
        "cells": b"\x00" * 100,
    }
    # A large grid that contains the zone.
    full = {
        "resolution": 0.06,
        "origin": (-10.0, -10.0),
        "width": 300,
        "height": 300,
        "cells": b"\x00" * 90000,
    }
    assert vm._grid_zone_coverage(partial, zones) == 0.0
    assert vm._grid_zone_coverage(full, zones) == 1.0
    assert vm._pick_grid([partial, full], zones) is full
    # Order must not matter.
    assert vm._pick_grid([full, partial], zones) is full



def test_pick_grid_rejects_misparse_with_bad_dimensions(vm):
    zones = [
        {
            "zone_name": "Kitchen",
            "boundary": [(5.0, 5.0), (6.0, 5.0), (6.0, 6.0)],
        }
    ]
    good = {
        "resolution": 0.06,
        "origin": (-10.0, -10.0),
        "width": 300,
        "height": 300,
        "cells": b"\x00" * 90000,
    }
    # A mis-decoded grid whose dimensions exceed its cell buffer.
    bad = {
        "resolution": 0.06,
        "origin": (-10.0, -10.0),
        "width": 4294967295,
        "height": 4294967295,
        "cells": b"\x00" * 100,
    }
    assert vm._pick_grid([good, bad], zones) is good
    # All candidates invalid -> fall back to the first one.
    assert vm._pick_grid([bad], zones) is bad


# ---------------------------------------------------------------------------
# Full parse
# ---------------------------------------------------------------------------


def _synthetic_floor_map_bytes() -> bytes:
    """Build a minimal well-formed floor map binary for parse tests."""
    origin = b"\x0d" + _f32(0.0) + b"\x15" + _f32(0.0)
    cells = bytes([0x64, 0x00, 0x00, 0x00, 0x4B, 0x00])  # 2 rows x 3 cols
    grid = (
        b"\x0d" + _f32(0.06)  # resolution
        + b"\x12" + bytes([len(origin)]) + origin  # origin
        + b"\x18\x03"  # height (cols)
        + b"\x20\x02"  # width (rows)
        + b"\x28\x00"  # skipped varint
        + b"\x32" + bytes([len(cells)]) + cells  # cells
    )
    pose = b"\x0d" + _f32(1.0) + b"\x15" + _f32(2.0) + b"\x1d" + _f32(0.5)
    zone = b"\x12\x02z1" + b"\x1a\x07Kitchen"
    pts = _points_msg([(1.0, 2.0, None), (3.0, 4.0, None), (5.0, 6.0, None)])
    boundary = b"\x22" + bytes([len(pts)]) + pts
    # A second, smaller valid grid in field 21 (extra grid candidate).
    grid2 = b"\x0d" + _f32(0.01) + b"\x18\x01" + b"\x20\x01" + b"\x32\x01" + b"\x00"
    return (
        b"\x08\x01"  # field 1: sequence
        + b"\x12\x04" + b"test"  # field 2: name
        + b"\x1a\x05" + b"mapid"  # field 3: map_id
        + b"\x20\x01"  # field 4: map_type
        + b"\x2a" + bytes([len(grid)]) + grid  # field 5: primary grid
        + b"\x3a" + bytes([len(pose)]) + pose  # field 7: pose
        + b"\x7a" + bytes([len(zone)]) + zone  # field 15: zone
        + b"\xe2\x01" + bytes([len(boundary)]) + boundary  # field 28: boundary
        + b"\x30\x05"  # field 6: varint (ignored by the parse loop)
        + b"\x45\x00\x01\x02\x03"  # field 8: fixed32 (ignored by the parse loop)
        + b"\xaa\x01" + bytes([len(grid2)]) + grid2  # field 21: extra grid
        + b"\x4b"  # field 9: group-start wire type -> parse loop breaks
    )


@pytest.mark.asyncio
async def test_parse_floor_map_bytes(vm):
    result = await vm.parse_floor_map_bytes(_synthetic_floor_map_bytes())
    assert result["name"] == "test"
    assert result["map_id"] == "mapid"
    assert result["pose"] == pytest.approx((1.0, 2.0, 0.5))
    grid = result["grid"]
    assert grid["width"] == 2
    assert grid["height"] == 3
    assert len(grid["cells"]) == 6
    assert result["zones"] == [{"zone_id": "z1", "zone_name": "Kitchen"}]
    assert result["boundaries"] == [[(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]]


@pytest.mark.asyncio
async def test_parse_floor_map_reads_file(vm, tmp_path):
    bin_path = tmp_path / "floor.bin"
    bin_path.write_bytes(_synthetic_floor_map_bytes())
    result = await vm.parse_floor_map(bin_path)
    assert result["name"] == "test"
    assert result["map_id"] == "mapid"
    assert result["pose"] == pytest.approx((1.0, 2.0, 0.5))


@pytest.mark.asyncio
async def test_parse_floor_map_bytes_ignores_bad_extra_grid(vm):
    # A field-21 payload that is not a valid occupancy grid must be skipped.
    data = _synthetic_floor_map_bytes()
    # The field-5 grid ends at offset 50 (header 17 + tag/len 2 + grid 31).
    head, tail = data[:50], data[50:]
    bad_grid = b"\xaa\x01\x04\x0d\x01\x02\x03"  # field 21 LEN, truncated fixed32
    parsed = await vm.parse_floor_map_bytes(head + bad_grid + tail)
    assert parsed["name"] == "test"
    # The real primary grid is still selected and the map stays parseable.
    assert parsed["grid"]["width"] == 2
    assert parsed["pose"] == pytest.approx((1.0, 2.0, 0.5))


# ---------------------------------------------------------------------------
# Pillow renderer (HA MQTT image path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_pillow_cell_categories_use_roborock_palette(vm):
    parsed = _parsed()
    parsed["zones"] = []
    parsed["boundaries"] = []
    parsed["pose"] = None
    img = Image.open(io.BytesIO(await vm.render_floor_map_pillow(parsed))).convert("RGB")
    assert _pixel(img, parsed, 0, 0) == _rgb(SupportedColor.MAP_WALL)
    assert _pixel(img, parsed, 0, 3) == _rgb(SupportedColor.VIRTUAL_WALLS)
    assert _pixel(img, parsed, 4, 2) == _rgb(SupportedColor.MAP_INSIDE)
    assert _pixel(img, parsed, 3, 2) == _rgb(SupportedColor.MAP_OUTSIDE)


@pytest.mark.asyncio
async def test_render_pillow_zone_and_obstacle_overlays(vm):
    parsed = _parsed()
    with_overlay = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(parsed))).convert("RGB")
    )
    assert with_overlay.size > 0

    # Zones change the image when present vs absent.
    no_zones = _parsed()
    no_zones["zones"] = []
    no_zones["boundaries"] = []
    no_zones["pose"] = None
    plain = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(no_zones))).convert("RGB")
    )
    assert not np.array_equal(plain, with_overlay)

    # The obstacle overlay (black @ alpha 128) halves the floor brightness.
    full = _parsed()
    full["zones"] = []
    full["pose"] = None
    with_obst = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(full))).convert("RGB")
    )
    # some pixels must be strictly darker where the obstacle sits
    assert np.any(with_obst < plain)


@pytest.mark.asyncio
async def test_render_pillow_returns_png_bytes_for_bytesio(vm):
    parsed = _parsed()
    buf = io.BytesIO()
    png = await vm.render_floor_map_pillow(parsed, buf)
    assert png == buf.getvalue()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"



@pytest.mark.asyncio
async def test_render_pillow_returns_png_bytes_for_file_path(vm, tmp_path):
    out = tmp_path / "floor.png"
    png = await vm.render_floor_map_pillow(_parsed(), output=out)
    assert png == out.read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_render_pillow_without_output_writes_no_file(vm, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    png = await vm.render_floor_map_pillow(_parsed(), dpi=150)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_render_pillow_robot_sprite_drawn(vm):
    parsed = _parsed()
    parsed["zones"] = []
    parsed["boundaries"] = []
    parsed["pose"] = None
    without = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(parsed))).convert("RGB")
    )
    parsed["pose"] = (0.06, 0.05, 1.5708)
    with_ = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(parsed))).convert("RGB")
    )
    assert not np.array_equal(without, with_)
    # the robot body fill appears in the sprite render
    robo = np.array(_rgb(SupportedColor.ROBO))
    assert np.any(np.all(with_ == robo, axis=-1))


@pytest.mark.asyncio
async def test_render_pillow_respects_show_flags(vm):
    base = _parsed()
    base["pose"] = None
    full = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(base))).convert("RGB")
    )
    stripped = _parsed()
    stripped["pose"] = None
    none = np.asarray(
        Image.open(
            io.BytesIO(
                await vm.render_floor_map_pillow(
                    stripped, show_zones=False, show_boundaries=False
                )
            )
        ).convert("RGB")
    )
    assert not np.array_equal(full, none)
    # With everything hidden the image is identical to a map with no zones/
    # obstacles at all.
    empty = _parsed()
    empty["pose"] = None
    empty["zones"] = []
    empty["boundaries"] = []
    empty_img = np.asarray(
        Image.open(io.BytesIO(await vm.render_floor_map_pillow(empty))).convert("RGB")
    )
    assert np.array_equal(none, empty_img)



def test_draw_robot_uses_draw_primitives(vm):
    from unittest.mock import MagicMock

    draw = MagicMock()
    vm._draw_robot(draw, 20, 20, 90.0, 16, (0, 0, 0), (255, 255, 255))
    # body + secondary ring (r >= 8) + lidar + button ellipses, bin cover line
    assert draw.ellipse.call_count == 4
    assert draw.line.call_count == 1


# ---------------------------------------------------------------------------
# Matplotlib renderer + CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_floor_map_saves_png(vm, tmp_path):
    out = tmp_path / "floor_plan.png"
    await vm.render_floor_map(_parsed(), output_path=str(out), dpi=100)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

@pytest.mark.asyncio
async def test_render_floor_map_saves_default_png(vm, tmp_path, monkeypatch):
    out = tmp_path /"floor_map_visual.png"
    monkeypatch.chdir(tmp_path)
    assert os.getcwd() == str(tmp_path)
    await vm.render_floor_map(_parsed(), output_path=None, dpi=100)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

@pytest.mark.asyncio
async def test_render_floor_map_hides_zones_and_boundaries(vm, tmp_path):
    out = tmp_path / "plain.png"
    await vm.render_floor_map(
        _parsed(), output_path=str(out), dpi=100, show_zones=False, show_boundaries=False
    )
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

