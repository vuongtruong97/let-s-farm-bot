"""Detect field plots and crop state. Vision only — no taps."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from app.state.field_state import FieldState, FieldStatus
from app.vision.detector import DetectedObject
from app.vision.regions import PLAY_AREA
from app.vision.template_matcher import as_bgr
from app.storage.logger import get_logger

log = get_logger("VISION")

# HSV ranges measured on 1920x1080 Small-UI farm screenshots (OpenCV H: 0-180).
READY_HSV = ((20, 160, 190), (34, 255, 255))
EMPTY_HSV = ((6, 80, 50), (20, 230, 155))
GROWING_HSV = ((48, 140, 70), (75, 255, 190))

MIN_AREA_FRAC = 0.002
MAX_AREA_FRAC = 0.12
MIN_ASPECT = 0.4
MAX_ASPECT = 3.2
CLUSTER_MIN_AREA_FRAC = 0.004
CELL_SOIL_FRAC = 0.22


Quad = tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]


@dataclass(frozen=True)
class PlotCell:
    """One of 9 small plots inside a Hay Day 3x3 cluster."""

    cluster: int
    row: int
    col: int
    quad: Quad
    state: FieldStatus
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return int(sum(xs) / 4), int(sum(ys) / 4)

    def to_field(self, index: int) -> FieldState:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        x, y = min(xs), min(ys)
        return FieldState(
            id=f"field:{index}",
            x=x,
            y=y,
            width=max(xs) - x,
            height=max(ys) - y,
            state=self.state,
            confidence=self.confidence,
        )


class FieldDetector:
    def detect(self, source) -> list[DetectedObject]:
        fields = self.detect_fields(source)
        return [field.to_object() for field in fields]

    def detect_fields(self, source) -> list[FieldState]:
        fields = self._detect_blobs(source)
        _log_counts(fields)
        return fields

    def detect_cells(self, source) -> list[PlotCell]:
        image = as_bgr(source)
        h, w = image.shape[:2]
        x0, y0, rw, rh = PLAY_AREA.to_pixels(w, h)
        hsv = cv2.cvtColor(image[y0 : y0 + rh, x0 : x0 + rw], cv2.COLOR_BGR2HSV)
        ready_mask, empty_mask, grow_mask = _state_masks(hsv)
        soil = cv2.bitwise_or(ready_mask, cv2.bitwise_or(empty_mask, grow_mask))
        soil = _clean(soil, 7, close_iter=2)
        min_area = int(CLUSTER_MIN_AREA_FRAC * h * w)
        max_area = int(MAX_AREA_FRAC * h * w)
        cells: list[PlotCell] = []
        n_labels, labels, stats, _cents = cv2.connectedComponentsWithStats(soil, 8)
        cluster_id = 0
        for i in range(1, n_labels):
            x, y, width, height, area = (int(v) for v in stats[i])
            if area < min_area or area > max_area:
                continue
            blob = (labels == i).astype(np.uint8) * 255
            split = _split_isometric_3x3(
                blob, ready_mask, empty_mask, grow_mask, x0, y0, cluster_id
            )
            if split is None:
                continue
            cells.extend(split)
            cluster_id += 1
        cells = _keep_crop_clusters(cells)
        cells.sort(key=lambda c: (c.cluster, c.row, c.col))
        return cells

    def _detect_blobs(self, source) -> list[FieldState]:
        image = as_bgr(source)
        h, w = image.shape[:2]
        x0, y0, rw, rh = PLAY_AREA.to_pixels(w, h)
        hsv = cv2.cvtColor(image[y0 : y0 + rh, x0 : x0 + rw], cv2.COLOR_BGR2HSV)
        ready_mask, empty_mask, grow_mask = _state_masks(hsv)
        min_area = int(MIN_AREA_FRAC * h * w)
        max_area = int(MAX_AREA_FRAC * h * w)
        ready = _blobs(ready_mask, FieldStatus.READY, x0, y0, min_area, max_area)
        empty = _blobs(empty_mask, FieldStatus.EMPTY, x0, y0, min_area, max_area)
        growing = _blobs(grow_mask, FieldStatus.GROWING, x0, y0, min_area, max_area)
        empty = [box for box in empty if all(_iou(box, r) < 0.25 for r in ready)]
        empty = [box for box in empty if all(_iou(box, g) < 0.45 for g in growing)]
        anchors = ready + empty
        growing = [box for box in growing if _near_any(box, anchors, max(w, h) * 0.22)]
        ordered = sorted(ready + empty + growing, key=lambda b: (b.y, b.x))
        return [
            FieldState(
                id=f"field:{i}",
                x=box.x,
                y=box.y,
                width=box.width,
                height=box.height,
                state=box.state,
                confidence=box.confidence,
            )
            for i, box in enumerate(ordered)
        ]


def _state_masks(hsv: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ready_mask = _clean(_hsv_mask(hsv, READY_HSV), 7, close_iter=2)
    empty_mask = _clean(_hsv_mask(hsv, EMPTY_HSV), 9, close_iter=2)
    grow_mask = _clean(_hsv_mask(hsv, GROWING_HSV), 7, close_iter=2)
    empty_mask[ready_mask > 0] = 0
    grow_mask[ready_mask > 0] = 0
    return ready_mask, empty_mask, grow_mask


def _split_isometric_3x3(
    blob: np.ndarray,
    ready_mask: np.ndarray,
    empty_mask: np.ndarray,
    grow_mask: np.ndarray,
    ox: int,
    oy: int,
    cluster: int,
) -> list[PlotCell] | None:
    origin, long_vec, short_vec = _cluster_axes(blob)
    if origin is None:
        return None
    cells: list[PlotCell] = []
    for row in range(3):
        for col in range(3):
            quad = _cell_quad(origin, long_vec, short_vec, row, col, ox, oy)
            state, conf = _classify_quad(quad, ready_mask, empty_mask, grow_mask, ox, oy)
            if state is FieldStatus.UNKNOWN:
                continue
            cells.append(
                PlotCell(
                    cluster=cluster,
                    row=row,
                    col=col,
                    quad=quad,
                    state=state,
                    confidence=conf,
                )
            )
    if len(cells) < 4:
        return None
    return cells


def _keep_crop_clusters(cells: list[PlotCell]) -> list[PlotCell]:
    """Drop trees/buildings that happen to split into a 3x3."""
    kept: list[PlotCell] = []
    for cluster_id in sorted({c.cluster for c in cells}):
        group = [c for c in cells if c.cluster == cluster_id]
        if _is_crop_cluster(group):
            kept.extend(group)
    return kept


def _is_crop_cluster(cells: list[PlotCell]) -> bool:
    if len(cells) < 6:
        return False
    soil = [c for c in cells if c.state in (FieldStatus.EMPTY, FieldStatus.READY)]
    if sum(c.confidence for c in soil) < 1.8:
        return False
    angles: list[float] = []
    by_row: dict[int, list[PlotCell]] = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)
    for row_cells in by_row.values():
        row_cells.sort(key=lambda c: c.col)
        if len(row_cells) < 2:
            continue
        x1, y1 = row_cells[0].center
        x2, y2 = row_cells[-1].center
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        ang = min(ang, 180 - ang)
        angles.append(ang)
    if not angles:
        return False
    mean_ang = sum(angles) / len(angles)
    return 12.0 <= mean_ang <= 55.0


def _cluster_axes(
    blob: np.ndarray,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None, None
    contour = max(contours, key=cv2.contourArea)
    rect = cv2.minAreaRect(contour)
    width, height = rect[1]
    short, long = min(width, height), max(width, height)
    if short < 48 or long < 100 or long / max(short, 1) > 4.5:
        return None, None, None
    pts = cv2.boxPoints(rect).astype(np.float32)
    long_edges: list[tuple[float, int]] = []
    for i in range(4):
        vec = pts[(i + 1) % 4] - pts[i]
        long_edges.append((float(np.hypot(vec[0], vec[1])), i))
    long_edges.sort(reverse=True)
    i1, i2 = long_edges[0][1], long_edges[1][1]

    def _mean_y(index: int) -> float:
        return float((pts[index][1] + pts[(index + 1) % 4][1]) / 2)

    start = i1 if _mean_y(i1) <= _mean_y(i2) else i2
    origin = pts[start]
    long_vec = pts[(start + 1) % 4] - origin
    short_vec = pts[(start - 1) % 4] - origin
    if short_vec[1] < 0:
        origin = pts[(start + 1) % 4]
        long_vec = pts[start] - origin
        short_vec = pts[(start + 2) % 4] - origin
    return origin, long_vec, short_vec


def _cell_quad(
    origin: np.ndarray,
    long_vec: np.ndarray,
    short_vec: np.ndarray,
    row: int,
    col: int,
    ox: int,
    oy: int,
) -> Quad:
    u0, u1 = col / 3.0, (col + 1) / 3.0
    v0, v1 = row / 3.0, (row + 1) / 3.0
    corners = [
        origin + u0 * long_vec + v0 * short_vec,
        origin + u1 * long_vec + v0 * short_vec,
        origin + u1 * long_vec + v1 * short_vec,
        origin + u0 * long_vec + v1 * short_vec,
    ]
    return tuple(
        (int(round(p[0] + ox)), int(round(p[1] + oy))) for p in corners
    )  # type: ignore[return-value]


def _classify_quad(
    quad: Quad,
    ready_mask: np.ndarray,
    empty_mask: np.ndarray,
    grow_mask: np.ndarray,
    ox: int,
    oy: int,
) -> tuple[FieldStatus, float]:
    poly = np.array([[x - ox, y - oy] for x, y in quad], dtype=np.int32)
    h, w = empty_mask.shape[:2]
    cell_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillConvexPoly(cell_mask, poly, 255)
    total = int(np.count_nonzero(cell_mask))
    if total < 40:
        return FieldStatus.UNKNOWN, 0.0
    ready = int(np.count_nonzero(cv2.bitwise_and(ready_mask, cell_mask)))
    empty = int(np.count_nonzero(cv2.bitwise_and(empty_mask, cell_mask)))
    grow = int(np.count_nonzero(cv2.bitwise_and(grow_mask, cell_mask)))
    soil = ready + empty + grow
    if soil / total < CELL_SOIL_FRAC:
        return FieldStatus.UNKNOWN, 0.0
    votes = (
        (FieldStatus.READY, ready),
        (FieldStatus.EMPTY, empty),
        (FieldStatus.GROWING, grow),
    )
    state, count = max(votes, key=lambda item: item[1])
    if count == 0:
        return FieldStatus.UNKNOWN, 0.0
    return state, round(count / total, 3)


def _log_counts(fields: list[FieldState]) -> None:
    counts = {status.value: 0 for status in FieldStatus}
    for field in fields:
        counts[field.state.value] += 1
    log.info(
        f"fields ready={counts['READY']} empty={counts['EMPTY']} "
        f"growing={counts['GROWING']}"
    )


def _hsv_mask(hsv: np.ndarray, bounds: tuple[tuple[int, ...], tuple[int, ...]]) -> np.ndarray:
    return cv2.inRange(hsv, np.array(bounds[0]), np.array(bounds[1]))


def _clean(mask: np.ndarray, kernel: int, close_iter: int = 1) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=close_iter)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)


def _blobs(
    mask: np.ndarray,
    state: FieldStatus,
    ox: int,
    oy: int,
    min_area: int,
    max_area: int,
) -> list[FieldState]:
    n_labels, _labels, stats, _cents = cv2.connectedComponentsWithStats(mask, 8)
    found: list[FieldState] = []
    for i in range(1, n_labels):
        x, y, width, height, area = (int(v) for v in stats[i])
        if area < min_area or area > max_area:
            continue
        aspect = width / max(height, 1)
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            continue
        confidence = min(1.0, area / max(width * height, 1))
        found.append(
            FieldState(
                id="",
                x=ox + x,
                y=oy + y,
                width=width,
                height=height,
                state=state,
                confidence=round(confidence, 3),
            )
        )
    return found


def _iou(a: FieldState, b: FieldState) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.width, b.x + b.width)
    y2 = min(a.y + a.height, b.y + b.height)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


def _near_any(box: FieldState, others: list[FieldState], dist: float) -> bool:
    if not others:
        return False
    cx, cy = box.center
    for other in others:
        ox, oy = other.center
        if math.hypot(cx - ox, cy - oy) <= dist:
            return True
    return False
