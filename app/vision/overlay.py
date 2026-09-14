from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.config import SCREENSHOT_DIR
from app.state.field_state import FieldStatus
from app.vision.detector import DetectedObject
from app.vision.fields import PlotCell
from app.vision.screen import GameScreen, ScreenDetection
from app.vision.template_matcher import as_bgr

_COLORS = {
    "ui": (40, 180, 40),
    "popup": (40, 40, 220),
    "field": (0, 200, 255),
    "newspaper": (180, 80, 0),
    "shop": (0, 140, 255),
    "item": (200, 0, 200),
    "price": (0, 200, 200),
}

_STATE_COLORS = {
    "READY": (0, 220, 255),
    "EMPTY": (0, 80, 220),
    "GROWING": (40, 180, 40),
}

_SCREEN_COLOR = {
    GameScreen.FARM: (40, 180, 40),
    GameScreen.POPUP: (40, 40, 220),
    GameScreen.NEWSPAPER: (180, 80, 0),
    GameScreen.PLAYER_SHOP: (0, 140, 255),
    GameScreen.UNKNOWN: (80, 80, 80),
}


def draw_overlay(source, detection: ScreenDetection) -> np.ndarray:
    image = as_bgr(source).copy()
    color = _SCREEN_COLOR.get(detection.screen, (200, 200, 0))
    label = f"{detection.screen.value} {detection.confidence:.2f}"
    cv2.rectangle(image, (8, 8), (8 + 12 * len(label) + 24, 48), color, -1)
    cv2.putText(
        image,
        label,
        (16, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    for obj in detection.objects:
        _draw_object(image, obj)
    return image


def save_overlay(source, detection: ScreenDetection, path: Path | None = None) -> Path:
    target = path or (SCREENSHOT_DIR / "debug" / "overlay.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    image = draw_overlay(source, detection)
    cv2.imwrite(str(target), image)
    return target


def draw_plot_plan(source, cells: list[PlotCell]) -> np.ndarray:
    """Paint each 3x3 cell, tap points on EMPTY plots, diagonal sow arrows."""
    image = as_bgr(source).copy()
    overlay = image.copy()
    for cell in cells:
        pts = np.array(cell.quad, dtype=np.int32)
        color = _STATE_COLORS.get(cell.state.value, (180, 180, 180))
        if cell.state is FieldStatus.EMPTY:
            cv2.fillConvexPoly(overlay, pts, (0, 0, 220))
        cv2.polylines(image, [pts], True, color, 2, cv2.LINE_AA)
        cx, cy = cell.center
        cv2.putText(
            image,
            f"{cell.row * 3 + cell.col + 1}",
            (cx - 8, cy + 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    image = cv2.addWeighted(image, 0.62, overlay, 0.38, 0)
    clusters = sorted({c.cluster for c in cells})
    for cluster_id in clusters:
        group = [c for c in cells if c.cluster == cluster_id]
        empties = [c for c in group if c.state is FieldStatus.EMPTY]
        if len(empties) < 3:
            continue
        tap = min(empties, key=lambda c: (c.row, c.col))
        tx, ty = tap.center
        cv2.circle(image, (tx, ty), 18, (0, 0, 0), 5)
        cv2.circle(image, (tx, ty), 18, (0, 255, 255), 3)
        cv2.circle(image, (tx, ty), 6, (0, 255, 255), -1)
        cv2.putText(
            image,
            f"TAP ({tx},{ty})",
            (tx + 22, ty - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        by_row: dict[int, list[PlotCell]] = {}
        for cell in empties:
            by_row.setdefault(cell.row, []).append(cell)
        for row, row_cells in by_row.items():
            row_cells.sort(key=lambda c: c.col)
            if len(row_cells) < 2:
                continue
            p1 = row_cells[0].center
            p2 = row_cells[-1].center
            cv2.arrowedLine(image, p1, p2, (0, 0, 0), 6, cv2.LINE_AA, tipLength=0.18)
            cv2.arrowedLine(image, p1, p2, (0, 255, 0), 3, cv2.LINE_AA, tipLength=0.18)
    cv2.rectangle(image, (14, 112), (760, 268), (0, 0, 0), -1)
    cv2.rectangle(image, (14, 112), (760, 268), (40, 40, 255), 2)
    cv2.putText(
        image,
        "Do = tung o nho TRONG trong cum 3x3",
        (30, 152),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 255),
        2,
    )
    cv2.putText(
        image,
        "Vang = TAP 1 o de mo khay hat",
        (30, 196),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
    )
    cv2.putText(
        image,
        "Xanh = keo gieo cheo theo hang o",
        (30, 240),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
    )
    return image


def _draw_object(image: np.ndarray, obj: DetectedObject) -> None:
    color = _STATE_COLORS.get(obj.state or "", _COLORS.get(obj.type, (0, 200, 200)))
    pt1 = (obj.x, obj.y)
    pt2 = (obj.x + obj.width, obj.y + obj.height)
    cv2.rectangle(image, pt1, pt2, color, 2)
    text = f"{obj.state or obj.type} {obj.confidence:.2f}"
    ty = max(18, obj.y - 6)
    cv2.putText(
        image,
        text,
        (obj.x, ty),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        color,
        1,
        cv2.LINE_AA,
    )
