"""Render a robot's floor plan from its MARD file as an SVG image.

MARD (Mobile_App_Room_Definition) is the JSON file the Shark app draws its
room map from. Besides the room names shark2mqtt already reads, it carries:

- ``areas[].points``: each room's outline as a polygon, in cells of the
  robot's floor grid, plus ``area_size`` in square metres.
- ``mapStatePbFloorBase64String``: a base64 protobuf holding the robot's
  floor scan. Field 5 is the occupancy grid: 1 = metres per cell (0.06 on
  the models seen so far), 2 = origin {1: x, 2: y} in metres, 3 = width,
  4 = height, 6 = one byte per cell, row-major from the top.

Cell values, as observed on RV2800ZEUK and RV2800YEUK units (inferred, not
documented by Shark): 75 unexplored, 0 and 15 explored floor, 100 wall or
obstacle, 90/92 rare edge values drawn as faint obstacles.

The output is SVG so room names stay crisp text; Home Assistant's MQTT image
platform serves it with ``content_type: image/svg+xml``. Nothing here needs a
third-party library.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import zlib
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

PX_PER_CELL = 5          # 6 cm cells -> 5 px
PAD = 14                 # px around the plan
FOOTER = 40              # px for the summary line
ROOM_COLOURS = ("#4C9F8A", "#D9894A", "#6E7FC4", "#C4607A",
                "#8FA64B", "#B48A3C", "#5BA3C9", "#9C6BB5")
SCAN_PALETTE = {0: (236, 240, 238, 255), 15: (214, 226, 221, 255),
                100: (40, 52, 58, 255), 92: (120, 132, 138, 255), 90: (150, 160, 165, 255)}


# --- protobuf (just enough for the floor grid) ---------------------------------

def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return v, i


def _fields(b: bytes) -> dict[int, Any]:
    """Top-level fields of a protobuf message (first occurrence of each)."""
    i, out = 0, {}
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]
            i += 8
        elif wire == 5:
            v = struct.unpack("<f", b[i:i + 4])[0]
            i += 4
        elif wire == 2:
            n, i = _varint(b, i)
            v = b[i:i + n]
            i += n
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(num, v)
    return out


def decode_floor_grid(mard: dict[str, Any]) -> tuple[int, int, bytes] | None:
    """Return (width, height, cells) of the robot's floor scan, or None."""
    blob = mard.get("mapStatePbFloorBase64String")
    if not isinstance(blob, str) or not blob:
        return None
    try:
        grid = _fields(_fields(base64.b64decode(blob))[5])
        w, h, cells = int(grid[3]), int(grid[4]), grid[6]
    except Exception:  # noqa: BLE001 - an unreadable scan just means rooms only
        logger.debug("Could not decode the floor scan in MARD", exc_info=True)
        return None
    if not isinstance(cells, bytes) or w <= 0 or h <= 0 or len(cells) != w * h:
        return None
    return w, h, cells


def _scan_png(w: int, h: int, cells: bytes) -> bytes:
    """The scan as an RGBA PNG, one pixel per cell, unexplored transparent."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: none
        for x in range(w):
            raw += bytes(SCAN_PALETTE.get(cells[y * w + x], (0, 0, 0, 0)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# --- rooms ---------------------------------------------------------------------

def _rooms(mard: dict[str, Any]) -> list[dict[str, Any]]:
    """User rooms with a usable outline: name, area (m²), points [(x, y)]."""
    rooms = []
    for area in mard.get("areas", []) or []:
        if not isinstance(area, dict):
            continue
        if not str(area.get("area_meta_data", "")).startswith("UserRoom:"):
            continue
        pts = []
        for p in area.get("points", []) or []:
            try:
                pts.append((float(p["x"]), float(p["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(pts) < 3:
            continue
        name = area.get("user_room_name") or area.get("robot_room_name") or "Room"
        try:
            size = float(area.get("area_size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        rooms.append({"name": str(name), "area": size, "points": pts})
    return rooms


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid, so labels land inside L-shaped rooms."""
    a = cx = cy = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-9:
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return cx / (3 * a), cy / (3 * a)


# --- public API ----------------------------------------------------------------

def render_map_svg(body: bytes, show_scan: bool = False) -> str | None:
    """Render a MARD file as an SVG floor plan, or None if it has no rooms.

    Rooms are drawn as tinted outlines labelled with their name and area. With
    ``show_scan`` the robot's floor scan (walls and explored floor) is drawn
    underneath. The canvas is square with the plan centred, which displays
    predictably in Home Assistant's picture cards.
    """
    try:
        mard = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(mard, dict):
        return None
    rooms = _rooms(mard)
    if not rooms:
        return None

    s = PX_PER_CELL
    xs = [x for r in rooms for x, _ in r["points"]]
    ys = [y for r in rooms for _, y in r["points"]]
    grid = decode_floor_grid(mard) if show_scan else None
    if grid:
        # frame everything the robot explored, not just the rooms
        gw, gh, cells = grid
        seen = [i for i, v in enumerate(cells) if v in SCAN_PALETTE]
        if seen:
            xs += [min(i % gw for i in seen), max(i % gw for i in seen) + 1]
            ys += [seen[0] // gw, seen[-1] // gw + 1]
    x0, y0 = min(xs) * s - PAD, min(ys) * s - PAD
    w = (max(xs) - min(xs)) * s + 2 * PAD
    h = (max(ys) - min(ys)) * s + 2 * PAD
    side = max(w, h + FOOTER)
    dx, dy = (side - w) / 2, (side - (h + FOOTER)) / 2

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side:.0f} {side:.0f}" '
        f'width="{side:.0f}" height="{side:.0f}" '
        f'font-family="Helvetica Neue, Helvetica, Arial, sans-serif">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<g transform="translate({dx - x0:.1f},{dy - y0:.1f})">',
    ]

    if grid:
        png = base64.b64encode(_scan_png(gw, gh, cells)).decode()
        out.append(f'<image href="data:image/png;base64,{png}" width="{gw * s}" '
                   f'height="{gh * s}" style="image-rendering:pixelated"/>')

    fill_opacity = "0.30" if show_scan else "0.22"
    for i, r in enumerate(rooms):
        pts = " ".join(f"{x * s:.1f},{y * s:.1f}" for x, y in r["points"])
        c = ROOM_COLOURS[i % len(ROOM_COLOURS)]
        out.append(f'<polygon points="{pts}" fill="{c}" fill-opacity="{fill_opacity}" '
                   f'stroke="{c}" stroke-width="3" stroke-linejoin="round"/>')

    for r in rooms:
        cx, cy = _centroid(r["points"])
        big = r["area"] >= 6
        halo = ' stroke="#ffffff" stroke-width="4" paint-order="stroke"' if show_scan else ""
        out.append(f'<text x="{cx * s:.0f}" y="{cy * s - 4:.0f}" text-anchor="middle" '
                   f'font-size="{21 if big else 17}" font-weight="700" fill="#1d2b2a"{halo}>'
                   f'{escape(r["name"])}</text>')
        if r["area"]:
            out.append(f'<text x="{cx * s:.0f}" y="{cy * s + 18:.0f}" text-anchor="middle" '
                       f'font-size="{16 if big else 14}" fill="#4a5a58"{halo}>'
                       f'{r["area"]:.1f} m²</text>')
    out.append("</g>")

    total = sum(r["area"] for r in rooms)
    summary = f'{len(rooms)} room{"s" if len(rooms) != 1 else ""}'
    if total:
        summary += f" · {total:.1f} m² mapped"
    out.append(f'<text x="{dx + PAD:.0f}" y="{dy + h + 26:.0f}" font-size="15" '
               f'fill="#4a5a58">{summary}</text>')
    out.append("</svg>")
    return "\n".join(out)
