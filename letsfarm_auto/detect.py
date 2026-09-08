from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from letsfarm_auto.models import Cell, CellState, Point, Rect
from letsfarm_auto.vision import ColorThresholds, classify_cell_bgr


@dataclass(slots=True)
class FarmLayout:
    grid: list[list[Cell]]
    farm: Rect
    seed: Point
    deselect: Point
    rows: int
    cols: int
    plot_count: int


def analyze_screen(
    screen: np.ndarray, thresholds: ColorThresholds | None = None
) -> FarmLayout:
    """Find plots and the seed bag from a screenshot — no manual grid."""
    t = thresholds or ColorThresholds()
    h, w = screen.shape[:2]
    hud_h, tray_top = _chrome_bands(h)
    playfield = Rect(0, hud_h, w, max(1, tray_top - hud_h))
    blobs = _plot_blobs(screen, playfield)
    rows = _cluster_rows(blobs)
    grid = _classify_rows(screen, rows, t)
    farm = _bounds([cell.box for row in grid for cell in row if cell.box], playfield)
    seed = detect_seed_button(screen)
    deselect = Point(max(12, w // 40), max(12, hud_h // 3))
    return FarmLayout(
        grid=grid,
        farm=farm,
        seed=seed,
        deselect=deselect,
        rows=len(grid),
        cols=max((len(row) for row in grid), default=0),
        plot_count=sum(len(row) for row in grid),
    )


def detect_seed_button(screen: np.ndarray) -> Point:
    h, w = screen.shape[:2]
    y0 = int(h * 0.78)
    tray = screen[y0:, :]
    hsv = cv2.cvtColor(tray, cv2.COLOR_BGR2HSV)
    hue, sat, val = cv2.split(hsv)
    # Inventory chips are saturated. Prefer the leftmost seed-bag / tool tile,
    # not the tiny round crop icons further right.
    hot = (sat >= 90) & (val >= 80)
    mask = (hot.astype(np.uint8)) * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[int, int, int, int]] = []
    tray_area = max(1, tray.shape[0] * tray.shape[1])
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        area = cw * ch
        if area < 500 or area > tray_area * 0.35:
            continue
        if ch < 16 or cw < 20:
            continue
        cx = x + cw // 2
        cy = y0 + y + ch // 2
        # Wider tiles (the seed bag button) beat small circles when x is close.
        candidates.append((x, -area, cx, cy))
    if not candidates:
        return Point(max(80, w // 7), min(h - 40, int(h * 0.88)))
    candidates.sort()
    _x, _neg_area, cx, cy = candidates[0]
    return Point(int(cx), int(cy))


def _chrome_bands(height: int) -> tuple[int, int]:
    hud_h = max(52, int(height * 0.10))
    tray_top = height - max(72, int(height * 0.155))
    return hud_h, tray_top


def _plot_blobs(screen: np.ndarray, playfield: Rect) -> list[Rect]:
    roi = screen[playfield.y : playfield.y2, playfield.x : playfield.x2]
    if roi.size == 0:
        return []
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hue, sat, val = cv2.split(hsv)
    soil = (hue >= 6) & (hue <= 28) & (sat >= 60) & (val >= 70) & (val <= 210)
    ripe = (
        (((hue <= 12) | (hue >= 160)) & (sat >= 80) & (val >= 100))
        | ((hue >= 12) & (hue <= 40) & (sat >= 80) & (val >= 175))
    )
    occ = ((soil | ripe).astype(np.uint8)) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    occ = cv2.morphologyEx(occ, cv2.MORPH_CLOSE, kernel)
    occ = cv2.morphologyEx(occ, cv2.MORPH_OPEN, kernel)
    n_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(occ)
    field_area = playfield.w * playfield.h
    min_area = max(280, int(field_area * 0.0012))
    max_area = int(field_area * 0.045)
    blobs: list[Rect] = []
    for i in range(1, n_labels):
        x, y, bw, bh, area = stats[i]
        if area < min_area or area > max_area:
            continue
        aspect = bw / max(1, bh)
        if aspect < 0.42 or aspect > 2.4:
            continue
        fill = area / max(1, bw * bh)
        if fill < 0.38:
            continue
        blobs.append(Rect(playfield.x + int(x), playfield.y + int(y), int(bw), int(bh)))
    return blobs


def _cluster_rows(blobs: list[Rect]) -> list[list[Rect]]:
    if not blobs:
        return []
    heights = sorted(b.h for b in blobs)
    y_tol = max(18, int(heights[len(heights) // 2] * 0.55))
    ordered = sorted(blobs, key=lambda b: (b.y + b.h / 2, b.x))
    rows: list[list[Rect]] = []
    row_ys: list[float] = []
    for blob in ordered:
        cy = blob.y + blob.h / 2
        if rows and abs(cy - row_ys[-1]) <= y_tol:
            rows[-1].append(blob)
            row_ys[-1] = sum(b.y + b.h / 2 for b in rows[-1]) / len(rows[-1])
        else:
            rows.append([blob])
            row_ys.append(cy)
    for row in rows:
        row.sort(key=lambda b: b.x)
    return rows


def _classify_rows(
    screen: np.ndarray, rows: list[list[Rect]], thresholds: ColorThresholds
) -> list[list[Cell]]:
    grid: list[list[Cell]] = []
    for r, row in enumerate(rows):
        line: list[Cell] = []
        for c, box in enumerate(row):
            crop = screen[box.y : box.y2, box.x : box.x2]
            state, score = classify_cell_bgr(crop, thresholds)
            line.append(
                Cell(
                    row=r,
                    col=c,
                    state=state,
                    center=Point(box.x + box.w // 2, box.y + box.h // 2),
                    score=score,
                    box=box,
                )
            )
        grid.append(line)
    return grid


def _bounds(boxes: list[Rect | None], fallback: Rect) -> Rect:
    valid = [b for b in boxes if b is not None]
    if not valid:
        return fallback
    x1 = min(b.x for b in valid)
    y1 = min(b.y for b in valid)
    x2 = max(b.x2 for b in valid)
    y2 = max(b.y2 for b in valid)
    return Rect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))
