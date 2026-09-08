from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Any


class CellState(StrEnum):
    EMPTY = "empty"
    GROWING = "growing"
    READY = "ready"
    UNKNOWN = "unknown"


class RunMode(StrEnum):
    HARVEST = "harvest"
    PLANT = "plant"
    CYCLE = "cycle"


class DeviceKind(StrEnum):
    DEMO = "demo"
    ADB = "adb"


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)

    def jitter(self, rng, amount: int) -> Point:
        if amount <= 0:
            return self
        return Point(
            self.x + int(rng.integers(-amount, amount + 1)),
            self.y + int(rng.integers(-amount, amount + 1)),
        )


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x2 and self.y <= y < self.y2

    def clamp_to(self, width: int, height: int) -> Rect:
        x = max(0, min(self.x, width - 1))
        y = max(0, min(self.y, height - 1))
        w = max(1, min(self.w, width - x))
        h = max(1, min(self.h, height - y))
        return Rect(x, y, w, h)


@dataclass(frozen=True, slots=True)
class Cell:
    row: int
    col: int
    state: CellState
    center: Point
    score: float = 0.0
    box: Rect | None = None


@dataclass(frozen=True, slots=True)
class Swipe:
    start: Point
    end: Point
    duration_ms: int
    cells: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Tap:
    point: Point
    hold_ms: int = 40


@dataclass
class BotConfig:
    device: DeviceKind = DeviceKind.DEMO
    adb_host: str = "127.0.0.1"
    adb_port: int = 5555
    adb_serial: str = ""
    farm: Rect = field(default_factory=lambda: Rect(80, 90, 880, 470))
    rows: int = 5
    cols: int = 8
    seed_button: Point = field(default_factory=lambda: Point(160, 620))
    deselect_point: Point = field(default_factory=lambda: Point(40, 40))
    run_mode: RunMode = RunMode.CYCLE
    match_threshold: float = 0.72
    cycle_pause_s: float = 2.4
    jitter_px: int = 4
    swipe_ms_per_px: float = 1.15
    swipe_min_ms: int = 280
    swipe_max_ms: int = 900
    tap_pause_s: float = 0.28
    max_swipes_per_cycle: int = 24
    demo_grow_s: float = 4.0
    demo_cols: int = 8
    demo_rows: int = 5

    def cell_rect(self, row: int, col: int) -> Rect:
        cw = self.farm.w / self.cols
        ch = self.farm.h / self.rows
        x = int(self.farm.x + col * cw)
        y = int(self.farm.y + row * ch)
        return Rect(x, y, max(1, int(cw)), max(1, int(ch)))

    def cell_center(self, row: int, col: int) -> Point:
        box = self.cell_rect(row, col)
        return Point(box.x + box.w // 2, box.y + box.h // 2)

    def hit_cell(self, x: int, y: int) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        best_d = None
        for row in range(self.rows):
            for col in range(self.cols):
                box = self.cell_rect(row, col)
                if not box.contains(x, y):
                    continue
                center = self.cell_center(row, col)
                dist = (center.x - x) ** 2 + (center.y - y) ** 2
                if best_d is None or dist < best_d:
                    best_d = dist
                    best = (row, col)
        return best

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["device"] = str(self.device)
        payload["run_mode"] = str(self.run_mode)
        payload["farm"] = {
            "x": self.farm.x,
            "y": self.farm.y,
            "w": self.farm.w,
            "h": self.farm.h,
        }
        payload["seed_button"] = {"x": self.seed_button.x, "y": self.seed_button.y}
        payload["deselect_point"] = {
            "x": self.deselect_point.x,
            "y": self.deselect_point.y,
        }
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotConfig:
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            kwargs[key] = value
        if "device" in kwargs:
            kwargs["device"] = DeviceKind(kwargs["device"])
        if "run_mode" in kwargs:
            kwargs["run_mode"] = RunMode(kwargs["run_mode"])
        farm = kwargs.get("farm")
        if isinstance(farm, dict):
            kwargs["farm"] = Rect(
                int(farm["x"]), int(farm["y"]), int(farm["w"]), int(farm["h"])
            )
        seed = kwargs.get("seed_button")
        if isinstance(seed, dict):
            kwargs["seed_button"] = Point(int(seed["x"]), int(seed["y"]))
        deselect = kwargs.get("deselect_point")
        if isinstance(deselect, dict):
            kwargs["deselect_point"] = Point(int(deselect["x"]), int(deselect["y"]))
        return cls(**kwargs)
