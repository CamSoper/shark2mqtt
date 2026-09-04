import io
import struct

import numpy as np
import pytest
from PIL import Image

from src.visualize_floor_map import (
    build_grid_image,
    decode_boundary_payload,
    decode_zone,
    render_floor_map_pillow,
    _grid_zone_coverage,
    _pick_grid,
)
from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor

palette = ColorsPalette()


def _rgb(color_name):
    return tuple(palette.get_color(color_name)[:3])


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


def test_build_grid_image_uses_header_width_as_row_count():
    # Header "width" = cell rows (y-dir), "height" = cols (x-dir).
    grid = {"width": 4, "height": 3, "cells": bytes(range(12))}
    img = build_grid_image(grid)
    assert img.shape == (4, 3)
    assert img[3, 2] == 8  # 0x0C unmapped -> "other"


def test_render_pillow_cell_categories_use_roborock_palette():
    parsed = _parsed()
    parsed["zones"] = []
    parsed["boundaries"] = []
    parsed["pose"] = None
    img = Image.open(io.BytesIO(render_floor_map_pillow(parsed))).convert("RGB")
    assert _pixel(img, parsed, 0, 0) == _rgb(SupportedColor.MAP_WALL)
    assert _pixel(img, parsed, 0, 3) == _rgb(SupportedColor.VIRTUAL_WALLS)
    assert _pixel(img, parsed, 4, 2) == _rgb(SupportedColor.MAP_INSIDE)
    assert _pixel(img, parsed, 3, 2) == _rgb(SupportedColor.MAP_OUTSIDE)


def test_render_pillow_zone_and_obstacle_overlays():
    parsed = _parsed()
    with_overlay = np.asarray(
        Image.open(io.BytesIO(render_floor_map_pillow(parsed))).convert("RGB")
    )
    assert with_overlay.size > 0

    # Zones change the image when present vs absent.
    no_zones = _parsed()
    no_zones["zones"] = []
    no_zones["boundaries"] = []
    no_zones["pose"] = None
    plain = np.asarray(
        Image.open(io.BytesIO(render_floor_map_pillow(no_zones))).convert("RGB")
    )
    assert not np.array_equal(plain, with_overlay)

    # The obstacle overlay (black @ alpha 128) halves the floor brightness.
    full = _parsed()
    full["zones"] = []
    full["pose"] = None
    with_obst = np.asarray(
        Image.open(io.BytesIO(render_floor_map_pillow(full))).convert("RGB")
    )
    # some pixels must be strictly darker where the obstacle sits
    assert np.any(with_obst < plain)


def test_render_pillow_returns_png_bytes_for_file_and_bytesio():
    parsed = _parsed()
    buf = io.BytesIO()
    png = render_floor_map_pillow(parsed, buf)
    assert png == buf.getvalue()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"


def test_render_pillow_without_output_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    png = render_floor_map_pillow(_parsed(), dpi=150)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert list(tmp_path.iterdir()) == []


def test_render_pillow_robot_sprite_drawn():
    parsed = _parsed()
    parsed["zones"] = []
    parsed["boundaries"] = []
    parsed["pose"] = None
    without = np.asarray(
        Image.open(io.BytesIO(render_floor_map_pillow(parsed))).convert("RGB")
    )
    parsed["pose"] = (0.06, 0.05, 1.5708)
    with_ = np.asarray(
        Image.open(io.BytesIO(render_floor_map_pillow(parsed))).convert("RGB")
    )
    assert not np.array_equal(without, with_)
    # the white robot body appears in the sprite render
    assert np.any(np.all(with_ == (255, 255, 255), axis=-1))


def test_decode_zone_prefers_nested_field16_name():
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
    result = decode_zone(zone)
    assert result["zone_id"] == "AZ_3"
    assert result["zone_name"] == "Kitchen"


def test_decode_zone_uses_field3_name_when_no_field16():
    # Dry-only model layout: field 3 holds the real name, no field 16.
    zone = (
        b"\x12\x07Hallway"  # field 2 zone_id
        + b"\x1a\x07Hallway"  # field 3 zone_name
        + b"\x22\x04\x0a\x02"  # field 4 boundary (stub)
        + b"\x28\x01"  # field 5 varint
    )
    result = decode_zone(zone)
    assert result["zone_name"] == "Hallway"


def test_decode_boundary_payload_plain_points():
    # Boundary-only layout: a single nested message (field 4) of points.
    pts = _points_msg([(1.0, 2.0, None), (3.0, 4.0, None), (5.0, 6.0, None)])
    payload = b"\x22" + bytes([len(pts)]) + pts
    assert decode_boundary_payload(payload) == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]


def test_decode_boundary_payload_named_edge_with_index():
    # Named edge/door layout: field 2 = label, field 4 = points, and each
    # point carries an extra index varint (field 3).
    pts = _points_msg([(1.0, 2.0, 0), (3.0, 4.0, 1), (5.0, 6.0, 2)])
    payload = b"\x12\x04edge\x22" + bytes([len(pts)]) + pts
    assert decode_boundary_payload(payload) == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]


def test_pick_grid_prefers_full_map_over_partial():
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
    assert _grid_zone_coverage(partial, zones) == 0.0
    assert _grid_zone_coverage(full, zones) == 1.0
    assert _pick_grid([partial, full], zones) is full
    # Order must not matter.
    assert _pick_grid([full, partial], zones) is full


def test_pick_grid_rejects_misparse_with_bad_dimensions():
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
    assert _pick_grid([good, bad], zones) is good


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
