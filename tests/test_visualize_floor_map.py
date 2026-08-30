from src.visualize_floor_map import build_grid_image


def test_build_grid_image_uses_header_width_as_row_count():
    # Header "width" = cell rows (y-dir), "height" = cols (x-dir).
    grid = {"width": 4, "height": 3, "cells": bytes(range(12))}
    img = build_grid_image(grid)
    assert img.shape == (4, 3)
    assert img[3, 2] == 8  # 0x0C unmapped -> "other"