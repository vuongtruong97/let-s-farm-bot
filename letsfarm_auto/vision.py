from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from letsfarm_auto.models import BotConfig, Cell, CellState, Point, Rect


@dataclass(slots=True)
class ColorThresholds:
    green_h: tuple[int, int] = (35, 90)
    yellow_h: tuple[int, int] = (12, 40)
    ready_color_ratio: float = 0.07
    ready_green_ratio: float = 0.12
    growing_green_ratio: float = 0.012
    produce_s: int = 80
    produce_v: int = 175
    red_v: int = 100


def _ratio(mask: np.ndarray) -> float:
    if mask.size == 0:
        return 0.0
    return float(mask.mean())


def _in_range(channel: np.ndarray, bounds: tuple[int, int]) -> np.ndarray:
    lo, hi = bounds
    return (channel >= lo) & (channel <= hi)


def classify_cell_bgr(
    cell_bgr: np.ndarray, thresholds: ColorThresholds | None = None
) -> tuple[CellState, float]:
    """Classify a plot crop from BGR pixels.

    Ready crops are colorful (gold/red) or densely leafy. Fresh sprouts are a
    small green patch on soil. Bare plots stay mostly brown.
    """
    t = thresholds or ColorThresholds()
    if cell_bgr.size == 0:
        return CellState.UNKNOWN, 0.0

    h, w = cell_bgr.shape[:2]
    inset = cell_bgr[max(0, h // 8) : h - h // 8, max(0, w // 8) : w - w // 8]
    if inset.size == 0:
        inset = cell_bgr
    hsv = cv2.cvtColor(inset, cv2.COLOR_BGR2HSV)
    hue, sat, val = cv2.split(hsv)

    green = _in_range(hue, t.green_h) & (sat >= 50) & (val >= 50)
    yellow = (
        _in_range(hue, t.yellow_h) & (sat >= t.produce_s) & (val >= t.produce_v)
    )
    red = ((hue <= 10) | (hue >= 160)) & (sat >= t.produce_s) & (val >= t.red_v)
    bright = (val >= t.produce_v) & (sat >= t.produce_s)

    green_ratio = _ratio(green)
    ripe_ratio = max(_ratio(yellow), _ratio(red), _ratio(bright))

    if ripe_ratio >= t.ready_color_ratio:
        return CellState.READY, min(1.0, 0.55 + ripe_ratio)
    if green_ratio >= t.ready_green_ratio:
        return CellState.READY, min(1.0, 0.5 + green_ratio)
    if green_ratio >= t.growing_green_ratio:
        return CellState.GROWING, min(1.0, 0.4 + green_ratio * 2)
    return CellState.EMPTY, 0.7


def match_template(
    screen: np.ndarray, template: np.ndarray, threshold: float
) -> list[tuple[Point, float]]:
    if template.size == 0 or screen.size == 0:
        return []
    if (
        template.shape[0] > screen.shape[0]
        or template.shape[1] > screen.shape[1]
    ):
        return []
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    tmpl = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if template.ndim == 3 else template
    result = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    hits: list[tuple[Point, float]] = []
    ys, xs = np.where(result >= threshold)
    th, tw = tmpl.shape[:2]
    for y, x in zip(ys.tolist(), xs.tolist(), strict=False):
        hits.append((Point(x + tw // 2, y + th // 2), float(result[y, x])))
    hits.sort(key=lambda item: item[1], reverse=True)
    return _nms(hits, min(tw, th) // 2)


def _nms(
    hits: list[tuple[Point, float]], radius: int
) -> list[tuple[Point, float]]:
    kept: list[tuple[Point, float]] = []
    for point, score in hits:
        if any((point.x - k.x) ** 2 + (point.y - k.y) ** 2 < radius * radius for k, _ in kept):
            continue
        kept.append((point, score))
    return kept


def scan_farm(
    screen: np.ndarray,
    config: BotConfig,
    thresholds: ColorThresholds | None = None,
) -> list[list[Cell]]:
    grid: list[list[Cell]] = []
    for row in range(config.rows):
        line: list[Cell] = []
        for col in range(config.cols):
            box = config.cell_rect(row, col)
            crop = _crop(screen, box)
            state, score = classify_cell_bgr(crop, thresholds)
            line.append(
                Cell(
                    row=row,
                    col=col,
                    state=state,
                    center=config.cell_center(row, col),
                    score=score,
                )
            )
        grid.append(line)
    return grid


def _crop(screen: np.ndarray, box: Rect) -> np.ndarray:
    h, w = screen.shape[:2]
    x1 = max(0, box.x)
    y1 = max(0, box.y)
    x2 = min(w, box.x2)
    y2 = min(h, box.y2)
    if x2 <= x1 or y2 <= y1:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    return screen[y1:y2, x1:x2]


def overlay_grid(
    screen: np.ndarray,
    grid: list[list[Cell]],
    config: BotConfig,
    swipes: list | None = None,
) -> np.ndarray:
    canvas = screen.copy()
    colors = {
        CellState.EMPTY: (62, 92, 168),
        CellState.GROWING: (72, 168, 80),
        CellState.READY: (36, 176, 232),
        CellState.UNKNOWN: (150, 150, 150),
    }
    for row in grid:
        for cell in row:
            box = cell.box or config.cell_rect(cell.row, cell.col)
            color = colors[cell.state]
            cv2.rectangle(canvas, (box.x, box.y), (box.x2, box.y2), color, 2)
            cv2.circle(canvas, cell.center.as_tuple(), 4, color, -1)
    if swipes:
        for swipe in swipes:
            cv2.arrowedLine(
                canvas,
                swipe.start.as_tuple(),
                swipe.end.as_tuple(),
                (0, 215, 255),
                3,
                tipLength=0.08,
            )
    if grid:
        farm = config.farm
        cv2.rectangle(
            canvas,
            (farm.x, farm.y),
            (farm.x2, farm.y2),
            (255, 255, 255),
            2,
        )
    seed = config.seed_button
    cv2.circle(canvas, seed.as_tuple(), 16, (40, 80, 255), 2)
    cv2.putText(
        canvas,
        "hat giong",
        (seed.x + 18, seed.y + 4),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (40, 80, 255),
        1,
        cv2.LINE_AA,
    )
    return canvas


def encode_jpeg(image: np.ndarray, quality: int = 82) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("Không nén được ảnh JPEG")
    return buf.tobytes()
