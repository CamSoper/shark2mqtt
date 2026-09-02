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
import logging
import math
import struct
import sys
from pathlib import Path

import numpy as np

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

    # Field 5: primary_grid
    _, offset = decode_varint(data, offset)
    grid_len, offset = decode_varint(data, offset)
    grid_buf = data[offset : offset + grid_len]
    result["grid"] = decode_occupancy_grid(grid_buf)
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

            elif field_num == 32:
                # Boundary polygons
                inner_offset = 0
                while inner_offset < len(payload):
                    if payload[inner_offset] == 0x22:
                        poly_len, inner_offset = decode_varint(payload, inner_offset + 1)
                        poly_data = payload[inner_offset : inner_offset + poly_len]
                        pts = decode_polygon_points(poly_data)
                        if pts:
                            boundaries.append(pts)
                        inner_offset += poly_len
                    else:
                        inner_offset += 1

            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            break

    result["zones"] = zones
    result["boundaries"] = boundaries
    result["pose"] = pose
    return result


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

# Cell value -> numeric category for colormap
CELL_CATEGORIES = {
    0x00: 0,  # free
    0x01: 1,  # unknown
    0x0F: 2,  # low confidence free
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
# vectorized gather instead of a per-cell Python loop.
_CELL_LUT = np.full(256, 8, dtype=np.uint8)
for _val, _cat in CELL_CATEGORIES.items():
    _CELL_LUT[_val] = _cat


def build_grid_image(grid):
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


def render_floor_map(parsed, output_path=None, dpi=150, show_zones=True, show_boundaries=True):
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
