#!/usr/bin/env python3
"""Visualize a decoded floor map as an annotated image using matplotlib.

Renders:
- Occupancy grid as a color-coded raster
- Zone polygons with semi-transparent fills and labels
- Boundary/obstacle outlines
- Robot pose marker with heading arrow
- Coordinate axes in meters
- Legend and scale bar

Usage:
    python3 visualize_floor_map.py Visual_Floor_1.bin
    python3 visualize_floor_map.py Visual_Floor_1.bin --output floor_plan.png
    python3 visualize_floor_map.py Visual_Floor_1.bin --output floor_plan.pdf
    python3 visualize_floor_map.py Visual_Floor_1.bin --dpi 200
    python3 visualize_floor_map.py Visual_Floor_1.bin --no-zones
    python3 visualize_floor_map.py Visual_Floor_1.bin --no-boundaries
"""

import argparse
import io
import logging
import math
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

try:
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Polygon as MplPolygon
except ImportError:
    logger.error("ERROR: matplotlib not installed. Run: pip install matplotlib")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Protobuf decoding (minimal, self-contained)
# ---------------------------------------------------------------------------


def decode_varint(buf, offset):
    result = 0
    shift = 0
    while offset < len(buf):
        b = buf[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, offset


def decode_point2d(buf):
    x = struct.unpack("<f", buf[1:5])[0]
    y = struct.unpack("<f", buf[6:10])[0]
    return (x, y)


def decode_occupancy_grid(buf):
    offset = 0
    grid = {}
    if buf[offset] == 0x0D:
        grid["resolution"] = struct.unpack("<f", buf[offset + 1 : offset + 5])[0]
        offset += 5
    if offset < len(buf) and buf[offset] == 0x12:
        sub_len = buf[offset + 1]
        origin_data = buf[offset + 2 : offset + 2 + sub_len]
        grid["origin"] = decode_point2d(origin_data)
        offset += 2 + sub_len
    if offset < len(buf) and buf[offset] == 0x18:
        grid["height"], offset = decode_varint(buf, offset + 1)
    if offset < len(buf) and buf[offset] == 0x20:
        grid["width"], offset = decode_varint(buf, offset + 1)
    if offset < len(buf) and buf[offset] == 0x28:
        _, offset = decode_varint(buf, offset + 1)
    if offset < len(buf) and buf[offset] == 0x32:
        cell_len, offset = decode_varint(buf, offset + 1)
        grid["cells"] = buf[offset : offset + cell_len]
        offset += cell_len
    return grid


def decode_polygon_points(buf):
    points = []
    i = 0
    while i < len(buf) - 11:
        if buf[i] == 0x0A and buf[i + 1] == 0x0A and buf[i + 2] == 0x0D:
            x = struct.unpack("<f", buf[i + 3 : i + 7])[0]
            if i + 7 < len(buf) and buf[i + 7] == 0x15:
                y = struct.unpack("<f", buf[i + 8 : i + 12])[0]
                points.append((x, y))
                i += 12
                continue
        i += 1
    return points


def _decode_message(buf):
    """Parse a protobuf message into a list of (field, wire_type, payload).

    `payload` is the raw value bytes (varint payload, message bytes, or
    None for fixed32/64). Raises ValueError when the buffer does not
    contain a well-formed message.
    """
    fields = []
    offset = 0
    while offset < len(buf):
        start = offset
        tag, offset = decode_varint(buf, offset)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            raise ValueError(f"invalid protobuf tag at offset {start}")
        if wire_type == 0:
            value, offset = decode_varint(buf, offset)
            fields.append((field_num, 0, value))
        elif wire_type == 2:
            length, offset = decode_varint(buf, offset)
            if offset + length > len(buf):
                raise ValueError(f"truncated field {field_num} at offset {start}")
            fields.append((field_num, 2, buf[offset : offset + length]))
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(buf):
                raise ValueError(f"truncated field {field_num} at offset {start}")
            fields.append((field_num, 5, None))
            offset += 4
        else:
            # 3/4 (group start/end) and 6 (group end) are not used by these
            # floor maps; stop rather than risk desyncing the reader.
            raise ValueError(f"unsupported wire type {wire_type} at offset {start}")
    return fields


def _collect_points(sub_msg):
    """Extract (x, y) float pairs from a boundary/points message.

    Each point is a nested message with at least two float fields
    (field 1 = x, field 2 = y). Some models add extra fields per point
    (e.g. an index varint), so the floats are matched by field number
    instead of by byte position.
    """
    points = []
    try:
        for field_num, wire_type, payload in _decode_message(sub_msg):
            # Each point is a message whose first two fields are the x and y
            # fixed32 floats. The minimum is 10 bytes (0x0d+4 + 0x15+4);
            # some models append extra fields (e.g. a point index), so a
            # longer payload is fine.
            if field_num == 1 and wire_type == 2 and len(payload) >= 10:
                x, y = decode_point2d(payload)
                points.append((x, y))
    except (ValueError, IndexError, struct.error):
        return []
    return points


def decode_boundary_payload(payload):
    """Decode the polygon points from a boundary/edge repeated message.

    Accepts both known encodings:
    - boundary-only payload: the first nested message (field 4) holds the
      points (used by the model that stores obstacles in field 32).
    - named edge/door message: field 2 = label, field 4 = points
      (used by the model that stores edges in field 28).
    """
    try:
        fields = _decode_message(payload)
    except ValueError:
        return []
    for field_num, wire_type, field_payload in fields:
        if field_num == 4 and wire_type == 2:
            pts = _collect_points(field_payload)
            if pts:
                return pts
    # Fallback: the payload itself is a points message.
    return _collect_points(payload)


def _decode_zone_zone_name(buf):
    """Return the human-readable room name for a zone payload, or None.

    The two models encode the display name differently:
    - the dry-only model puts it in field 3 (zone_name) directly;
    - the wet/deep model leaves "AZ_<n>" placeholders in fields 2/3 and puts
      the real name in the nested string field 16.
    Field 16 (when non-empty) is always the real name and wins.
    """
    real_name = None
    try:
        fields = _decode_message(buf)
    except ValueError:
        fields = []
    for field_num, wire_type, payload in fields:
        if field_num == 3 and wire_type == 2 and isinstance(payload, bytes):
            name = payload.decode("utf-8", errors="replace").strip()
            if name and not name.startswith("AZ_"):
                real_name = real_name or name
        elif field_num == 16 and wire_type == 2 and isinstance(payload, bytes):
            name = payload.decode("utf-8", errors="replace").strip()
            if name:
                real_name = name
    return real_name


def decode_zone(buf):
    offset = 0
    zone = {}
    if buf[offset] == 0x08:
        zone["type"], offset = decode_varint(buf, offset + 1)
    if offset < len(buf) and buf[offset] == 0x12:
        str_len, offset = decode_varint(buf, offset + 1)
        zone["zone_id"] = buf[offset : offset + str_len].decode("utf-8", errors="replace")
        offset += str_len
    if offset < len(buf) and buf[offset] == 0x1A:
        str_len, offset = decode_varint(buf, offset + 1)
        zone["zone_name"] = buf[offset : offset + str_len].decode("utf-8", errors="replace")
        offset += str_len
    if offset < len(buf) and buf[offset] == 0x22:
        boundary_len, offset = decode_varint(buf, offset + 1)
        boundary_data = buf[offset : offset + boundary_len]
        zone["boundary"] = decode_polygon_points(boundary_data)
        offset += boundary_len
    # Real room name. The wet/deep model stores it in the nested string
    # field 16, which follows field 5 and the repeated field-13 list, so it
    # is not reachable by the sequential offsets above. Look it up directly
    # and let it override the "AZ_<n>" placeholder.
    real_name = _decode_zone_zone_name(buf)
    if real_name:
        zone["zone_name"] = real_name
    return zone


def parse_floor_map(filepath):
    """Parse the binary into components needed for visualization."""
    data = Path(filepath).read_bytes()
    return parse_floor_map_bytes(data)


def parse_floor_map_bytes(data: bytes):
    """Parse floor map binary data into components needed for visualization.

    Args:
        data: Raw binary floor map data

    Returns:
        Dictionary with grid, zones, boundaries, pose, name, and map_id
    """
    offset = 0
    result = {}

    # Skip through top-level fields
    # Field 1: sequence
    _, offset = decode_varint(data, offset)
    _, offset = decode_varint(data, offset)
    # Field 2: name
    _, offset = decode_varint(data, offset)
    str_len, offset = decode_varint(data, offset)
    result["name"] = data[offset : offset + str_len].decode("utf-8")
    offset += str_len
    # Field 3: map_id
    _, offset = decode_varint(data, offset)
    str_len, offset = decode_varint(data, offset)
    result["map_id"] = data[offset : offset + str_len].decode("utf-8")
    offset += str_len
    # Field 4: map_type
    _, offset = decode_varint(data, offset)
    _, offset = decode_varint(data, offset)

    # Field 5: primary_grid (candidate for the rendered raster)
    _, offset = decode_varint(data, offset)
    grid_len, offset = decode_varint(data, offset)
    grid_buf = data[offset : offset + grid_len]
    grid_candidates = [decode_occupancy_grid(grid_buf)]
    offset += grid_len

    # Parse remaining fields
    zones = []
    boundaries = []
    pose = None

    while offset < len(data):
        tag_val, new_offset = decode_varint(data, offset)
        field_num = tag_val >> 3
        wire_type = tag_val & 0x07
        offset = new_offset

        if wire_type == 0:
            _, offset = decode_varint(data, offset)
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            payload = data[offset : offset + length]

            if field_num == 7:
                px = struct.unpack("<f", payload[1:5])[0]
                py = struct.unpack("<f", payload[6:10])[0]
                pz = struct.unpack("<f", payload[11:15])[0]
                pose = (px, py, pz)

            elif field_num == 15:
                zones.append(decode_zone(payload))

            elif field_num == 21:
                # Additional occupancy grid. The models do not agree on which
                # field holds the *full* map: one model's field 5 is only a
                # small area while its complete map is in field 21, and the
                # other model's field 5 already is the complete map. The
                # candidates are scored against the zone polygons below and
                # the best one is rendered.
                try:
                    grid = decode_occupancy_grid(payload)
                    if grid.get("cells"):
                        grid_candidates.append(grid)
                except (ValueError, struct.error):
                    pass

            elif field_num in (28, 32):
                # Boundary/obstacle polygons. The two models store them in
                # different fields with different point encodings:
                #   28: named edges (field 2 = "edge"/"door")
                #   32: plain boundaries
                pts = decode_boundary_payload(payload)
                if pts:
                    boundaries.append(pts)

            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            break

    result["grid"] = _pick_grid(grid_candidates, zones)
    result["zones"] = zones
    result["boundaries"] = boundaries
    result["pose"] = pose
    return result


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

# Cell value -> numeric category for colormap. The 0x0A and 0x19 values are
# interior/free cells on some models that are otherwise unmapped; routing them
# to the navigable category keeps them rendering as floor instead of gray.
CELL_CATEGORIES = {
    0x00: 0,  # free
    0x01: 1,  # unknown
    0x0F: 2,  # low confidence free
    0x0A: 3,  # navigable (alt encoding)
    0x19: 3,  # navigable (alt encoding)
    0x4B: 3,  # navigable
    0x5A: 4,  # partial occupied 90
    0x5C: 5,  # partial occupied 92
    0x64: 6,  # wall
    0x56: 7,  # virtual wall
}

CELL_COLORS = [
    "#FFFFFF",  # 0: free - white
    "#D0D0D0",  # 1: unknown - light gray
    "#E8F5E9",  # 2: low confidence free - pale green
    "#81C784",  # 3: navigable - green
    "#FF9800",  # 4: partial occupied 90 - orange
    "#F57C00",  # 5: partial occupied 92 - dark orange
    "#212121",  # 6: wall - near black
    "#F44336",  # 7: virtual wall - red
    "#9E9E9E",  # 8: other/default - gray
]

CELL_LABELS = [
    "Free",
    "Unknown",
    "Low-conf Free",
    "Navigable",
    "Partial (90%)",
    "Partial (92%)",
    "Wall",
    "Virtual Wall",
    "Other",
]

ZONE_COLORS = [
    "#2196F3",  # blue
    "#4CAF50",  # green
    "#FF9800",  # orange
    "#9C27B0",  # purple
    "#00BCD4",  # cyan
    "#E91E63",  # pink
    "#CDDC39",  # lime
    "#795548",  # brown
]

# Byte value -> category lookup table so the whole grid maps in one
# vectorized gather instead of a per-cell Python loop. Unmapped bytes fall
# into the "other" category (index 8).
_CELL_LUT = np.full(256, len(CELL_COLORS) - 1, dtype=np.uint8)
for _val, _cat in CELL_CATEGORIES.items():
    _CELL_LUT[_val] = _cat


def _grid_world_extent(grid):
    """Return (x_min, x_max, y_min, y_max) of an occupancy grid in meters."""
    res = grid.get("resolution", 0.0)
    ox, oy = grid.get("origin", (0.0, 0.0))
    rows = grid.get("width", 0) or 0
    cols = grid.get("height", 0) or 0
    return ox, ox + cols * res, oy, oy + rows * res


def _grid_zone_coverage(grid, zones):
    """Fraction of zone boundary points that fall inside the grid."""
    x_min, x_max, y_min, y_max = _grid_world_extent(grid)
    if x_min >= x_max or y_min >= y_max:
        return 0.0
    total = 0
    inside = 0
    for zone in zones:
        for x, y in zone.get("boundary", []):
            total += 1
            if x_min <= x <= x_max and y_min <= y <= y_max:
                inside += 1
    return inside / total if total else 0.0


def _pick_grid(grid_candidates, zones):
    """Choose the occupancy grid that best covers the zone polygons.

    Different robot models disagree about which field holds the full map, so
    the rendered grid is selected by scoring each candidate: zone coverage
    first (the full map contains every room), area as a tie-breaker. Falls
    back to the first candidate when no zones are present.
    """
    def valid(grid):
        # A grid whose dimensions disagree with the length of its cell buffer
        # is a mis-parse (e.g. a mis-decoded wrapper message) and must not be
        # chosen over the real map.
        cells = grid.get("cells")
        rows = grid.get("width", 0) or 0
        cols = grid.get("height", 0) or 0
        return cells is not None and rows * cols <= len(cells)

    valid_candidates = [g for g in grid_candidates if valid(g)] or grid_candidates

    def score(grid):
        coverage = _grid_zone_coverage(grid, zones)
        res = grid.get("resolution", 0.0) or 0.0
        area = res * res * (grid.get("width", 0) or 0) * (grid.get("height", 0) or 0)
        return (coverage, area)

    return max(valid_candidates, key=score)


def build_grid_image(grid) -> np.ndarray:
    """Convert raw cell bytes to a categorized numpy array.

    In this format the header "width" is the number of cell rows
    (y-direction) and "height" is the number of columns (x-direction).
    The buffer is row-major with the row index running along world y, so
    the returned array is shaped (rows, cols) = (width, height) with row 0
    at the origin (lowest y).
    """
    rows = grid["width"]
    cols = grid["height"]
    cells = grid["cells"]
    if len(cells) < rows * cols:
        raise ValueError(f"cells buffer too short: got {len(cells)}, need {rows * cols}")
    raw = np.frombuffer(cells[: rows * cols], dtype=np.uint8)
    return _CELL_LUT[raw.reshape(rows, cols)]


def render_floor_map(parsed, output_path=None, dpi=150, show_zones=True, show_boundaries=True) -> None:
    """Render the full annotated floor plan."""
    grid = parsed["grid"]
    resolution = grid["resolution"]
    origin_x, origin_y = grid["origin"]
    # Header "width" is the number of cell rows (y-direction), "height" is
    # the number of columns (x-direction). See build_grid_image.
    rows = grid["width"]
    cols = grid["height"]

    # World-space extent
    x_min = origin_x
    x_max = origin_x + cols * resolution
    y_min = origin_y
    y_max = origin_y + rows * resolution

    # Build grid image
    img = build_grid_image(grid)

    # Create colormap
    cmap = ListedColormap(CELL_COLORS)
    norm = BoundaryNorm(range(len(CELL_COLORS) + 1), cmap.N)

    # Figure setup
    fig_width = max(12, cols * resolution * 0.8)
    fig_height = max(9, rows * resolution * 0.8)
    fig: plt.Figure
    ax: plt.Axes
    fig, ax = plt.subplots(1, 1, figsize=(fig_width, fig_height), dpi=dpi)

    # Render grid (flip vertically so y increases upward)
    ax.imshow(
        img[::-1],
        cmap=cmap,
        norm=norm,
        extent=[x_min, x_max, y_min, y_max],
        interpolation="nearest",
        aspect="equal",
        zorder=1,
    )

    # Zone overlays
    if show_zones and parsed["zones"]:
        for i, zone in enumerate(parsed["zones"]):
            pts = zone.get("boundary", [])
            if len(pts) < 3:
                continue
            color = ZONE_COLORS[i % len(ZONE_COLORS)]
            polygon = MplPolygon(
                pts,
                closed=True,
                facecolor=color,
                edgecolor=color,
                alpha=0.2,
                linewidth=1.5,
                zorder=3,
            )
            ax.add_patch(polygon)
            # Zone outline
            outline = MplPolygon(
                pts,
                closed=True,
                facecolor="none",
                edgecolor=color,
                linewidth=2.0,
                linestyle="--",
                zorder=4,
            )
            ax.add_patch(outline)
            # Label at centroid
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            ax.text(
                cx,
                cy,
                zone.get("zone_name", zone.get("zone_id", "")),
                ha="center",
                va="center",
                fontsize=9,
                fontweight="bold",
                color=color,
                bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "alpha": 0.8, "edgecolor": color},
                zorder=5,
            )

    # Boundary outlines (obstacles/walls)
    if show_boundaries and parsed["boundaries"]:
        for boundary in parsed["boundaries"]:
            if len(boundary) < 3:
                continue
            polygon = MplPolygon(
                boundary,
                closed=True,
                facecolor="none",
                edgecolor="#D32F2F",
                linewidth=1.5,
                linestyle="-",
                zorder=4,
            )
            ax.add_patch(polygon)

    # Robot pose
    if parsed["pose"]:
        px, py, pz = parsed["pose"]
        ax.plot(px, py, "o", color="#1565C0", markersize=10, zorder=6)
        # Heading arrow (pz is yaw in radians)
        arrow_len = 0.4
        dx = arrow_len * math.cos(pz)
        dy = arrow_len * math.sin(pz)
        ax.annotate(
            "",
            xy=(px + dx, py + dy),
            xytext=(px, py),
            arrowprops={"arrowstyle": "->", "color": "#1565C0", "lw": 2.5},
            zorder=6,
        )
        ax.text(
            px + 0.15,
            py - 0.3,
            "Robot",
            fontsize=8,
            color="#1565C0",
            fontweight="bold",
            zorder=6,
        )

    # Axes
    ax.set_xlabel("X (meters)", fontsize=11)
    ax.set_ylabel("Y (meters)", fontsize=11)
    ax.set_title(
        f"Floor Map: {parsed['map_id']}  |  "
        f"{cols}x{rows} cells @ {resolution}m  |  "
        f"{cols * resolution:.1f}m x {rows * resolution:.1f}m",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_xlim(x_min - 0.5, x_max + 0.5)
    ax.set_ylim(y_min - 0.5, y_max + 0.5)
    ax.grid(True, alpha=0.2, linewidth=0.5)
    ax.set_aspect("equal")

    # Legend for cell types
    legend_patches = []
    for i, (color, label) in enumerate(zip(CELL_COLORS, CELL_LABELS)):
        legend_patches.append(mpatches.Patch(facecolor=color, edgecolor="#666", label=label))
    # Add zone entries
    if show_zones and parsed["zones"]:
        for i, zone in enumerate(parsed["zones"]):
            color = ZONE_COLORS[i % len(ZONE_COLORS)]
            legend_patches.append(
                mpatches.Patch(facecolor=color, alpha=0.3, edgecolor=color, label=f"Zone: {zone.get('zone_name', '')}")
            )
    if show_boundaries and parsed["boundaries"]:
        legend_patches.append(mpatches.Patch(facecolor="none", edgecolor="#D32F2F", label="Obstacle boundary"))
    if parsed["pose"]:
        legend_patches.append(mpatches.Patch(color="#1565C0", label="Robot pose"))

    ax.legend(
        handles=legend_patches,
        loc="upper left",
        bbox_to_anchor=(1.01, 1.0),
        fontsize=8,
        framealpha=0.9,
    )

    # Scale bar
    scale_len = 1.0  # 1 meter
    sx = x_min + 0.3
    sy = y_min + 0.3
    ax.plot([sx, sx + scale_len], [sy, sy], "k-", linewidth=3, zorder=7)
    ax.text(sx + scale_len / 2, sy + 0.15, "1m", ha="center", fontsize=8, zorder=7)

    plt.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
        print(f"Saved: {output_path} ({dpi} DPI)")
    else:
        output_path = "floor_map_visual.png"
        fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
        print(f"Saved: {output_path} ({dpi} DPI)")

    plt.close(fig)


# ---------------------------------------------------------------------------
# Pillow renderer (Roborock-style palette) for the HA MQTT image path
# ---------------------------------------------------------------------------

# Pixels per meter for the raster base. The dpi arg only tags the PNG; the
# on-screen size of the HA image entity is driven by this scale.
_PX_PER_M = 100.0
# Physical robot radius, used to size the sprite.
_ROBOT_RADIUS_M = 0.16


def render_floor_map_pillow(parsed, output=None, dpi=150, show_zones=True, show_boundaries=True) -> bytes:
    """Render the floor plan with Pillow using the Roborock blue/pastel palette.

    Writes a PNG to `output` (a path or a binary file-like object such as
    io.BytesIO) and returns the PNG bytes. Mirrors the Roborock look: dark-blue
    floor, light-blue walls, pastel zone fills with labels, semi-transparent
    obstacle fills, and the white/black robot sprite oriented by pose.

    `parsed` is the dict from parse_floor_map_bytes().
    """
    from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor

    palette = ColorsPalette()
    grid = parsed["grid"]
    res = grid["resolution"]
    ox, oy = grid["origin"]
    rows = grid["width"]  # header "width" = cell rows (y-direction)
    cols = grid["height"]  # header "height" = cell columns (x-direction)
    x_min, x_max = ox, ox + cols * res
    y_min, y_max = oy, oy + rows * res
    img_w = max(1, int(round((x_max - x_min) * _PX_PER_M)))
    img_h = max(1, int(round((y_max - y_min) * _PX_PER_M)))

    def w2p(x, y):
        """World meters (y up) -> image pixels (y down)."""
        return (int(round((x - x_min) * _PX_PER_M)), int(round((y_max - y) * _PX_PER_M)))

    # Floor base: map each cell category to a Roborock color, then upscale.
    cat_color = {
        0: palette.get_color(SupportedColor.MAP_OUTSIDE),  # free -> darker blue (looks "open")
        1: palette.get_color(SupportedColor.MAP_OUTSIDE),  # unknown
        2: palette.get_color(SupportedColor.MAP_INSIDE),  # low-confidence free
        3: palette.get_color(SupportedColor.MAP_INSIDE),  # navigable
        4: palette.get_color(SupportedColor.MAP_INSIDE),  # partial occupied 90
        5: palette.get_color(SupportedColor.MAP_INSIDE),  # partial occupied 92
        6: palette.get_color(SupportedColor.MAP_WALL),  # wall -> light blue
        7: palette.get_color(SupportedColor.VIRTUAL_WALLS),  # virtual wall -> red
        8: palette.get_color(SupportedColor.MAP_OUTSIDE),  # other/unknown bytes
    }
    rgb_lut = np.zeros((9, 3), dtype=np.uint8)
    for cat, col in cat_color.items():
        rgb_lut[cat] = col[:3]
    cats = build_grid_image(grid)  # (rows, cols), one category per cell
    rgb = rgb_lut[cats[::-1]]  # flip so world y (up) maps to the image top
    base = Image.fromarray(rgb, "RGB").resize((img_w, img_h), Image.NEAREST)

    # Zone + obstacle overlays on a transparent layer.
    overlay = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, "RGBA")
    if show_zones:
        for i, zone in enumerate(parsed["zones"]):
            pts = zone.get("boundary", [])
            if len(pts) < 3:
                continue
            color = palette.get_room_color(i + 1)[:3]
            poly = [w2p(x, y) for x, y in pts]
            od.polygon(poly, fill=(*color, 0x8F), outline=(*color, 0xFF))
    if show_boundaries:
        obst = palette.get_color(SupportedColor.OBSTACLE)
        for boundary in parsed["boundaries"]:
            if len(boundary) < 3:
                continue
            poly = [w2p(x, y) for x, y in boundary]
            od.polygon(poly, fill=(*obst[:3], 128), outline=(*obst[:3], 160))

    final = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    fd = ImageDraw.Draw(final, "RGBA")

    # Robot sprite (Roborock style), oriented by pose yaw.
    pose = parsed.get("pose")
    if pose:
        px, py, pz = pose
        cx, cy = w2p(px, py)
        r = max(6, int(round(_ROBOT_RADIUS_M * _PX_PER_M)))
        fill = tuple(palette.get_color(SupportedColor.ROBO)[:3])
        outline = tuple(palette.get_color(SupportedColor.ROBO_OUTLINE)[:3])
        _draw_robot(fd, cx, cy, math.degrees(pz), r, outline, fill)

    # Zone labels on top, with a halo for legibility.
    if show_zones:
        font = ImageFont.load_default(size=max(10, int(round(_PX_PER_M / 6))))
        for zone in parsed["zones"]:
            pts = zone.get("boundary", [])
            if len(pts) < 3:
                continue
            name = zone.get("zone_name") or zone.get("zone_id") or ""
            if not name:
                continue
            cx = sum(w2p(x, y)[0] for x, y in pts) / len(pts)
            cy = sum(w2p(x, y)[1] for x, y in pts) / len(pts)
            fd.text(
                (cx, cy),
                name,
                font=font,
                fill=(0, 0, 0, 255),
                anchor="mm",
                stroke_width=2,
                stroke_fill=(255, 255, 255, 255),
            )

    if output is None:
        output = io.BytesIO()
    final.save(output, format="PNG", dpi=(dpi, dpi))
    if hasattr(output, "getvalue"):
        return output.getvalue()
    return Path(output).read_bytes()


def _draw_robot(draw, cx, cy, heading_deg, r, outline, fill):
    """Draw the Roborock-style vacuum sprite, oriented by heading in degrees.

    Faithful port of
    vacuum_map_parser_base.image_generator.ImageGenerator._draw_vacuum,
    with the image-y flipped relative to world-y.
    """
    a = heading_deg
    r_scaled = r / 16
    # main body
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=outline, fill=fill)
    if r >= 8:
        # secondary ring
        r2 = r_scaled * 14
        draw.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], outline=outline)
    # bin cover
    a1 = (a + 104) / 180 * math.pi
    a2 = (a - 104) / 180 * math.pi
    r2 = r_scaled * 13
    x1 = cx - r2 * math.cos(a1)
    y1 = cy + r2 * math.sin(a1)
    x2 = cx - r2 * math.cos(a2)
    y2 = cy + r2 * math.sin(a2)
    draw.line([x1, y1, x2, y2], width=1, fill=outline)
    # lidar
    angle = a / 180 * math.pi
    r2 = r_scaled * 3
    lx = cx + r2 * math.cos(angle)
    ly = cy - r2 * math.sin(angle)
    r3 = r_scaled * 4
    draw.ellipse([lx - r3, ly - r3, lx + r3, ly + r3], outline=outline, fill=fill)
    # button
    half = (
        (outline[0] + fill[0]) // 2,
        (outline[1] + fill[1]) // 2,
        (outline[2] + fill[2]) // 2,
    )
    r2 = r_scaled * 10
    bx = cx + r2 * math.cos(angle)
    by = cy - r2 * math.sin(angle)
    r3 = r_scaled * 2
    draw.ellipse([bx - r3, by - r3, bx + r3, by + r3], outline=half, fill=half)


def main():
    parser = argparse.ArgumentParser(description="Visualize floor map protobuf as annotated image")
    parser.add_argument("input", help="Path to .bin file")
    parser.add_argument("--output", "-o", help="Output image path (png, pdf, svg)")
    parser.add_argument("--dpi", type=int, default=150, help="Image DPI (default: 150)")
    parser.add_argument("--no-zones", action="store_true", help="Hide zone overlays")
    parser.add_argument("--no-boundaries", action="store_true", help="Hide boundary outlines")
    args = parser.parse_args()

    parsed = parse_floor_map(args.input)

    render_floor_map(
        parsed,
        output_path=args.output,
        dpi=args.dpi,
        show_zones=not args.no_zones,
        show_boundaries=not args.no_boundaries,
    )


if __name__ == "__main__":
    main()
