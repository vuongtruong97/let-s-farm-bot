from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.vision.detector import DetectedObject


class FieldStatus(str, Enum):
    EMPTY = "EMPTY"
    GROWING = "GROWING"
    READY = "READY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FieldState:
    id: str
    x: int
    y: int
    width: int
    height: int
    state: FieldStatus
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def harvest_swipe(self) -> tuple[int, int, int, int]:
        y = self.y + self.height // 2
        x1 = self.x + max(8, self.width // 6)
        x2 = self.x + self.width - max(8, self.width // 6)
        return x1, y, x2, y

    def plant_drag_from(self, seed_x: int, seed_y: int) -> tuple[int, int, int, int]:
        """Drag seed onto soil along an isometric diagonal, not a horizontal line.

        Hay Day plots are diamonds; a row of tiles runs down-right or down-left.
        One ADB swipe is a straight line, so we start on the seed icon and end
        at the far bottom corner of the detected plot.
        """
        pad_x = max(8, self.width // 6)
        pad_y = max(8, self.height // 6)
        se = (self.x + self.width - pad_x, self.y + self.height - pad_y)
        sw = (self.x + pad_x, self.y + self.height - pad_y)
        d_se = (seed_x - se[0]) ** 2 + (seed_y - se[1]) ** 2
        d_sw = (seed_x - sw[0]) ** 2 + (seed_y - sw[1]) ** 2
        end = se if d_se >= d_sw else sw
        return int(seed_x), int(seed_y), int(end[0]), int(end[1])

    def to_object(self) -> DetectedObject:
        from app.vision.detector import DetectedObject
        return DetectedObject(
            type="field",
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            confidence=self.confidence,
            state=self.state.value,
        )

    @classmethod
    def from_object(cls, obj: DetectedObject, index: int) -> FieldState:
        try:
            status = FieldStatus(obj.state or "UNKNOWN")
        except ValueError:
            status = FieldStatus.UNKNOWN
        return cls(
            id=f"field:{index}",
            x=obj.x,
            y=obj.y,
            width=obj.width,
            height=obj.height,
            state=status,
            confidence=obj.confidence,
        )
