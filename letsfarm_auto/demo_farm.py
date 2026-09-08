from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from letsfarm_auto.models import BotConfig, CellState, Point, Rect, Swipe, Tap

CROPS = (
    {"name": "lua", "ready": (48, 188, 230), "sprout": (70, 150, 70)},
    {"name": "ca_chua", "ready": (54, 52, 210), "sprout": (68, 160, 78)},
    {"name": "bap", "ready": (40, 200, 245), "sprout": (64, 148, 72)},
    {"name": "dua_hau", "ready": (56, 150, 46), "sprout": (72, 155, 68)},
    {"name": "ca_rot", "ready": (40, 110, 230), "sprout": (66, 152, 74)},
)


def _noise(shape: tuple[int, ...], rng: np.random.Generator, amp: int = 10) -> np.ndarray:
    return rng.integers(-amp, amp + 1, size=shape, dtype=np.int16)


@dataclass
class Plot:
    state: CellState = CellState.EMPTY
    crop: int = 0
    ready_at: float = 0.0


@dataclass
class DemoFarm:
    """Synthetic Let's Farm board used when BlueStacks is not connected."""

    width: int = 1080
    height: int = 720
    config: BotConfig = field(default_factory=BotConfig)
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(7))
    plots: list[list[Plot]] = field(default_factory=list)
    selected_seed: bool = False
    coins: int = 1280
    name: str = "Mô phỏng Let's Farm"
    world_farm: Rect = field(init=False)
    world_rows: int = field(init=False)
    world_cols: int = field(init=False)
    world_seed: Point = field(init=False)

    def __post_init__(self) -> None:
        self.world_farm = Rect(
            self.config.farm.x, self.config.farm.y, self.config.farm.w, self.config.farm.h
        )
        self.world_rows = self.config.rows
        self.world_cols = self.config.cols
        self.world_seed = Point(self.config.seed_button.x, self.config.seed_button.y)
        tray_top = min(self.height - 86, self.world_seed.y - 28) - 8
        if self.world_farm.y2 > tray_top:
            self.world_farm = Rect(
                self.world_farm.x,
                self.world_farm.y,
                self.world_farm.w,
                max(80, tray_top - self.world_farm.y),
            )
        if not self.plots:
            self.reset()

    def reset(self) -> None:
        self.plots = [
            [Plot() for _ in range(self.world_cols)] for _ in range(self.world_rows)
        ]
        now = time.monotonic()
        for r in range(self.world_rows):
            for c in range(self.world_cols):
                roll = int(self.rng.integers(0, 10))
                crop = int(self.rng.integers(0, len(CROPS)))
                plot = self.plots[r][c]
                plot.crop = crop
                if roll <= 3:
                    plot.state = CellState.READY
                elif roll <= 6:
                    plot.state = CellState.GROWING
                    plot.ready_at = now + self.config.demo_grow_s * (0.4 + 0.8 * self.rng.random())
                else:
                    plot.state = CellState.EMPTY
        self.selected_seed = False
        self.coins = 1280

    def tick(self) -> None:
        now = time.monotonic()
        for row in self.plots:
            for plot in row:
                if plot.state == CellState.GROWING and now >= plot.ready_at:
                    plot.state = CellState.READY

    def screenshot(self) -> np.ndarray:
        self.tick()
        return render_farm(self)

    def tap(self, action: Tap) -> None:
        self.tick()
        x, y = action.point.x, action.point.y
        if self._is_seed(x, y):
            self.selected_seed = True
            return
        deselect = self.config.deselect_point
        if abs(x - deselect.x) < 40 and abs(y - deselect.y) < 40:
            self.selected_seed = False
            return
        hit = self.hit_cell(x, y)
        if hit and self.selected_seed:
            r, c = hit
            self._plant(r, c)

    def swipe(self, action: Swipe) -> None:
        self.tick()
        visited = cells_on_segment(action.start, action.end, self)
        for r, c in visited:
            plot = self.plots[r][c]
            if plot.state == CellState.READY:
                plot.state = CellState.EMPTY
                self.coins += 8 + int(self.rng.integers(0, 6))
            elif plot.state == CellState.EMPTY and self.selected_seed:
                self._plant(r, c)
        if visited:
            self.selected_seed = False

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def cell_rect(self, row: int, col: int) -> Rect:
        cw = self.world_farm.w / self.world_cols
        ch = self.world_farm.h / self.world_rows
        x = int(self.world_farm.x + col * cw)
        y = int(self.world_farm.y + row * ch)
        return Rect(x, y, max(1, int(cw)), max(1, int(ch)))

    def cell_center(self, row: int, col: int) -> Point:
        box = self.cell_rect(row, col)
        return Point(box.x + box.w // 2, box.y + box.h // 2)

    def hit_cell(self, x: int, y: int) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        best_d = None
        for row in range(self.world_rows):
            for col in range(self.world_cols):
                box = self.cell_rect(row, col)
                if not box.contains(x, y):
                    continue
                center = self.cell_center(row, col)
                dist = (center.x - x) ** 2 + (center.y - y) ** 2
                if best_d is None or dist < best_d:
                    best_d = dist
                    best = (row, col)
        return best

    def _is_seed(self, x: int, y: int) -> bool:
        for seed in (self.world_seed, self.config.seed_button):
            if abs(x - seed.x) < 55 and abs(y - seed.y) < 42:
                return True
        return False

    def _plant(self, row: int, col: int) -> None:
        plot = self.plots[row][col]
        if plot.state != CellState.EMPTY:
            return
        plot.state = CellState.GROWING
        plot.crop = int(self.rng.integers(0, len(CROPS)))
        plot.ready_at = time.monotonic() + self.config.demo_grow_s
        self.selected_seed = True


def cells_on_segment(start: Point, end: Point, source) -> list[tuple[int, int]]:
    dist = max(1, int(((end.x - start.x) ** 2 + (end.y - start.y) ** 2) ** 0.5))
    steps = max(dist, 8)
    seen: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        x = int(start.x + (end.x - start.x) * t)
        y = int(start.y + (end.y - start.y) * t)
        hit = source.hit_cell(x, y)
        if hit and hit not in seen:
            seen.append(hit)
    return seen


def render_farm(farm: DemoFarm) -> np.ndarray:
    rng = farm.rng
    img = np.zeros((farm.height, farm.width, 3), dtype=np.uint8)
    img[:, :] = (78, 148, 92)
    grass = img.astype(np.int16) + _noise(img.shape, rng, 9)
    img = np.clip(grass, 0, 255).astype(np.uint8)

    hud = Rect(0, 0, farm.width, 72)
    cv2.rectangle(img, (hud.x, hud.y), (hud.x2, hud.y2), (54, 92, 46), -1)
    cv2.putText(
        img,
        "Let's Farm",
        (24, 46),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.05,
        (230, 240, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.circle(img, (farm.width - 210, 36), 16, (40, 196, 255), -1)
    cv2.putText(
        img,
        str(farm.coins),
        (farm.width - 188, 44),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (240, 250, 255),
        2,
        cv2.LINE_AA,
    )

    pad = 10
    farm_box = farm.world_farm
    cv2.rectangle(
        img,
        (farm_box.x - pad, farm_box.y - pad),
        (farm_box.x2 + pad, farm_box.y2 + pad),
        (36, 48, 44),
        6,
    )

    for r in range(farm.world_rows):
        for c in range(farm.world_cols):
            box = farm.cell_rect(r, c)
            _draw_plot(img, box, farm.plots[r][c], rng)

    tray_y = min(farm.height - 86, farm.world_seed.y - 28)
    cv2.rectangle(img, (40, tray_y), (farm.width - 40, farm.height - 18), (48, 70, 92), -1)
    cv2.rectangle(img, (40, tray_y), (farm.width - 40, farm.height - 18), (30, 44, 60), 3)
    seed = farm.world_seed
    color = (54, 52, 210) if farm.selected_seed else (46, 96, 210)
    cv2.rectangle(img, (seed.x - 42, seed.y - 28), (seed.x + 42, seed.y + 28), color, -1)
    cv2.rectangle(
        img, (seed.x - 42, seed.y - 28), (seed.x + 42, seed.y + 28), (255, 255, 255), 2
    )
    cv2.putText(
        img,
        "HAT GIONG",
        (seed.x - 38, seed.y + 6),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    labels = ("Lua", "Ca chua", "Bap", "Dua hau")
    for i, label in enumerate(labels):
        x = 280 + i * 150
        y = seed.y
        cv2.circle(img, (x, y), 22, CROPS[i]["ready"], -1)
        cv2.putText(
            img,
            label,
            (x - 28, y + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (230, 230, 230),
            1,
            cv2.LINE_AA,
        )
    return img


def _draw_plot(img: np.ndarray, box: Rect, plot: Plot, rng: np.random.Generator) -> None:
    soil = (42, 92, 150)
    cv2.rectangle(img, (box.x + 4, box.y + 4), (box.x2 - 4, box.y2 - 4), soil, -1)
    cv2.rectangle(img, (box.x + 4, box.y + 4), (box.x2 - 4, box.y2 - 6), (58, 118, 176), 1)
    cx, cy = box.x + box.w // 2, box.y + box.h // 2
    crop = CROPS[plot.crop % len(CROPS)]
    if plot.state == CellState.GROWING:
        cv2.circle(img, (cx, cy + 8), 8, crop["sprout"], -1)
        cv2.line(img, (cx, cy + 10), (cx, cy - 6), crop["sprout"], 2)
    elif plot.state == CellState.READY:
        cv2.ellipse(img, (cx, cy + 6), (box.w // 4, box.h // 5), 0, 0, 360, crop["ready"], -1)
        cv2.circle(img, (cx - 10, cy - 4), 9, crop["ready"], -1)
        cv2.circle(img, (cx + 11, cy - 2), 8, crop["ready"], -1)
        highlight = tuple(min(255, c + 40) for c in crop["ready"])
        cv2.circle(img, (cx, cy - 10), 6, highlight, -1)
    dirt = img[box.y + 5 : box.y2 - 5, box.x + 5 : box.x2 - 5]
    if dirt.size:
        noisy = dirt.astype(np.int16) + _noise(dirt.shape, rng, 6)
        img[box.y + 5 : box.y2 - 5, box.x + 5 : box.x2 - 5] = np.clip(noisy, 0, 255).astype(
            np.uint8
        )
